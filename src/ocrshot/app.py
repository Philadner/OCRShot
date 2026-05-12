from __future__ import annotations

import ctypes
import math
from ctypes import wintypes
import json
import os
import re
import sys
import threading
import time
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import numpy as np
from PIL import Image
from PySide6.QtCore import QEasingCurve, QObject, Property, QPropertyAnimation, QRect, QSize, QTimer, Qt, Signal
from PySide6.QtGui import QColor, QDoubleValidator, QImage, QPainter, QPalette, QPen, QPixmap
from PySide6.QtWidgets import (
    QApplication,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QStatusBar,
    QVBoxLayout,
    QWidget,
)

if sys.platform == "win32":
    import winreg

    import win32con
    import win32gui
    import win32process
    import win32ui
else:  # pragma: no cover - this app is intentionally Windows-only.
    win32con = win32gui = win32process = win32ui = None


APP_DIR = Path(os.environ.get("LOCALAPPDATA", Path.home())) / "Philadner" / "OCRShot"
SCREENSHOT_DIR = APP_DIR / "Screenshots"
CONFIG_PATH = APP_DIR / "config.json"
OCR_METADATA_VERSION = 1
OCR_TILE_GRID = 4
OCR_TILE_FINGERPRINT_SIZE = 16
OCR_TILE_PADDING = 32
OCR_TILE_CHANGE_THRESHOLD = 4.0
OCR_MAX_INCREMENTAL_TILES = 8

GA_ROOT = 2
PW_RENDERFULLCONTENT = 0x00000002
DWMWA_USE_IMMERSIVE_DARK_MODE = 20
DWMWA_CAPTION_COLOR = 35
DWMWA_TEXT_COLOR = 36
DWMWA_SYSTEMBACKDROP_TYPE = 38
DWMSBT_MAINWINDOW = 2
DWMWA_COLOR_DEFAULT = 0xFFFFFFFF
TBPF_NOPROGRESS = 0
TBPF_INDETERMINATE = 1


def colorref(color: QColor) -> int:
    return color.red() | (color.green() << 8) | (color.blue() << 16)


