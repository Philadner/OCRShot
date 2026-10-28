# OCRShot

OCRShot is a small Windows Qt app that monitors one selected application window.
When the configured word appears in that window's OCR text, it saves a screenshot
of only that window.

Screenshots are saved to:

```text
%LOCALAPPDATA%\Philadner\OCRShot\Screenshots
```

Configuration is saved to:

```text
%LOCALAPPDATA%\Philadner\OCRShot\config.json
```

## Run

```powershell
uv sync
go build -C backend -o ocrshot-backend.exe .
uv run ocrshot
```

If `backend\ocrshot-backend.exe` is missing during development, the Python
frontend will try to start the backend with `go run .` from the `backend`
directory.

The project targets Python 3.13 because the Qt, OCR, and Windows packages are
more likely to have compatible wheels there than on Python 3.14.

## Architecture

OCRShot is migrating to a hybrid architecture:

- The Python PySide6 app remains the native Windows frontend.
- The Go backend speaks JSON Lines over stdin/stdout and owns config/history,
  window capture, screenshot saving, and backend-driven monitoring.
- OCR runs in the Go backend through ONNX Runtime. It auto-tries CUDA when the
  ONNX Runtime GPU DLLs are available, and falls back to CPU. Set
  `OCRSHOT_OCR_PROVIDER=CPU` or `OCRSHOT_DISABLE_CUDA=1` to force CPU.

## Go OCR Runtime

The backend looks for RapidOCR/PaddleOCR ONNX models in this order:

1. `OCRSHOT_MODELS_DIR`
2. `backend\models`
3. the development `.venv` RapidOCR package model directory

The backend looks for `onnxruntime.dll` in `OCRSHOT_ORT_DLL`, next to the
backend executable, or in the development `.venv` ONNX Runtime package.
