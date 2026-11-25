"""Custom Windows mouse cursor: one image for the base state, another for clickable elements (hand).

Uses SetSystemCursor, so it works system-wide with no overlay. Cursors reset on logoff/reboot,
and this app re-applies them at start. Run with pythonw.exe to have no console window.
"""
import ctypes
import json
import os
import struct
import sys
import wave

from PIL import Image, ImageSequence
from PyQt6.QtGui import QAction, QColor, QIcon, QImage, QPixmap
from PyQt6.QtCore import Qt, QTimer, QUrl
from PyQt6.QtMultimedia import QAudioDecoder, QAudioFormat, QSoundEffect
from PyQt6.QtWidgets import (
    QApplication, QDialog, QDialogButtonBox, QFileDialog, QFormLayout, QGridLayout, QHBoxLayout, QLabel,
    QLineEdit, QMenu, QMessageBox, QPushButton, QSlider, QSystemTrayIcon, QWidget,
)

APP_DIR = os.path.join(os.environ.get("APPDATA", os.path.expanduser("~")), "CustomCursor")
CONFIG = os.path.join(APP_DIR, "config.json")
ICON_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ICO", "ICONO.jpg")
OCR_NORMAL, OCR_HAND, OCR_WAIT, OCR_APPSTARTING = 32512, 32649, 32514, 32650
KEYS = {"base": (OCR_NORMAL,), "click": (OCR_HAND,), "loading": (OCR_WAIT, OCR_APPSTARTING)}
SPI_SETCURSORS = 0x0057
IMAGE_CURSOR, LR_LOADFROMFILE = 2, 0x10
SIZE = 32  # default cursor size
MIN_SIZE, MAX_SIZE = 16, 64
PREVIEW_SRC = 128  # max side of the in-memory preview source frames

user32 = ctypes.windll.user32
user32.LoadImageW.restype = ctypes.c_void_p
user32.CopyImage.restype = ctypes.c_void_p
user32.CopyImage.argtypes = [ctypes.c_void_p, ctypes.c_uint, ctypes.c_int, ctypes.c_int, ctypes.c_uint]
user32.SetSystemCursor.argtypes = [ctypes.c_void_p, ctypes.c_uint]


