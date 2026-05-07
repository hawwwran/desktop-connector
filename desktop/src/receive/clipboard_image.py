"""Clipboard-image filename helpers (free functions, no Poller state)."""


def _clipboard_image_extension(mime_type: str | None, data: bytes) -> str:
    normalized = (mime_type or "").split(";", 1)[0].strip().lower()
    by_mime = {
        "image/png": ".png",
        "image/jpeg": ".jpg",
        "image/jpg": ".jpg",
        "image/gif": ".gif",
        "image/webp": ".webp",
        "image/bmp": ".bmp",
        "image/tiff": ".tiff",
        "image/heic": ".heic",
        "image/heif": ".heif",
        "image/svg+xml": ".svg",
    }
    if normalized in by_mime:
        return by_mime[normalized]

    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return ".png"
    if data.startswith(b"\xff\xd8\xff"):
        return ".jpg"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return ".gif"
    if len(data) >= 12 and data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        return ".webp"
    if data.startswith(b"BM"):
        return ".bmp"
    if data.startswith((b"II*\x00", b"MM\x00*")):
        return ".tiff"

    return ".png"


def _clipboard_image_filename(mime_type: str | None, data: bytes) -> str:
    return f"clipboard-image{_clipboard_image_extension(mime_type, data)}"