def get_windows_accent_color() -> QColor:
    registry_paths = (
        (winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\DWM", "AccentColor"),
        (winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\DWM", "ColorizationColor"),
        (winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Explorer\Accent", "AccentColorMenu"),
    )
    for root, path, name in registry_paths:
        try:
            with winreg.OpenKey(root, path) as key:
                value, _ = winreg.QueryValueEx(key, name)
                red = value & 0xFF
                green = (value >> 8) & 0xFF
                blue = (value >> 16) & 0xFF
                if max(red, green, blue) > 0:
                    return QColor(red, green, blue)
        except OSError:
            pass

    colorization = wintypes.DWORD()
    opaque = wintypes.BOOL()
    try:
        if ctypes.windll.dwmapi.DwmGetColorizationColor(ctypes.byref(colorization), ctypes.byref(opaque)) == 0:
            value = colorization.value
            return QColor((value >> 16) & 0xFF, (value >> 8) & 0xFF, value & 0xFF)
    except Exception:
        pass
    return QColor(0, 120, 212)


def set_dwm_int_attribute(hwnd: int, attribute: int, value: int) -> None:
    try:
        data = wintypes.DWORD(value)
        ctypes.windll.dwmapi.DwmSetWindowAttribute(hwnd, attribute, ctypes.byref(data), ctypes.sizeof(data))
    except Exception:
        pass


def set_mica(hwnd: int) -> None:
    set_dwm_int_attribute(hwnd, DWMWA_USE_IMMERSIVE_DARK_MODE, 1)
    set_dwm_int_attribute(hwnd, DWMWA_SYSTEMBACKDROP_TYPE, DWMSBT_MAINWINDOW)


def set_caption_color(hwnd: int, color: QColor | None) -> None:
    value = DWMWA_COLOR_DEFAULT if color is None else colorref(color)
    set_dwm_int_attribute(hwnd, DWMWA_CAPTION_COLOR, value)
    text_value = DWMWA_COLOR_DEFAULT if color is None else colorref(QColor(255, 255, 255))
    set_dwm_int_attribute(hwnd, DWMWA_TEXT_COLOR, text_value)


class TaskbarProgress:
    def __init__(self) -> None:
        self._taskbar: Any | None = None

    def _load(self) -> Any | None:
        if self._taskbar is not None:
            return self._taskbar
        try:
            import pythoncom

            taskbar = pythoncom.CoCreateInstance(
                "{56FDF344-FD6D-11d0-958A-006097C9A090}",
                None,
                pythoncom.CLSCTX_INPROC_SERVER,
                "{EA1AFB91-9E28-4B86-90E9-9E9F8A5EEA84}",
            )
            taskbar.HrInit()
        except Exception:
            return None
        self._taskbar = taskbar
        return self._taskbar

    def set_state(self, hwnd: int, state: int) -> None:
        taskbar = self._load()
        if taskbar is None:
            return
        try:
            taskbar.SetProgressState(hwnd, state)
        except Exception:
            pass


@dataclass
class AppConfig:
    monitor_word: str = ""
    selected_hwnd: int | None = None
    selected_title: str = ""
    selected_process_id: int | None = None
    selected_process_name: str = ""
    scan_interval_seconds: float = 3.0
    trigger_cooldown_seconds: float = 10.0
    match_case: bool = False


@dataclass
class TileOcrCache:
    image_size: tuple[int, int] | None = None
    fingerprints: list[np.ndarray] | None = None
    tile_texts: list[str] | None = None
    combined_text: str = ""
    tile_cache_valid: bool = False


def ensure_app_dirs() -> None:
    APP_DIR.mkdir(parents=True, exist_ok=True)
    SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)


def load_config() -> AppConfig:
    ensure_app_dirs()
    if not CONFIG_PATH.exists():
        return AppConfig()
    try:
        data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return AppConfig()

    valid_keys = set(AppConfig.__dataclass_fields__)
    filtered = {key: value for key, value in data.items() if key in valid_keys}
    return AppConfig(**filtered)


def save_config(config: AppConfig) -> None:
    ensure_app_dirs()
    CONFIG_PATH.write_text(json.dumps(asdict(config), indent=2), encoding="utf-8")


def get_window_title(hwnd: int) -> str:
    if not hwnd or not win32gui.IsWindow(hwnd):
        return ""
    return win32gui.GetWindowText(hwnd).strip()


def get_process_info(hwnd: int) -> tuple[int | None, str]:
    if not hwnd or not win32gui.IsWindow(hwnd):
        return None, ""
    _, pid = win32process.GetWindowThreadProcessId(hwnd)
    process_name = ""
    try:
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        handle = ctypes.windll.kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if handle:
            try:
                size = wintypes.DWORD(32768)
                buffer = ctypes.create_unicode_buffer(size.value)
                ok = ctypes.windll.kernel32.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(size))
                if ok:
                    process_name = Path(buffer.value).name
            finally:
                ctypes.windll.kernel32.CloseHandle(handle)
    except Exception:
        process_name = ""
    return pid, process_name


def is_selectable_window(hwnd: int) -> bool:
    if not hwnd or not win32gui.IsWindow(hwnd) or not win32gui.IsWindowVisible(hwnd):
        return False
    if win32gui.IsIconic(hwnd):
        return False
    title = get_window_title(hwnd)
    if not title:
        return False
    rect = win32gui.GetWindowRect(hwnd)
    return rect[2] - rect[0] > 20 and rect[3] - rect[1] > 20


def find_remembered_window(config: AppConfig) -> int | None:
    if config.selected_hwnd and is_selectable_window(config.selected_hwnd):
        return config.selected_hwnd

    candidates: list[int] = []

    def enum_callback(hwnd: int, _: Any) -> bool:
        if is_selectable_window(hwnd):
            title = get_window_title(hwnd)
            _, process_name = get_process_info(hwnd)
            same_title = bool(config.selected_title and title == config.selected_title)
            same_process = bool(config.selected_process_name and process_name == config.selected_process_name)
            if same_title and (same_process or not config.selected_process_name):
                candidates.append(hwnd)
        return True

    win32gui.EnumWindows(enum_callback, None)
    return candidates[0] if candidates else None


def window_from_current_cursor() -> int | None:
    point = win32gui.GetCursorPos()
    hwnd = win32gui.WindowFromPoint(point)
    if not hwnd:
        return None
    root = win32gui.GetAncestor(hwnd, GA_ROOT)
    return root if is_selectable_window(root) else None


def top_level_window_under_cursor(excluded_hwnds: set[int] | None = None) -> int | None:
    excluded_hwnds = excluded_hwnds or set()
    cursor_x, cursor_y = win32gui.GetCursorPos()
    current_pid = os.getpid()
    found: list[int] = []

    def enum_callback(hwnd: int, _: Any) -> bool:
        if hwnd in excluded_hwnds or not is_selectable_window(hwnd):
            return True

        _, pid = win32process.GetWindowThreadProcessId(hwnd)
        if pid == current_pid:
            return True

        left, top, right, bottom = win32gui.GetWindowRect(hwnd)
        if left <= cursor_x <= right and top <= cursor_y <= bottom:
            found.append(hwnd)
            return False
        return True

    win32gui.EnumWindows(enum_callback, None)
    return found[0] if found else None


def capture_window(hwnd: int) -> Image.Image:
    if not is_selectable_window(hwnd):
        raise RuntimeError("The selected window is no longer available.")

    left, top, right, bottom = win32gui.GetWindowRect(hwnd)
    width = right - left
    height = bottom - top
    if width <= 0 or height <= 0:
        raise RuntimeError("The selected window has an invalid size.")

    window_dc = win32gui.GetWindowDC(hwnd)
    source_dc = win32ui.CreateDCFromHandle(window_dc)
    memory_dc = source_dc.CreateCompatibleDC()
    bitmap = win32ui.CreateBitmap()
    bitmap.CreateCompatibleBitmap(source_dc, width, height)
    memory_dc.SelectObject(bitmap)

    try:
        result = ctypes.windll.user32.PrintWindow(hwnd, memory_dc.GetSafeHdc(), PW_RENDERFULLCONTENT)
        if result != 1:
            raise RuntimeError("Windows could not capture that window with PrintWindow.")

        bitmap_info = bitmap.GetInfo()
        bitmap_bits = bitmap.GetBitmapBits(True)
        image = Image.frombuffer(
            "RGB",
            (bitmap_info["bmWidth"], bitmap_info["bmHeight"]),
            bitmap_bits,
            "raw",
            "BGRX",
            0,
            1,
        )
        return image.copy()
    finally:
        win32gui.DeleteObject(bitmap.GetHandle())
        memory_dc.DeleteDC()
        source_dc.DeleteDC()
        win32gui.ReleaseDC(hwnd, window_dc)


def tile_boxes(size: tuple[int, int], grid: int = OCR_TILE_GRID) -> list[tuple[int, int, int, int]]:
    width, height = size
    boxes: list[tuple[int, int, int, int]] = []
    for row in range(grid):
        top = round(row * height / grid)
        bottom = round((row + 1) * height / grid)
        for col in range(grid):
            left = round(col * width / grid)
            right = round((col + 1) * width / grid)
            boxes.append((left, top, right, bottom))
    return boxes


def padded_box(box: tuple[int, int, int, int], size: tuple[int, int], padding: int = OCR_TILE_PADDING) -> tuple[int, int, int, int]:
    width, height = size
    left, top, right, bottom = box
    return (
        max(0, left - padding),
        max(0, top - padding),
        min(width, right + padding),
        min(height, bottom + padding),
    )


def tile_fingerprint(image: Image.Image, box: tuple[int, int, int, int]) -> np.ndarray:
    crop = image.crop(box).convert("L").resize(
        (OCR_TILE_FINGERPRINT_SIZE, OCR_TILE_FINGERPRINT_SIZE),
        Image.Resampling.BILINEAR,
    )
    return np.asarray(crop, dtype=np.int16)


def tile_difference(previous: np.ndarray, current: np.ndarray) -> float:
    return float(np.mean(np.abs(current - previous)))


class OcrEngine:
    def __init__(self) -> None:
        self._engine: Any | None = None
        self.provider_label = "unloaded"

    def _load(self) -> Any:
        if self._engine is not None:
            return self._engine
        try:
            from rapidocr_onnxruntime import RapidOCR
        except ImportError as exc:
            raise RuntimeError("rapidocr-onnxruntime is not installed. Run `uv sync`.") from exc

        use_cuda = False
        try:
            import onnxruntime as ort

            use_cuda = "CUDAExecutionProvider" in ort.get_available_providers()
        except Exception:
            use_cuda = False

        if use_cuda and self.has_cuda_runtime_dlls():
            try:
                self._engine = RapidOCR(
                    det_use_cuda=True,
                    cls_use_cuda=True,
                    rec_use_cuda=True,
                    print_verbose=False,
                )
                self.provider_label = self.active_provider_label(self._engine)
                return self._engine
            except Exception:
                self._engine = None

        self._engine = RapidOCR(print_verbose=False)
        self.provider_label = self.active_provider_label(self._engine)
        return self._engine

    def has_cuda_runtime_dlls(self) -> bool:
        required = ("cublasLt64_12.dll", "cudnn64_9.dll")
        path_dirs = [Path(path) for path in os.environ.get("PATH", "").split(os.pathsep) if path]
        return all(any((path_dir / dll).exists() for path_dir in path_dirs) for dll in required)

    def active_provider_label(self, engine: Any) -> str:
        try:
            providers = engine.text_rec.infer.session.get_providers()
        except Exception:
            return "CPU"
        if providers and providers[0] == "CUDAExecutionProvider":
            return "CUDA"
        if providers:
            return providers[0].replace("ExecutionProvider", "")
        return "CPU"

    def read_text(self, image: Image.Image) -> str:
        engine = self._load()
        image_array = np.array(image.convert("RGB"))
        raw_result = engine(image_array)
        return "\n".join(extract_text_fragments(raw_result))


def extract_text_fragments(raw_result: Any) -> list[str]:
    if raw_result is None:
        return []
    if isinstance(raw_result, tuple):
        raw_result = raw_result[0]
    if hasattr(raw_result, "txts"):
        return [str(text) for text in raw_result.txts if text]
    if hasattr(raw_result, "texts"):
        return [str(text) for text in raw_result.texts if text]

    fragments: list[str] = []
    if isinstance(raw_result, list):
        for item in raw_result:
            if isinstance(item, (list, tuple)) and len(item) >= 2 and isinstance(item[1], str):
                fragments.append(item[1])
            elif isinstance(item, dict):
                text = item.get("text") or item.get("txt")
                if text:
                    fragments.append(str(text))
    return fragments


def contains_word(text: str, word: str, match_case: bool) -> bool:
    if not word:
        return False
    flags = 0 if match_case else re.IGNORECASE
    pattern = rf"\b{re.escape(word)}\b"
    return re.search(pattern, text, flags) is not None


def safe_filename_piece(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", value.strip())
    return cleaned.strip("._")[:48] or "window"


def ocr_metadata_path(image_path: Path) -> Path:
    return image_path.with_suffix(image_path.suffix + ".ocr.json")


def write_ocr_metadata(image_path: Path, text: str, window_title: str = "") -> None:
    metadata = {
        "version": OCR_METADATA_VERSION,
        "image": image_path.name,
        "window_title": window_title,
        "ocr_text": text,
        "indexed_at": datetime.now().isoformat(timespec="seconds"),
    }
    ocr_metadata_path(image_path).write_text(json.dumps(metadata, indent=2), encoding="utf-8")


def read_ocr_metadata(image_path: Path) -> dict[str, Any]:
    path = ocr_metadata_path(image_path)
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def read_ocr_text(image_path: Path) -> str:
    data = read_ocr_metadata(image_path)
    text = data.get("ocr_text", "")
    return text if isinstance(text, str) else ""


def screenshot_has_ocr_data(image_path: Path) -> bool:
    return "ocr_text" in read_ocr_metadata(image_path)


class MonitorBridge(QObject):
    completed = Signal(str, str, str)
    failed = Signal(str)
    preview_ready = Signal(object)
    ocr_index_updated = Signal(str)


class SelectionOverlay(QWidget):
    selected = Signal(int)
    canceled = Signal()

    def __init__(self) -> None:
        super().__init__()
        self._highlight_rect = QRect()
        self._target_hwnd: int | None = None
        self._mouse_was_down = False
        self._animation = QPropertyAnimation(self, b"highlightRect", self)
        self._animation.setDuration(180)
        self._animation.setEasingCurve(QEasingCurve.OutCubic)

        flags = (
            Qt.Tool
            | Qt.FramelessWindowHint
            | Qt.WindowStaysOnTopHint
            | Qt.WindowDoesNotAcceptFocus
        )
        self.setWindowFlags(flags)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_ShowWithoutActivating)

        self.timer = QTimer(self)
        self.timer.setInterval(16)
        self.timer.timeout.connect(self.update_target)
        self.setMouseTracking(True)
        self.setCursor(Qt.CrossCursor)

    def get_highlight_rect(self) -> QRect:
        return QRect(self._highlight_rect)

    def set_highlight_rect(self, rect: QRect) -> None:
        self._highlight_rect = QRect(rect)
        self.update()

    highlightRect = Property(QRect, get_highlight_rect, set_highlight_rect)

    def start(self) -> None:
        self.setGeometry(self.virtual_desktop_geometry())
        self._highlight_rect = QRect()
        self._target_hwnd = None
        self._mouse_was_down = bool(ctypes.windll.user32.GetAsyncKeyState(win32con.VK_LBUTTON) & 0x8000)
        self.show()
        self.raise_()
        self.timer.start()

    def stop(self) -> None:
        self.timer.stop()
        self.hide()

    def virtual_desktop_geometry(self) -> QRect:
        return virtual_desktop_geometry()

    def update_target(self) -> None:
        if ctypes.windll.user32.GetAsyncKeyState(win32con.VK_ESCAPE) & 0x8000:
            self.stop()
            self.canceled.emit()
            return

        hwnd = top_level_window_under_cursor({int(self.winId())})
        if hwnd and hwnd != self._target_hwnd:
            self._target_hwnd = hwnd
            self.animate_to_window(hwnd)

        mouse_down = bool(ctypes.windll.user32.GetAsyncKeyState(win32con.VK_LBUTTON) & 0x8000)
        if mouse_down and not self._mouse_was_down:
            selected_hwnd = self._target_hwnd or top_level_window_under_cursor({int(self.winId())})
            if selected_hwnd:
                self.stop()
                self.selected.emit(selected_hwnd)
                return
        self._mouse_was_down = mouse_down

    def mousePressEvent(self, event: Any) -> None:
        if event.button() == Qt.LeftButton and self._target_hwnd:
            selected_hwnd = self._target_hwnd
            self.stop()
            self.selected.emit(selected_hwnd)
            return
        super().mousePressEvent(event)

    def animate_to_window(self, hwnd: int) -> None:
        left, top, right, bottom = win32gui.GetWindowRect(hwnd)
        origin = self.geometry().topLeft()
        padding = 7
        target = QRect(
            left - origin.x() - padding,
            top - origin.y() - padding,
            right - left + padding * 2,
            bottom - top + padding * 2,
        )
        if self._highlight_rect.isNull():
            self._highlight_rect = target
            self.update()
            return

        self._animation.stop()
        self._animation.setStartValue(self._highlight_rect)
        self._animation.setEndValue(target)
        self._animation.start()

    def paintEvent(self, event: Any) -> None:
        if self._highlight_rect.isNull():
            return

        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)

        rect = self._highlight_rect.adjusted(3, 3, -3, -3)
        radius = 13

        for width, alpha in ((14, 26), (8, 42), (4, 64)):
            pen = QPen(QColor(54, 153, 255, alpha), width)
            painter.setPen(pen)
            painter.setBrush(Qt.NoBrush)
            painter.drawRoundedRect(rect, radius, radius)

        painter.setPen(QPen(QColor(255, 255, 255, 235), 2))
        painter.drawRoundedRect(rect, radius, radius)

        painter.setPen(QPen(QColor(32, 127, 255, 245), 2))
        painter.drawRoundedRect(rect.adjusted(2, 2, -2, -2), radius - 2, radius - 2)