def cur_bytes(img, size):
    """Encode an RGBA PIL image as a 32-bit .cur (hotspot at top-left)."""
    img = img.convert("RGBA").resize((size, size), Image.LANCZOS)
    r, g, b, a = img.split()
    xor = Image.merge("RGBA", (b, g, r, a)).transpose(Image.FLIP_TOP_BOTTOM).tobytes()  # BGRA, bottom-up
    and_mask = b"\x00" * (((size + 31) // 32) * 4 * size)
    header = struct.pack("<IiiHHIIiiII", 40, size, size * 2, 1, 32, 0, len(xor) + len(and_mask), 0, 0, 0, 0)
    data = header + xor + and_mask
    entry = struct.pack("<BBBBHHII", size, size, 0, 0, 0, 0, len(data), 22)
    return struct.pack("<HHH", 0, 2, 1) + entry + data


def read_frames(src):
    """Return [(RGBA image, duration in ms)] for any image or animated GIF."""
    out = []
    with Image.open(src) as im:
        for frame in ImageSequence.Iterator(im):
            out.append((frame.convert("RGBA"), frame.info.get("duration", 100) or 100))
    return out


def _chunk(tag, data):
    return tag + struct.pack("<I", len(data)) + data + (b"\x00" if len(data) % 2 else b"")


def frames_to_ani(frames, size):
    """Build an animated cursor (.ani) from frames; durations are in 1/60 s jiffies."""
    curs = [cur_bytes(img, size) for img, _ in frames]
    rates = [max(1, round(ms * 60 / 1000)) for _, ms in frames]
    n = len(frames)
    anih = struct.pack("<IIIIIIIII", 36, n, n, 0, 0, 0, 1, rates[0], 1)
    fram = b"fram" + b"".join(_chunk(b"icon", c) for c in curs)
    body = (b"ACON" + _chunk(b"anih", anih) + _chunk(b"rate", struct.pack(f"<{n}I", *rates))
            + _chunk(b"LIST", fram))
    return b"RIFF" + struct.pack("<I", len(body)) + body


def image_to_cursor_file(src, dst_base, size=SIZE):
    """Write a .cur (static) or .ani (multi-frame GIF) next to dst_base; return its path."""
    frames = read_frames(src)
    for ext in (".cur", ".ani"):
        if os.path.exists(dst_base + ext):
            os.remove(dst_base + ext)
    if len(frames) > 1:
        path, data = dst_base + ".ani", frames_to_ani(frames, size)
    else:
        path, data = dst_base + ".cur", cur_bytes(frames[0][0], size)
    with open(path, "wb") as f:
        f.write(data)
    return path


def set_cursor(path, cursor_id):
    # Loaded fresh each call: SetSystemCursor destroys the handle, and CopyImage would drop the animation
    h = user32.LoadImageW(None, path, IMAGE_CURSOR, 0, 0, LR_LOADFROMFILE)
    if not h:
        raise OSError("No se pudo cargar el cursor")
    user32.SetSystemCursor(h, cursor_id)


def restore_default():
    user32.SystemParametersInfoW(SPI_SETCURSORS, 0, None, 0)


def clamp_size(v):
    try:
        return max(MIN_SIZE, min(MAX_SIZE, int(v)))
    except (TypeError, ValueError):
        return SIZE


def load_config():
    try:
        with open(CONFIG, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"base": "", "click": ""}


def save_config(cfg):
    os.makedirs(APP_DIR, exist_ok=True)
    with open(CONFIG, "w", encoding="utf-8") as f:
        json.dump(cfg, f)


def apply(cfg):
    os.makedirs(APP_DIR, exist_ok=True)
    for key, ids in KEYS.items():
        src = cfg.get(key)
        if src and os.path.isfile(src):
            path = image_to_cursor_file(src, os.path.join(APP_DIR, key), clamp_size(cfg.get("size")))
            for cid in ids:
                set_cursor(path, cid)


class ClickSound:
    """Plays a sound on every left click, system-wide, via a low-level mouse hook.

    The hook and the audio player only exist while a sound file is set, so it costs nothing otherwise.
    """
    WH_MOUSE_LL, WM_LBUTTONDOWN = 14, 0x0201
    PROC = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, ctypes.c_int, ctypes.c_size_t, ctypes.c_size_t)

    MAX_SECONDS = 10  # cap decoded audio so memory stays small

    def __init__(self):
        self.effect = None
        self.decoder = None
        self.pcm = bytearray()
        self.pending_test = False
        self.path = None
        self.volume = 1.0
        self.hook = None
        self.proc = self.PROC(self._on_mouse)  # keep a reference or it gets garbage collected
        user32.SetWindowsHookExW.argtypes = [ctypes.c_int, self.PROC, ctypes.c_void_p, ctypes.c_uint]
        user32.SetWindowsHookExW.restype = ctypes.c_void_p
        user32.CallNextHookEx.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_size_t, ctypes.c_size_t]
        user32.CallNextHookEx.restype = ctypes.c_ssize_t
        user32.UnhookWindowsHookEx.argtypes = [ctypes.c_void_p]

    def _on_mouse(self, code, wparam, lparam):
        if code >= 0 and wparam == self.WM_LBUTTONDOWN:
            QTimer.singleShot(0, self.play)  # return from the hook immediately
        return user32.CallNextHookEx(None, code, wparam, lparam)

    def play(self):
        if self.effect is not None and self.effect.status() == QSoundEffect.Status.Ready:
            self.effect.stop()
            self.effect.setVolume(self.volume)
            self.effect.play()

    def configure(self, volume_pct):
        """volume_pct 0-100."""
        self.volume = max(0, min(100, int(volume_pct))) / 100
        if self.effect is not None:
            self.effect.setVolume(self.volume)

    def _decode(self, path):
        """Decode any mp3/ogg once to a small WAV, so clicks play with low latency via QSoundEffect."""
        self._drop_decoder()
        self.pcm = bytearray()
        fmt = QAudioFormat()
        fmt.setSampleFormat(QAudioFormat.SampleFormat.Int16)
        fmt.setChannelCount(2)
        fmt.setSampleRate(44100)
        self.decoder = dec = QAudioDecoder()
        dec.setAudioFormat(fmt)
        dec.setSource(QUrl.fromLocalFile(path))
        limit = self.MAX_SECONDS * 44100 * 4

        def on_buffer():
            buf = dec.read()
            if buf.isValid() and len(self.pcm) < limit:
                self.pcm += buf.data().tobytes() if hasattr(buf.data(), "tobytes") else bytes(buf.data())

        dec.bufferReady.connect(on_buffer)
        dec.finished.connect(lambda: self._decoded(dec))
        dec.error.connect(lambda *_: self._drop_decoder() if dec is self.decoder else None)
        dec.start()

    def _drop_decoder(self):
        if self.decoder is not None:
            self.decoder.stop()
            self.decoder.deleteLater()
            self.decoder = None

    def _decoded(self, dec):
        if dec is not self.decoder or not self.pcm:
            return
        os.makedirs(APP_DIR, exist_ok=True)
        wav = os.path.join(APP_DIR, "click.wav")
        with wave.open(wav, "wb") as f:
            f.setnchannels(2)
            f.setsampwidth(2)
            f.setframerate(44100)
            f.writeframes(bytes(self.pcm[: self.MAX_SECONDS * 44100 * 4]))
        self.pcm = bytearray()
        self._drop_decoder()
        self.effect = QSoundEffect()
        self.effect.setVolume(self.volume)
        self.effect.setSource(QUrl.fromLocalFile(wav))
        if self.pending_test:
            self.pending_test = False
            self.effect.statusChanged.connect(self.play)

    def test(self, path):
        if self.effect is not None and path == self.path:
            self.play()
        elif path and os.path.isfile(path):
            self.pending_test = True
            self.path = path
            self._decode(path)

    def set_file(self, path):
        if path and os.path.isfile(path):
            if path != self.path or self.effect is None:
                self.path = path
                self.effect = None
                self._decode(path)
            if self.hook is None:
                self.hook = user32.SetWindowsHookExW(self.WH_MOUSE_LL, self.proc, None, 0)
        else:
            self.path = None
            if self.hook is not None:
                user32.UnhookWindowsHookEx(self.hook)
                self.hook = None
            self._drop_decoder()
            self.effect = None


