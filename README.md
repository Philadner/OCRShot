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
uv run ocrshot
```

The project targets Python 3.13 because the Qt, OCR, and Windows packages are
more likely to have compatible wheels there than on Python 3.14.
