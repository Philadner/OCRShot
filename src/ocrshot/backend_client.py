from __future__ import annotations

import base64
import json
import os
import subprocess
import sys
import threading
import uuid
from pathlib import Path
from typing import Any

from PySide6.QtCore import QObject, Signal


class BackendError(RuntimeError):
    pass


class BackendClient(QObject):
    previewReady = Signal(dict)
    scanCompleted = Signal(dict)
    scanFailed = Signal(dict)
    historyUpdated = Signal(dict)
    ocrIndexUpdated = Signal(dict)

    def __init__(self, process: subprocess.Popen[str]) -> None:
        super().__init__()
        self.process = process
        self._pending: dict[str, tuple[threading.Event, dict[str, Any]]] = {}
        self._lock = threading.Lock()
        self._reader = threading.Thread(target=self._read_loop, daemon=True)
        self._reader.start()

    @classmethod
    def start_default(cls) -> "BackendClient | None":
        frozen_root = Path(getattr(sys, "_MEIPASS", ""))
        bundled_exe = frozen_root / "ocrshot-backend.exe" if frozen_root else Path()
        root = Path(__file__).resolve().parents[2]
        backend_dir = root / "backend"
        source_exe = backend_dir / "ocrshot-backend.exe"
        if bundled_exe.exists():
            command = [str(bundled_exe)]
            cwd = str(frozen_root)
        elif source_exe.exists():
            command = [str(source_exe)]
            cwd = str(root)
        else:
            command = ["go", "run", "."]
            cwd = str(backend_dir)

        env = os.environ.copy()

        startupinfo = None
        if sys.platform == "win32":
            startupinfo = subprocess.STARTUPINFO()
            startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW

        try:
            process = subprocess.Popen(
                command,
                cwd=cwd,
                env=env,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                encoding="utf-8",
                startupinfo=startupinfo,
            )
        except OSError:
            return None
        return cls(process)

    def available(self) -> bool:
        return self.process.poll() is None and self.process.stdin is not None

    def call(self, method: str, params: dict[str, Any] | None = None, timeout: float = 30.0) -> Any:
        if not self.available():
            raise BackendError("OCRShot backend is not running.")
        request_id = uuid.uuid4().hex
        event = threading.Event()
        holder: dict[str, Any] = {}
        with self._lock:
            self._pending[request_id] = (event, holder)
        payload = {"id": request_id, "method": method, "params": params or {}}
        assert self.process.stdin is not None
        self.process.stdin.write(json.dumps(payload) + "\n")
        self.process.stdin.flush()
        if not event.wait(timeout):
            with self._lock:
                self._pending.pop(request_id, None)
            raise BackendError(f"Backend call timed out: {method}")
        if holder.get("error"):
            raise BackendError(str(holder["error"].get("message", "Backend error")))
        return holder.get("result")

    def shutdown(self) -> None:
        try:
            if self.available():
                self.call("Shutdown", timeout=2.0)
        except Exception:
            pass
        try:
            if self.process.stdin:
                self.process.stdin.close()
        except Exception:
            pass
        if self.process.poll() is None:
            self.process.terminate()

    def _read_loop(self) -> None:
        if self.process.stdout is None:
            return
        for line in self.process.stdout:
            try:
                message = json.loads(line)
            except json.JSONDecodeError:
                continue
            if "id" in message:
                request_id = str(message["id"])
                with self._lock:
                    pending = self._pending.pop(request_id, None)
                if pending is None:
                    continue
                event, holder = pending
                holder.update(message)
                event.set()
                continue
            event_name = message.get("event")
            payload = message.get("payload") or {}
            if event_name == "previewReady":
                self.previewReady.emit(payload)
            elif event_name == "scanCompleted":
                self.scanCompleted.emit(payload)
            elif event_name == "scanFailed":
                self.scanFailed.emit(payload)
            elif event_name == "historyUpdated":
                self.historyUpdated.emit(payload)
            elif event_name == "ocrIndexUpdated":
                self.ocrIndexUpdated.emit(payload)


def png_base64_to_qimage(data: str) -> Any:
    from PySide6.QtCore import QByteArray
    from PySide6.QtGui import QImage

    raw = base64.b64decode(data)
    image = QImage()
    image.loadFromData(QByteArray(raw), "PNG")
    return image