class SoundSettings(QDialog):
    """Click sound volume; changes apply live so they can be tested."""

    def __init__(self, parent, clicks, volume):
        super().__init__(parent)
        self.setWindowTitle("Configuración del sonido")
        self.clicks = clicks
        self.initial = volume
        form = QFormLayout(self)
        self.vol = QSlider(Qt.Orientation.Horizontal)
        self.vol.setRange(0, 100)
        self.vol.setValue(volume)
        self.vol_lbl = QLabel()
        row = QHBoxLayout()
        row.addWidget(self.vol)
        row.addWidget(self.vol_lbl)
        form.addRow("Volumen:", row)
        box =  QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        box.accepted.connect(self.accept)
        box.rejected.connect(self.reject)
        form.addRow(box)
        self.vol.valueChanged.connect(self.changed)
        self.changed()

    def changed(self, _=None):
        self.vol_lbl.setText(f"{self.vol.value()}%")
        self.clicks.configure(self.vol.value())

    def reject(self):
        self.clicks.configure(self.initial)
        super().reject()

    def value(self):
        return self.vol.value()


class Window(QWidget):
    """Window that reports show/hide so previews only consume resources while visible."""

    def __init__(self, on_show, on_hide):
        super().__init__()
        self._on_show, self._on_hide = on_show, on_hide

    def showEvent(self, e):
        super().showEvent(e)
        self._on_show()

    def hideEvent(self, e):
        super().hideEvent(e)
        self._on_hide()