def image_to_pixmap(image: Image.Image) -> QPixmap:
    rgb = image.convert("RGBA")
    data = rgb.tobytes("raw", "RGBA")
    qimage = QImage(data, rgb.width, rgb.height, QImage.Format_RGBA8888)
    return QPixmap.fromImage(qimage.copy())


def virtual_desktop_geometry() -> QRect:
    screens = QApplication.screens()
    if not screens:
        return QRect(0, 0, 1, 1)
    geometry = QRect(screens[0].geometry())
    for screen in screens[1:]:
        geometry = geometry.united(screen.geometry())
    return geometry


class SquircleFrame(QFrame):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("squircleFrame")


class LabeledLineEdit(SquircleFrame):
    textChanged = Signal(str)

    def __init__(self, label: str, placeholder: str = "") -> None:
        super().__init__()
        self.label = QLabel(label)
        self.input = QLineEdit()
        self.input.setPlaceholderText(placeholder)
        self.input.textChanged.connect(self.textChanged.emit)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 7, 12, 8)
        layout.setSpacing(4)
        layout.addWidget(self.label)
        layout.addWidget(self.input)

    def text(self) -> str:
        return self.input.text()

    def setText(self, text: str) -> None:
        self.input.setText(text)


class ToggleSwitch(QWidget):
    toggled = Signal(bool)

    def __init__(self, label: str, checked: bool = False) -> None:
        super().__init__()
        self._checked = checked
        self._knob_position = 1.0 if checked else 0.0
        self.label = QLabel(label)
        self.track = QWidget()
        self.track.setFixedSize(38, 22)
        self.track.paintEvent = self.paint_track

        self.animation = QPropertyAnimation(self, b"knobPosition", self)
        self.animation.setDuration(150)
        self.animation.setEasingCurve(QEasingCurve.OutCubic)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.label)
        layout.addStretch(1)
        layout.addWidget(self.track)
        self.setCursor(Qt.PointingHandCursor)

    def get_knob_position(self) -> float:
        return self._knob_position

    def set_knob_position(self, value: float) -> None:
        self._knob_position = value
        self.track.update()

    knobPosition = Property(float, get_knob_position, set_knob_position)

    def isChecked(self) -> bool:
        return self._checked

    def setChecked(self, checked: bool) -> None:
        if self._checked == checked:
            return
        self._checked = checked
        self.animation.stop()
        self.animation.setStartValue(self._knob_position)
        self.animation.setEndValue(1.0 if checked else 0.0)
        self.animation.start()
        self.toggled.emit(checked)

    def mousePressEvent(self, event: Any) -> None:
        if event.button() == Qt.LeftButton:
            self.setChecked(not self._checked)
            return
        super().mousePressEvent(event)

    def paint_track(self, event: Any) -> None:
        painter = QPainter(self.track)
        painter.setRenderHint(QPainter.Antialiasing)
        accent = self.palette().highlight().color()
        off = QColor(180, 187, 197)
        color = QColor(
            round(off.red() + (accent.red() - off.red()) * self._knob_position),
            round(off.green() + (accent.green() - off.green()) * self._knob_position),
            round(off.blue() + (accent.blue() - off.blue()) * self._knob_position),
        )
        painter.setPen(Qt.NoPen)
        painter.setBrush(color)
        painter.drawRoundedRect(0, 0, 38, 22, 11, 11)
        knob_x = 3 + round(16 * self._knob_position)
        painter.setBrush(QColor(255, 255, 255))
        painter.drawEllipse(knob_x, 3, 16, 16)


