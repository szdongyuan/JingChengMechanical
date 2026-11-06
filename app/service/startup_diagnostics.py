"""Bounded startup evidence shared by the capture worker and parent."""

import unicodedata


MAX_STARTUP_DETAIL_CHARS = 256


def bounded_startup_text(value: object, limit: int = MAX_STARTUP_DETAIL_CHARS) -> str:
    try:
        text = str(value)
    except Exception:
        text = "<message unavailable>"
    if isinstance(value, Exception):
        text = f"{type(value).__name__}: {text}"
    # Keep diagnostics printable and encodable even for malformed backend text.
    text = text[:limit].encode("utf-8", errors="replace").decode("utf-8")
    text = "".join(
        " " if unicodedata.category(character) == "Cc" else character
        for character in text
    )
    return " ".join(text.split())