class App:
    def __init__(self):
        self.cfg = load_config()
        self.root = Window(self.start_previews, self.stop_previews)
        self.root.setWindowTitle("Custom Cursor")
        grid = QGridLayout(self.root)
        self.edits = {}
        labels = {"base": "Imagen estado base:", "click": "Imagen al poder hacer click:",
                  "loading": "GIF de carga:"}
        for i, k in enumerate(KEYS):
            edit = QLineEdit(self.cfg.get(k, ""))
            edit.setMinimumWidth(320)
            self.edits[k] = edit
            btn = QPushButton("Subir...")
            btn.clicked.connect(lambda _=False, k=k: self.pick(k))
            grid.addWidget(QLabel(labels[k]), i, 0)
            grid.addWidget(edit, i, 1)
            grid.addWidget(btn, i, 2)
        self.sound_edit = QLineEdit(self.cfg.get("sound", ""))
        sound_btn = QPushButton("Subir...")
        sound_btn.clicked.connect(self.pick_sound)
        test_btn = QPushButton("▶")
        test_btn.setToolTip("Probar sonido")
        test_btn.setFixedWidth(30)
        test_btn.clicked.connect(self.test_sound)
        gear_btn = QPushButton("⚙")
        gear_btn.setToolTip("Configuración del sonido")
        gear_btn.setFixedWidth(30)
        gear_btn.clicked.connect(self.open_sound_settings)
        sound_row = QHBoxLayout()
        sound_row.addWidget(sound_btn)
        sound_row.addWidget(test_btn)
        sound_row.addWidget(gear_btn)
        grid.addWidget(QLabel("Sonido de click (mp3/ogg):"), 3, 0)
        grid.addWidget(self.sound_edit, 3, 1)
        grid.addLayout(sound_row, 3, 2)
        self.clicks = ClickSound()
        self.sound_vol = max(0, min(100, int(self.cfg.get("sound_volume", 100))))
        self.clicks.configure(self.sound_vol)
        self.slider = QSlider(Qt.Orientation.Horizontal)
        self.slider.setRange(MIN_SIZE, MAX_SIZE)
        self.slider.setValue(clamp_size(self.cfg.get("size")))
        self.size_lbl = QLabel()
        self.slider.valueChanged.connect(self.on_size)
        self.on_size()
        grid.addWidget(QLabel("Tamaño:"), 4, 0)
        grid.addWidget(self.slider, 4, 1)
        grid.addWidget(self.size_lbl, 4, 2)
        prev = QHBoxLayout()
        self.previews = {}
        self.frames = {k: ([], []) for k in KEYS}  # key -> (source pixmaps, durations in ms)
        self.idx = {k: 0 for k in KEYS}
        self.timers = {}
        for k, title in (("base", "Base"), ("click", "Click"), ("loading", "Carga")):
            box = QLabel()
            box.setFixedSize(MAX_SIZE + 16, MAX_SIZE + 16)
            box.setAlignment(Qt.AlignmentFlag.AlignCenter)
            box.setStyleSheet("background: #d8d8d8; border: 1px solid #888;")
            self.previews[k] = box
            timer = QTimer(self.root)
            timer.setSingleShot(True)
            timer.timeout.connect(lambda k=k: self.next_frame(k))
            self.timers[k] = timer
            prev.addStretch()
            prev.addWidget(QLabel(f"{title}:"))
            prev.addWidget(box)
        prev.addStretch()
        grid.addLayout(prev, 5, 0, 1, 3)
        self.load_timer = QTimer(self.root)  # debounce typing: decode files only after a pause
        self.load_timer.setSingleShot(True)
        self.load_timer.setInterval(300)
        self.load_timer.timeout.connect(self.load_previews)
        for e in self.edits.values():
            e.textChanged.connect(lambda _: self.load_timer.start())
        bar = QHBoxLayout()
        for text, fn in (("Aplicar", self.on_apply), ("Restaurar por defecto", self.on_reset),
                         ("Segundo plano", self.root.hide)):
            b = QPushButton(text)
            b.clicked.connect(fn)
            bar.addWidget(b)
        grid.addLayout(bar, 6, 0, 1, 3)

        pix = QPixmap(ICON_PATH)
        if pix.isNull():  # fallback if the icon file is missing
            pix = QPixmap(64, 64)
            pix.fill(QColor(30, 120, 220))
        icon = QIcon(pix)
        self.root.setWindowIcon(icon)
        self.tray = QSystemTrayIcon(icon)
        self.tray.setToolTip("Custom Cursor")
        menu = QMenu()
        open_act = QAction("Abrir", menu)
        open_act.triggered.connect(self.show_window)
        quit_act = QAction("Salir", menu)
        quit_act.triggered.connect(self.quit)
        menu.addAction(open_act)
        menu.addAction(quit_act)
        self.menu = menu
        self.tray.setContextMenu(menu)
        self.tray.activated.connect(
            lambda r: self.show_window() if r == QSystemTrayIcon.ActivationReason.Trigger else None)
        self.tray.show()

        if any(self.cfg.get(k) for k in (*KEYS, "sound")):
            try:
                apply(self.cfg)
            except Exception:
                pass  # start hidden when already configured
            self.clicks.set_file(self.cfg.get("sound", ""))
        else:
            self.root.show()

    def pick_sound(self):
        p, _ = QFileDialog.getOpenFileName(self.root, "Elegir sonido", "", "Audio (*.mp3 *.ogg)")
        if p:
            self.sound_edit.setText(p)

    def open_sound_settings(self):
        dlg = SoundSettings(self.root, self.clicks, self.sound_vol)
        if dlg.exec():
            self.sound_vol = dlg.value()

    def test_sound(self):
        self.clicks.test(self.sound_edit.text())

    def show_window(self):
        self.root.show()
        self.root.raise_()
        self.root.activateWindow()

    def pick(self, key):
        p, _ = QFileDialog.getOpenFileName(
            self.root, "Elegir imagen", "", "Imágenes (*.png *.jpg *.jpeg *.bmp *.gif *.ico *.webp)")
        if p:
            self.edits[key].setText(p)

    def on_size(self, _=None):
        self.size_lbl.setText(f"{self.slider.value()} px")
        if hasattr(self, "previews"):
            for k in self.previews:
                self.show_frame(k)

    def start_previews(self):
        self.load_previews()

    def stop_previews(self):
        self.load_timer.stop()
        for k in self.previews:
            self.timers[k].stop()
            self.frames[k] = ([], [])
            self.previews[k].clear()

    def load_previews(self):
        if not self.root.isVisible():
            return
        for k in self.previews:
            self.timers[k].stop()
            self.idx[k] = 0
            pixmaps, delays = [], []
            try:
                for img, ms in read_frames(self.edits[k].text()):
                    img.thumbnail((PREVIEW_SRC, PREVIEW_SRC))  # small source keeps memory/scaling cheap
                    w, h = img.size
                    qimg = QImage(img.tobytes(), w, h, w * 4, QImage.Format.Format_RGBA8888).copy()
                    pixmaps.append(QPixmap.fromImage(qimg))
                    delays.append(max(20, ms))
            except Exception:
                pixmaps, delays = [], []
            self.frames[k] = (pixmaps, delays)
            self.show_frame(k)
            if len(pixmaps) > 1:
                self.timers[k].start(delays[0])

    def show_frame(self, k):
        pixmaps, _ = self.frames[k]
        if not pixmaps:
            self.previews[k].clear()
            return
        size = self.slider.value()
        self.previews[k].setPixmap(pixmaps[self.idx[k]].scaled(
            size, size, Qt.AspectRatioMode.IgnoreAspectRatio, Qt.TransformationMode.SmoothTransformation))

    def next_frame(self, k):
        pixmaps, delays = self.frames[k]
        if not pixmaps:
            return
        self.idx[k] = (self.idx[k] + 1) % len(pixmaps)
        self.show_frame(k)
        self.timers[k].start(delays[self.idx[k]])

    def on_reset(self):
        restore_default()
        for e in self.edits.values():
            e.setText("")
        self.sound_edit.setText("")
        self.clicks.set_file("")
        self.slider.setValue(SIZE)
        self.sound_vol = 100
        self.clicks.configure(self.sound_vol)
        self.cfg = {"base": "", "click": "", "loading": "", "sound": "", "sound_volume": 100, "size": SIZE}
        save_config(self.cfg)

    def on_apply(self):
        self.cfg = {k: e.text() for k, e in self.edits.items()}
        self.cfg["sound"] = self.sound_edit.text()
        self.cfg["sound_volume"] = self.sound_vol
        self.cfg["size"] = clamp_size(self.slider.value())
        try:
            apply(self.cfg)
            self.clicks.set_file(self.cfg["sound"])
            save_config(self.cfg)
        except Exception as e:
            QMessageBox.critical(self.root, "Error", str(e))

    def quit(self):
        restore_default()
        self.clicks.set_file("")
        self.tray.hide()
        QApplication.quit()


if __name__ == "__main__":
    if sys.platform != "win32":
        sys.exit("Solo Windows")
    qapp = QApplication(sys.argv)
    qapp.setQuitOnLastWindowClosed(False)  # keep running in the tray
    window = App()
    sys.exit(qapp.exec())