class ScrubHandle(QWidget):
    pixelsMoved = Signal(float)

    def __init__(self) -> None:
        super().__init__()
        self.setFixedSize(52, 24)
        self.setCursor(Qt.SizeHorCursor)
        self._dragging = False
        self._last_global_x = 0
        self._ignore_next_move = False
        self._value = 0.0
        self._poll_timer = QTimer(self)
        self._poll_timer.setInterval(8)
        self._poll_timer.timeout.connect(self.poll_cursor)

    def mousePressEvent(self, event: Any) -> None:
        if event.button() != Qt.LeftButton:
            return
        self._dragging = True
        self._last_global_x = round(event.globalPosition().x())
        self._poll_timer.start()
        self.update()
        self.grabMouse()

    def mouseReleaseEvent(self, event: Any) -> None:
        if self._dragging:
            self._dragging = False
            self._poll_timer.stop()
            self.update()
            self.releaseMouse()

    def mouseMoveEvent(self, event: Any) -> None:
        self.poll_cursor()

    def poll_cursor(self) -> None:
        if not self._dragging:
            return

        x, y = win32gui.GetCursorPos()
        if self._ignore_next_move:
            self._ignore_next_move = False
            self._last_global_x = x
            return

        dx = x - self._last_global_x
        self._last_global_x = x
        if dx:
            self.update()
            self.pixelsMoved.emit(float(dx))

        desktop = virtual_desktop_geometry()
        left = desktop.left()
        right = desktop.right()
        if x <= left + 2:
            ctypes.windll.user32.SetCursorPos(right - 3, y)
            self._last_global_x = right - 3
            self._ignore_next_move = True
        elif x >= right - 2:
            ctypes.windll.user32.SetCursorPos(left + 3, y)
            self._last_global_x = left + 3
            self._ignore_next_move = True

    def setValue(self, value: float) -> None:
        self._value = value
        self.update()

    def paintEvent(self, event: Any) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        rect = self.rect().adjusted(1, 3, -1, -3)
        painter.setPen(QPen(QColor(96, 96, 96), 1))
        painter.setBrush(QColor(45, 45, 45))
        painter.drawRoundedRect(rect, 5, 5)

        center = rect.center().x()
        highlight = self.palette().highlight().color()
        minor_spacing = 7
        minor_height = 8
        major_height = 16
        baseline = rect.center().y()
        tick_value = 0.2
        left_value = self._value - ((center + 20) / minor_spacing) * tick_value
        first_tick_number = math.floor(left_value / tick_value) - 1

        painter.setClipRect(rect.adjusted(2, 2, -2, -2))
        tick_number = first_tick_number
        while True:
            tick = round(tick_number * tick_value, 10)
            x = center + ((tick - self._value) / tick_value) * minor_spacing
            if x > self.width() + 20:
                break
            if x < -20:
                tick_number += 1
                continue
            is_major = tick_number % 5 == 0
            if is_major:
                painter.setPen(QPen(highlight, 2))
                height = major_height
            else:
                painter.setPen(QPen(QColor(150, 150, 150), 1))
                height = minor_height
            painter.drawLine(round(x), baseline - height // 2, round(x), baseline + height // 2)
            tick_number += 1

        painter.setClipping(False)
        painter.setPen(QPen(QColor(225, 225, 225), 1))
        painter.drawLine(center, rect.top() + 3, center, rect.bottom() - 3)


class IntervalControl(SquircleFrame):
    valueChanged = Signal(float)

    def __init__(self, label: str, minimum: float, maximum: float, value: float) -> None:
        super().__init__()
        self.label = QLabel(label)
        self.value_input = QLineEdit()
        self.value_input.setObjectName("scrubValue")
        self.value_input.setFixedWidth(70)
        self.value_input.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        self.value_input.setValidator(QDoubleValidator(minimum, maximum, 1, self))
        self.scrub = ScrubHandle()
        self.minimum = minimum
        self.maximum = maximum
        self._value = value
        self._visual_value = value
        self._set_input_text(value)
        self.scrub.setValue(value)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(12, 6, 12, 6)
        layout.setSpacing(8)
        layout.addWidget(self.label, 1)
        layout.addWidget(self.value_input)
        layout.addWidget(self.scrub)

        self.value_input.editingFinished.connect(self.commit_input)
        self.scrub.pixelsMoved.connect(self.scrub_pixels)

    def _set_input_text(self, value: float) -> None:
        self.value_input.setText(f"{value:.1f}")

    def value(self) -> float:
        return self._value

    def setValue(self, value: float, update_scrub: bool = True) -> None:
        value = max(self.minimum, min(self.maximum, value))
        value = round(value * 5) / 5
        if abs(value - self._value) < 0.001:
            if update_scrub:
                self._visual_value = value
                self.scrub.setValue(value)
            return
        self._value = value
        if update_scrub:
            self._visual_value = value
            self.scrub.setValue(value)
        self._set_input_text(value)
        self.valueChanged.emit(value)

    def scrub_pixels(self, pixels: float) -> None:
        pixels_per_step = 6.0
        self._visual_value = max(self.minimum, min(self.maximum, self._visual_value - (pixels / pixels_per_step) * 0.2))
        self.scrub.setValue(self._visual_value)
        rounded_value = round(self._visual_value * 5) / 5
        self.setValue(rounded_value, update_scrub=False)

    def commit_input(self) -> None:
        raw = self.value_input.text().lower().replace("s", "").strip()
        try:
            value = float(raw)
        except ValueError:
            self._set_input_text(self._value)
            return
        self.setValue(value)


class WindowPreviewPanel(SquircleFrame):
    toggled = Signal(bool)
    resized = Signal()

    def __init__(self, select_button: QPushButton) -> None:
        super().__init__()
        self._expanded = False
        self._preview_pixmap: QPixmap | None = None
        self.header = QPushButton()
        self.header.setObjectName("windowPill")
        self.header.clicked.connect(self.toggle)
        self.select_button = select_button

        self.preview = QLabel("No preview yet")
        self.preview.setAlignment(Qt.AlignCenter)
        self.preview.setMinimumHeight(0)
        self.preview.setObjectName("previewPane")

        self.animation = QPropertyAnimation(self.preview, b"maximumHeight", self)
        self.animation.setDuration(220)
        self.animation.setEasingCurve(QEasingCurve.OutCubic)
        self.animation.valueChanged.connect(lambda _value: self.resized.emit())
        self.animation.finished.connect(self.resized.emit)
        self.preview.setMaximumHeight(0)

        header_row = QHBoxLayout()
        header_row.setContentsMargins(0, 0, 0, 0)
        header_row.setSpacing(8)
        header_row.addWidget(self.header, 1)
        header_row.addWidget(self.select_button)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(6, 6, 6, 6)
        layout.setSpacing(6)
        layout.addLayout(header_row)
        layout.addWidget(self.preview)
        self.set_window_text("No window selected")

    def set_window_text(self, text: str) -> None:
        arrow = "^" if self._expanded else "<"
        self.header.setText(f"{text}        {arrow}")

    def is_expanded(self) -> bool:
        return self._expanded

    def set_preview(self, pixmap: QPixmap) -> None:
        self._preview_pixmap = pixmap
        self.update_preview_pixmap()

    def resizeEvent(self, event: Any) -> None:
        super().resizeEvent(event)
        self.update_preview_pixmap()

    def update_preview_pixmap(self) -> None:
        if self._preview_pixmap is None:
            return
        scaled = self._preview_pixmap.scaled(
            QSize(max(1, self.preview.width() - 16), 220),
            Qt.KeepAspectRatio,
            Qt.SmoothTransformation,
        )
        self.preview.setPixmap(scaled)

    def toggle(self) -> None:
        self._expanded = not self._expanded
        text = self.header.text().rsplit("        ", 1)[0]
        self.set_window_text(text)
        self.animation.stop()
        self.animation.setStartValue(self.preview.maximumHeight())
        self.animation.setEndValue(238 if self._expanded else 0)
        self.animation.start()
        self.toggled.emit(self._expanded)


class HistoryPanel(SquircleFrame):
    previousClicked = Signal()
    nextClicked = Signal()
    deleteClicked = Signal()
    searchChanged = Signal(str)

    def __init__(self) -> None:
        super().__init__()
        self.setObjectName("historyPanel")
        self._pixmap: QPixmap | None = None
        self.title = QLabel("Screenshot history")
        self.title.setObjectName("historyTitle")
        self.search_input = QLineEdit()
        self.search_input.setPlaceholderText("Search OCR text")

        self.image = QLabel("No screenshots yet")
        self.image.setObjectName("historyImage")
        self.image.setAlignment(Qt.AlignCenter)
        self.image.setMinimumSize(420, 260)

        self.counter = QLabel("0 / 0")
        self.counter.setAlignment(Qt.AlignCenter)

        self.previous_button = QPushButton("Previous")
        self.previous_button.setObjectName("softButton")
        self.next_button = QPushButton("Next")
        self.next_button.setObjectName("softButton")
        self.delete_button = QPushButton("Delete")
        self.delete_button.setObjectName("dangerButton")

        nav = QHBoxLayout()
        nav.setSpacing(8)
        nav.addWidget(self.previous_button)
        nav.addWidget(self.counter, 1)
        nav.addWidget(self.next_button)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(8)
        layout.addWidget(self.title)
        layout.addWidget(self.search_input)
        layout.addWidget(self.image, 1)
        layout.addLayout(nav)
        layout.addWidget(self.delete_button)

        self.previous_button.clicked.connect(self.previousClicked.emit)
        self.next_button.clicked.connect(self.nextClicked.emit)
        self.delete_button.clicked.connect(self.deleteClicked.emit)
        self.search_input.textChanged.connect(self.searchChanged.emit)

    def set_history_state(self, current: int, total: int, path: Path | None, empty_message: str = "No screenshots yet") -> None:
        self.counter.setText(f"{current} / {total}")
        self.previous_button.setEnabled(total > 1)
        self.next_button.setEnabled(total > 1)
        self.delete_button.setEnabled(path is not None)
        if path is None:
            self._pixmap = None
            self.image.setPixmap(QPixmap())
            self.image.setText(empty_message)
            return

        pixmap = QPixmap(str(path))
        self._pixmap = pixmap if not pixmap.isNull() else None
        if self._pixmap is None:
            self.image.setPixmap(QPixmap())
            self.image.setText("Could not load screenshot")
            return
        self.image.setText("")
        self.update_pixmap()

    def resizeEvent(self, event: Any) -> None:
        super().resizeEvent(event)
        self.update_pixmap()

    def update_pixmap(self) -> None:
        if self._pixmap is None:
            return
        scaled = self._pixmap.scaled(
            QSize(max(1, self.image.width() - 12), max(1, self.image.height() - 12)),
            Qt.KeepAspectRatio,
            Qt.SmoothTransformation,
        )
        self.image.setPixmap(scaled)


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.config = load_config()
        self.ocr = OcrEngine()
        self.bridge = MonitorBridge()
        self.selected_hwnd = find_remembered_window(self.config)
        self.monitoring = False
        self.scan_busy = False
        self.last_trigger_at = 0.0
        self.last_had_match = False
        self.tile_ocr_cache = TileOcrCache()
        self.selection_overlay = SelectionOverlay()
        self.taskbar = TaskbarProgress()
        self.accent_color = get_windows_accent_color()
        self.history_open = False
        self.history_files: list[Path] = []
        self.history_index = 0
        self.history_search_query = ""
        self.ocr_backfill_busy = False
        self.ocr_lock = threading.Lock()
        self.window_geometry_animation: QPropertyAnimation | None = None
        self.compact_geometry: QRect | None = None
        self.compact_left_width = 460
        self.history_hide_connected = False
        self.monitoring_started_at = 0.0
        self.caption_timer = QTimer(self)
        self.caption_timer.setInterval(33)
        self.caption_timer.timeout.connect(self.update_monitoring_caption)

        self.setWindowTitle("OCRShot")
        self.setMinimumWidth(460)
        self.apply_theme()

        self.select_button = QPushButton("Select Window")
        self.select_button.setObjectName("accentButton")
        self.window_panel = WindowPreviewPanel(self.select_button)
        self.word_field = LabeledLineEdit("Monitor word", "Word to watch for")
        self.word_field.setText(self.config.monitor_word)

        self.start_button = QPushButton("Start Monitoring")
        self.capture_button = QPushButton("Test Capture")
        self.history_button = QPushButton("History")
        self.start_button.setObjectName("accentButton")
        self.capture_button.setObjectName("softButton")
        self.history_button.setObjectName("softButton")

        self.match_case_box = SquircleFrame()
        self.match_case_switch = ToggleSwitch("Match case", self.config.match_case)
        match_layout = QVBoxLayout(self.match_case_box)
        match_layout.setContentsMargins(12, 7, 12, 7)
        match_layout.addWidget(self.match_case_switch)

        self.interval_input = IntervalControl("Scan interval", 0.2, 60.0, float(self.config.scan_interval_seconds))
        self.cooldown_input = IntervalControl(
            "Screenshot cooldown",
            0.2,
            600.0,
            float(self.config.trigger_cooldown_seconds),
        )

        controls = QVBoxLayout()
        controls.setSpacing(7)
        controls.addWidget(self.window_panel)
        controls.addWidget(self.word_field)
        controls.addWidget(self.interval_input)
        controls.addWidget(self.cooldown_input)
        controls.addWidget(self.match_case_box)

        button_row = QHBoxLayout()
        button_row.setSpacing(8)
        button_row.addWidget(self.capture_button)
        button_row.addWidget(self.history_button)
        button_row.addStretch(1)
        button_row.addWidget(self.start_button)

        left_root = QVBoxLayout()
        left_root.setContentsMargins(12, 10, 12, 8)
        left_root.setSpacing(8)
        left_root.addLayout(controls)
        left_root.addLayout(button_row)

        self.left_pane = QWidget()
        self.left_pane.setLayout(left_root)
        self.left_pane.setMinimumWidth(460)
        self.history_panel = HistoryPanel()
        self.history_panel.setMaximumWidth(0)
        self.history_panel.hide()

        self.history_panel_animation = QPropertyAnimation(self.history_panel, b"maximumWidth", self)
        self.history_panel_animation.setDuration(260)
        self.history_panel_animation.setEasingCurve(QEasingCurve.OutCubic)

        root = QHBoxLayout()
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        root.addWidget(self.left_pane)
        root.addWidget(self.history_panel)

        container = QWidget()
        container.setLayout(root)
        self.setCentralWidget(container)
        self.setStatusBar(QStatusBar())

        self.scan_timer = QTimer(self)
        self.scan_timer.timeout.connect(self.start_scan)

        self.select_button.clicked.connect(self.begin_window_selection)
        self.start_button.clicked.connect(self.toggle_monitoring)
        self.capture_button.clicked.connect(self.save_manual_capture)
        self.history_button.clicked.connect(self.toggle_history)
        self.history_panel.previousClicked.connect(self.show_previous_history)
        self.history_panel.nextClicked.connect(self.show_next_history)
        self.history_panel.deleteClicked.connect(self.delete_current_history)
        self.history_panel.searchChanged.connect(self.search_history)
        self.word_field.textChanged.connect(lambda _text: self.persist_config())
        self.interval_input.valueChanged.connect(lambda _value: self.persist_config())
        self.cooldown_input.valueChanged.connect(lambda _value: self.persist_config())
        self.match_case_switch.toggled.connect(lambda _checked: self.persist_config())
        self.bridge.completed.connect(self.handle_scan_completed)
        self.bridge.failed.connect(self.handle_scan_failed)
        self.bridge.preview_ready.connect(self.update_preview_from_image)
        self.bridge.ocr_index_updated.connect(self.handle_ocr_index_updated)
        self.selection_overlay.selected.connect(self.complete_window_selection)
        self.selection_overlay.canceled.connect(self.cancel_window_selection)
        self.window_panel.resized.connect(self.resize_to_content)
        self.window_panel.toggled.connect(lambda _expanded: self.resize_to_content())

        self.refresh_window_label()
        self.statusBar().showMessage(f"Screenshots: {SCREENSHOT_DIR}")
        QTimer.singleShot(0, self.resize_to_content)
        QTimer.singleShot(800, self.start_ocr_backfill)

    def apply_theme(self) -> None:
        accent = self.accent_color
        accent_dark = accent.darker(135)
        self.setStyleSheet(
            f"""
            QMainWindow {{
                background: #202020;
            }}
            QLabel {{
                color: #f1f1f1;
                font-size: 13px;
            }}
            QLabel#previewPane {{
                background: #181818;
                border: 1px solid #3a3a3a;
                border-radius: 8px;
                color: #a7a7a7;
            }}
            QFrame#squircleFrame {{
                background: #272727;
                border: 1px solid #3a3a3a;
                border-radius: 9px;
            }}
            QFrame#historyPanel {{
                background: #242424;
                border-left: 1px solid #3a3a3a;
                border-radius: 0px;
            }}
            QLineEdit {{
                background: #1e1e1e;
                border: 1px solid #464646;
                border-radius: 6px;
                padding: 5px 8px;
                color: #f3f3f3;
                selection-background-color: {accent.name()};
            }}
            QLineEdit:focus {{
                border: 2px solid {accent.name()};
                background: #1b1b1b;
                padding: 4px 7px;
            }}
            QLineEdit#scrubValue {{
                font-family: "Cascadia Mono", "Consolas";
                background: transparent;
                border: 1px solid transparent;
                padding: 4px 2px;
                color: #f5f5f5;
            }}
            QLineEdit#scrubValue:focus {{
                background: #1b1b1b;
                border: 1px solid {accent.name()};
                padding: 4px 2px;
            }}
            QPushButton {{
                border-radius: 6px;
                padding: 6px 12px;
                font-weight: 600;
            }}
            QPushButton#accentButton {{
                color: white;
                background: {accent.name()};
                border: 1px solid {accent.lighter(130).name()};
                border-bottom: 2px solid {accent_dark.name()};
            }}
            QPushButton#accentButton:hover {{
                background: {accent.lighter(112).name()};
            }}
            QPushButton#accentButton:pressed {{
                background: {accent_dark.name()};
                border-bottom: 1px solid {accent_dark.darker(115).name()};
                padding-top: 7px;
                padding-bottom: 5px;
            }}
            QPushButton#softButton {{
                color: #f1f1f1;
                background: #303030;
                border: 1px solid #4a4a4a;
                border-bottom: 2px solid #1c1c1c;
            }}
            QPushButton#softButton:hover {{
                background: #3a3a3a;
            }}
            QPushButton#dangerButton {{
                color: white;
                background: #b3261e;
                border: 1px solid #e46962;
                border-bottom: 2px solid #6f1712;
            }}
            QPushButton#dangerButton:hover {{
                background: #c9362e;
            }}
            QPushButton#dangerButton:pressed {{
                background: #7f1b15;
                border-bottom: 1px solid #5c120e;
                padding-top: 7px;
                padding-bottom: 5px;
            }}
            QPushButton#windowPill {{
                text-align: left;
                color: #f3f3f3;
                background: #2d2d2d;
                border: 1px solid #494949;
                border-radius: 8px;
                padding: 8px 10px;
                font-weight: 650;
            }}
            QPushButton#windowPill:hover {{
                background: #343434;
            }}
            QLabel#historyTitle {{
                font-size: 14px;
                font-weight: 650;
            }}
            QLabel#historyImage {{
                background: #141414;
                border: 1px solid #3a3a3a;
                border-radius: 8px;
                color: #9a9a9a;
            }}
            QStatusBar {{
                background: transparent;
                color: #a7a7a7;
            }}
            """
        )
        palette = self.palette()
        palette.setColor(self.backgroundRole(), QColor(32, 32, 32))
        palette.setColor(QPalette.Highlight, accent)
        self.setPalette(palette)

    def resize_to_content(self) -> None:
        if self.history_open:
            self.resize_for_history()
            return
        width = max(self.minimumWidth(), self.sizeHint().width())
        self.adjustSize()
        self.resize(width, self.sizeHint().height())

    def screenshot_history_files(self) -> list[Path]:
        ensure_app_dirs()
        return sorted(SCREENSHOT_DIR.glob("*.png"), key=lambda path: path.stat().st_mtime, reverse=True)

    def refresh_history(self, preferred_path: Path | None = None) -> None:
        files = self.screenshot_history_files()
        query = self.history_search_query.strip().lower()
        if query:
            terms = [term for term in query.split() if term]
            files = [
                path
                for path in files
                if all(term in read_ocr_text(path).lower() for term in terms)
            ]
        self.history_files = files
        if preferred_path and preferred_path in self.history_files:
            self.history_index = self.history_files.index(preferred_path)
        elif self.history_files:
            self.history_index = max(0, min(self.history_index, len(self.history_files) - 1))
        else:
            self.history_index = 0
        self.update_history_view()

    def update_history_view(self) -> None:
        total = len(self.history_files)
        if not total:
            message = "No OCR matches" if self.history_search_query.strip() else "No screenshots yet"
            self.history_panel.set_history_state(0, 0, None, message)
            return
        current_path = self.history_files[self.history_index]
        self.history_panel.set_history_state(self.history_index + 1, total, current_path)

    def search_history(self, query: str) -> None:
        self.history_search_query = query
        self.history_index = 0
        self.refresh_history()

    def screenshots_missing_ocr_data(self) -> list[Path]:
        return [path for path in self.screenshot_history_files() if not screenshot_has_ocr_data(path)]

    def start_ocr_backfill(self) -> None:
        if self.ocr_backfill_busy:
            return
        missing = self.screenshots_missing_ocr_data()
        if not missing:
            return
        self.ocr_backfill_busy = True
        threading.Thread(target=self.ocr_backfill_worker, args=(missing,), daemon=True).start()

    def ocr_backfill_worker(self, files: list[Path]) -> None:
        indexed = 0
        try:
            for path in files:
                if not path.exists() or screenshot_has_ocr_data(path):
                    continue
                try:
                    with Image.open(path) as image:
                        with self.ocr_lock:
                            text = self.ocr.read_text(image)
                    write_ocr_metadata(path, text)
                    indexed += 1
                except Exception:
                    continue
        finally:
            self.ocr_backfill_busy = False
            self.bridge.ocr_index_updated.emit(str(indexed))

    def handle_ocr_index_updated(self, indexed_text: str) -> None:
        try:
            indexed = int(indexed_text)
        except ValueError:
            indexed = 0
        if self.history_open:
            self.refresh_history()
        if indexed:
            self.statusBar().showMessage(f"OCR indexed {indexed} screenshot{'s' if indexed != 1 else ''}.")

    def toggle_history(self) -> None:
        if self.history_open:
            self.close_history()
        else:
            self.open_history()

    def open_history(self) -> None:
        self.refresh_history()
        self.compact_geometry = QRect(self.geometry())
        self.compact_left_width = max(self.minimumWidth(), self.left_pane.width(), self.left_pane.sizeHint().width())
        self.left_pane.setFixedWidth(self.compact_left_width)
        self.history_open = True
        self.history_button.setText("Hide History")
        self.history_panel.show()
        if self.history_hide_connected:
            self.history_panel_animation.finished.disconnect(self.hide_history_after_animation)
            self.history_hide_connected = False
        self.history_panel_animation.stop()
        self.history_panel_animation.setStartValue(self.history_panel.maximumWidth())
        self.history_panel_animation.setEndValue(self.target_history_width())
        self.history_panel_animation.start()
        self.resize_for_history()

    def close_history(self) -> None:
        self.history_open = False
        self.history_button.setText("History")
        self.history_panel_animation.stop()
        self.history_panel_animation.setStartValue(self.history_panel.maximumWidth())
        self.history_panel_animation.setEndValue(0)
        if self.history_hide_connected:
            self.history_panel_animation.finished.disconnect(self.hide_history_after_animation)
            self.history_hide_connected = False
        self.history_panel_animation.finished.connect(self.hide_history_after_animation)
        self.history_hide_connected = True
        self.history_panel_animation.start()
        target = self.compact_geometry or QRect(self.x(), self.y(), max(self.minimumWidth(), self.left_pane.sizeHint().width()), self.sizeHint().height())
        self.animate_window_geometry(target)

    def hide_history_after_animation(self) -> None:
        if self.history_hide_connected:
            self.history_panel_animation.finished.disconnect(self.hide_history_after_animation)
            self.history_hide_connected = False
        if not self.history_open:
            self.history_panel.hide()
            self.left_pane.setMinimumWidth(460)
            self.left_pane.setMaximumWidth(16777215)
            if self.compact_geometry is not None:
                self.setGeometry(self.compact_geometry)
            self.compact_geometry = None

    def target_history_width(self) -> int:
        screen = QApplication.screenAt(self.frameGeometry().center()) or QApplication.primaryScreen()
        available = screen.availableGeometry() if screen else QRect(0, 0, 1200, 800)
        compact_width = self.compact_left_width if self.history_open else self.left_pane.sizeHint().width()
        return max(420, min(760, available.width() - compact_width - 44))

    def resize_for_history(self) -> None:
        screen = QApplication.screenAt(self.frameGeometry().center()) or QApplication.primaryScreen()
        available = screen.availableGeometry() if screen else QRect(0, 0, 1200, 800)
        compact_width = max(self.minimumWidth(), self.left_pane.sizeHint().width())
        target_width = min(available.width() - 24, compact_width + self.target_history_width())
        target_height = max(self.sizeHint().height(), min(available.height() - 48, 520))
        current = self.geometry()
        current_center_x = current.center().x()
        screen_center_x = available.center().x()
        desired_center_x = round((current_center_x + screen_center_x) / 2)
        target_x = desired_center_x - target_width // 2
        target_x = max(available.left() + 12, min(target_x, available.right() - target_width - 12))
        target_y = max(available.top() + 12, min(current.y(), available.bottom() - target_height - 12))
        target = QRect(target_x, target_y, target_width, target_height)
        self.animate_window_geometry(target)

    def animate_window_geometry(self, target: QRect) -> None:
        self.window_geometry_animation = QPropertyAnimation(self, b"geometry", self)
        self.window_geometry_animation.setDuration(260)
        self.window_geometry_animation.setEasingCurve(QEasingCurve.OutCubic)
        self.window_geometry_animation.setStartValue(self.geometry())
        self.window_geometry_animation.setEndValue(target)
        self.window_geometry_animation.start()

    def show_previous_history(self) -> None:
        if not self.history_files:
            return
        self.history_index = (self.history_index - 1) % len(self.history_files)
        self.update_history_view()

    def show_next_history(self) -> None:
        if not self.history_files:
            return
        self.history_index = (self.history_index + 1) % len(self.history_files)
        self.update_history_view()

    def delete_current_history(self) -> None:
        if not self.history_files:
            return
        path = self.history_files[self.history_index]
        try:
            path.unlink()
            metadata_path = ocr_metadata_path(path)
            if metadata_path.exists():
                metadata_path.unlink()
        except OSError as exc:
            QMessageBox.critical(self, "OCRShot", f"Could not delete screenshot:\n{exc}")
            return
        self.refresh_history()
        self.statusBar().showMessage(f"Deleted screenshot: {path.name}")

    def refresh_window_label(self) -> None:
        if self.selected_hwnd and is_selectable_window(self.selected_hwnd):
            title = get_window_title(self.selected_hwnd)
            _, process_name = get_process_info(self.selected_hwnd)
            suffix = f" ({process_name})" if process_name else ""
            self.window_panel.set_window_text(f"{title}{suffix}")
        elif self.config.selected_title:
            self.window_panel.set_window_text(f"Last used: {self.config.selected_title} (not currently found)")
        else:
            self.window_panel.set_window_text("No window selected")

    def persist_config(self) -> None:
        self.config.monitor_word = self.word_field.text().strip()
        self.config.selected_hwnd = self.selected_hwnd
        self.config.scan_interval_seconds = self.interval_input.value()
        self.config.trigger_cooldown_seconds = self.cooldown_input.value()
        self.config.match_case = self.match_case_switch.isChecked()
        if self.selected_hwnd and win32gui.IsWindow(self.selected_hwnd):
            self.config.selected_title = get_window_title(self.selected_hwnd)
            self.config.selected_process_id, self.config.selected_process_name = get_process_info(self.selected_hwnd)
        save_config(self.config)
        if self.monitoring:
            self.scan_timer.setInterval(round(self.config.scan_interval_seconds * 1000))

    def begin_window_selection(self) -> None:
        self.statusBar().showMessage("Click the window you want OCRShot to monitor...")
        self.setWindowState(self.windowState() | Qt.WindowMinimized)
        QTimer.singleShot(180, self.selection_overlay.start)

    def complete_window_selection(self, hwnd: int) -> None:
        self.setWindowState(self.windowState() & ~Qt.WindowMinimized)
        self.activateWindow()
        self.selected_hwnd = hwnd
        self.reset_ocr_scan_cache()
        self.persist_config()
        self.refresh_window_label()
        try:
            self.update_preview_from_image(capture_window(hwnd))
        except Exception:
            pass
        self.statusBar().showMessage("Window selected.")

    def cancel_window_selection(self) -> None:
        self.setWindowState(self.windowState() & ~Qt.WindowMinimized)
        self.activateWindow()
        self.statusBar().showMessage("Window selection canceled.")

    def reset_ocr_scan_cache(self) -> None:
        self.tile_ocr_cache = TileOcrCache()

    def showEvent(self, event: Any) -> None:
        super().showEvent(event)
        set_mica(int(self.winId()))
        if not self.monitoring:
            set_caption_color(int(self.winId()), None)

    def closeEvent(self, event: Any) -> None:
        self.taskbar.set_state(int(self.winId()), TBPF_NOPROGRESS)
        set_caption_color(int(self.winId()), None)
        super().closeEvent(event)

    def update_monitoring_caption(self) -> None:
        elapsed = time.monotonic() - self.monitoring_started_at
        fade = min(1.0, elapsed / 0.5)
        red = QColor(214, 46, 57)
        dark_red = QColor(128, 22, 31)
        if fade < 1.0:
            start = self.accent_color
            color = QColor(
                round(start.red() + (red.red() - start.red()) * fade),
                round(start.green() + (red.green() - start.green()) * fade),
                round(start.blue() + (red.blue() - start.blue()) * fade),
            )
        else:
            pulse = (math.sin((elapsed - 0.5) * math.tau / 1.4) + 1) / 2
            color = QColor(
                round(dark_red.red() + (red.red() - dark_red.red()) * pulse),
                round(dark_red.green() + (red.green() - dark_red.green()) * pulse),
                round(dark_red.blue() + (red.blue() - dark_red.blue()) * pulse),
            )
        set_caption_color(int(self.winId()), color)

    def toggle_monitoring(self) -> None:
        if self.monitoring:
            self.monitoring = False
            self.scan_timer.stop()
            self.start_button.setText("Start Monitoring")
            self.caption_timer.stop()
            set_caption_color(int(self.winId()), None)
            self.taskbar.set_state(int(self.winId()), TBPF_NOPROGRESS)
            self.statusBar().showMessage("Monitoring stopped.")
            return

        if not self.selected_hwnd or not is_selectable_window(self.selected_hwnd):
            QMessageBox.warning(self, "OCRShot", "Select a window before starting monitoring.")
            return
        if not self.word_field.text().strip():
            QMessageBox.warning(self, "OCRShot", "Enter a word to monitor before starting.")
            return

        self.persist_config()
        self.monitoring = True
        self.last_had_match = False
        self.monitoring_started_at = time.monotonic()
        self.caption_timer.start()
        self.taskbar.set_state(int(self.winId()), TBPF_INDETERMINATE)
        self.scan_timer.start(round(self.interval_input.value() * 1000))
        self.start_button.setText("Stop Monitoring")
        self.statusBar().showMessage("Monitoring started.")
        self.start_scan()

    def start_scan(self) -> None:
        if self.scan_busy or not self.selected_hwnd:
            return
        self.scan_busy = True
        hwnd = self.selected_hwnd
        word = self.word_field.text().strip()
        match_case = self.match_case_switch.isChecked()
        cooldown = self.cooldown_input.value()
        threading.Thread(target=self.scan_once, args=(hwnd, word, match_case, cooldown), daemon=True).start()

    def ocr_crop_text(self, image: Image.Image, box: tuple[int, int, int, int]) -> str:
        crop = image.crop(box)
        with self.ocr_lock:
            return self.ocr.read_text(crop)

    def ocr_all_tiles(self, image: Image.Image, boxes: list[tuple[int, int, int, int]]) -> list[str]:
        size = image.size
        return [self.ocr_crop_text(image, padded_box(box, size)) for box in boxes]

    def read_text_optimized(self, image: Image.Image) -> tuple[str, str, int]:
        size = image.size
        boxes = tile_boxes(size)
        fingerprints = [tile_fingerprint(image, box) for box in boxes]
        cache = self.tile_ocr_cache

        if cache.image_size != size or cache.fingerprints is None:
            changed_indices = list(range(len(boxes)))
            cache.tile_cache_valid = False
        else:
            changed_indices = [
                index
                for index, (previous, current) in enumerate(zip(cache.fingerprints, fingerprints, strict=True))
                if tile_difference(previous, current) >= OCR_TILE_CHANGE_THRESHOLD
            ]

        if not changed_indices:
            cache.fingerprints = fingerprints
            return cache.combined_text, "unchanged", 0

        if not cache.tile_cache_valid or cache.tile_texts is None or len(changed_indices) > OCR_MAX_INCREMENTAL_TILES:
            tile_texts = self.ocr_all_tiles(image, boxes)
            text = "\n".join(text for text in tile_texts if text.strip())
            cache.image_size = size
            cache.fingerprints = fingerprints
            cache.tile_texts = tile_texts
            cache.combined_text = text
            cache.tile_cache_valid = True
            return text, "rebuilt", len(changed_indices)

        tile_texts = list(cache.tile_texts)
        for index in changed_indices:
            tile_texts[index] = self.ocr_crop_text(image, padded_box(boxes[index], size))

        text = "\n".join(text for text in tile_texts if text.strip())
        cache.image_size = size
        cache.fingerprints = fingerprints
        cache.tile_texts = tile_texts
        cache.combined_text = text
        cache.tile_cache_valid = True
        return text, "tiles", len(changed_indices)

    def scan_once(self, hwnd: int, word: str, match_case: bool, cooldown: int) -> None:
        try:
            image = capture_window(hwnd)
            self.bridge.preview_ready.emit(image.copy())
            text, scan_mode, changed_count = self.read_text_optimized(image)
            matched = contains_word(text, word, match_case)
            now = time.monotonic()
            should_save = matched and (not self.last_had_match or now - self.last_trigger_at >= cooldown)
            screenshot_path = ""
            if should_save:
                screenshot_path = self.save_screenshot_image(image, word, text)
                self.last_trigger_at = now
            self.last_had_match = matched
            self.bridge.completed.emit(text, screenshot_path, f"{scan_mode}:{changed_count}:{self.ocr.provider_label}")
        except Exception as exc:
            self.bridge.failed.emit(str(exc))

    def save_screenshot_image(self, image: Image.Image, word: str, ocr_text: str | None = None) -> str:
        ensure_app_dirs()
        window_title = get_window_title(self.selected_hwnd) if self.selected_hwnd else "window"
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = f"{timestamp}_{safe_filename_piece(window_title)}_{safe_filename_piece(word)}.png"
        path = SCREENSHOT_DIR / filename
        image.save(path)
        if ocr_text is not None:
            write_ocr_metadata(path, ocr_text, window_title)
        return str(path)

    def save_manual_capture(self) -> None:
        if not self.selected_hwnd or not is_selectable_window(self.selected_hwnd):
            QMessageBox.warning(self, "OCRShot", "Select a window before capturing.")
            return
        try:
            image = capture_window(self.selected_hwnd)
            path = self.save_screenshot_image(image, "manual")
            self.update_preview_from_image(image)
            if self.history_open:
                self.refresh_history(Path(path))
            self.start_ocr_backfill()
        except Exception as exc:
            QMessageBox.critical(self, "OCRShot", str(exc))
            return
        self.statusBar().showMessage(f"Saved screenshot: {path}")

    def update_preview_from_image(self, image: Image.Image) -> None:
        self.window_panel.set_preview(image_to_pixmap(image))
        if self.window_panel.is_expanded():
            self.resize_to_content()

    def handle_scan_completed(self, text: str, screenshot_path: str, selected_hwnd: str) -> None:
        self.scan_busy = False
        scan_mode, changed_count, provider = self.parse_scan_status(selected_hwnd)
        if screenshot_path:
            if self.history_open:
                self.refresh_history(Path(screenshot_path))
            self.statusBar().showMessage(f"Match found. Saved {screenshot_path}")
        else:
            fragment_count = len([line for line in text.splitlines() if line.strip()])
            if scan_mode == "unchanged":
                self.statusBar().showMessage(f"Skipped OCR; window unchanged. Cached text fragments: {fragment_count}")
            elif scan_mode == "tiles":
                self.statusBar().showMessage(f"OCR {changed_count} changed tile{'s' if changed_count != 1 else ''} on {provider}. Text fragments: {fragment_count}")
            elif scan_mode == "rebuilt":
                self.statusBar().showMessage(f"OCR cache rebuilt on {provider}. Changed tiles: {changed_count}. Text fragments: {fragment_count}")
            else:
                self.statusBar().showMessage(f"Scanned window. OCR text fragments: {fragment_count}")

    def parse_scan_status(self, status: str) -> tuple[str, int, str]:
        parts = status.split(":")
        if len(parts) != 3:
            return "", 0, self.ocr.provider_label
        mode, count_text, provider = parts
        try:
            count = int(count_text)
        except ValueError:
            count = 0
        return mode, count, provider

    def handle_scan_failed(self, message: str) -> None:
        self.scan_busy = False
        self.setWindowState(self.windowState() & ~Qt.WindowMinimized)
        self.activateWindow()
        self.statusBar().showMessage(message)
        if self.monitoring:
            self.toggle_monitoring()


def main() -> None:
    if sys.platform != "win32":
        raise SystemExit("OCRShot only supports Windows.")

    ensure_app_dirs()
    app = QApplication(sys.argv)
    window = MainWindow()
    window.show()
    raise SystemExit(app.exec())
