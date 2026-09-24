"""Deterministic display mappings and safe v2 package member names."""

from __future__ import annotations

import re
import unicodedata


_INVALID_SN_CHARACTERS = frozenset('<>:"/\\|?*')
_RESERVED_WINDOWS_BASENAMES = frozenset(
    {"CON", "PRN", "AUX", "NUL"}
    | {f"COM{index}" for index in range(1, 10)}
    | {f"LPT{index}" for index in range(1, 10)}
)
_ERROR_STATES = frozenset({"FAILED", "CANCELED", "INTERRUPTED"})


def excel_result(*, state: str, judgment: str | None) -> str:
    """Return the exact operator-facing final-result text."""

    if type(state) is not str:
        raise TypeError("state must be an exact string")
    if judgment is not None and type(judgment) is not str:
        raise TypeError("judgment must be an exact string or None")
    if judgment == "PASS":
        return "OK"
    if judgment == "FAIL":
        return "NG"
    if judgment is not None:
        raise ValueError("judgment is invalid")
    if state == "NO_RESULT":
        return "无结果"
    if state in _ERROR_STATES:
        return "错误"
    raise ValueError("state and judgment do not describe an exportable result")


def manifest_result(*, state: str, judgment: str | None) -> str:
    """Return the exact v2 manifest outcome enum."""

    display = excel_result(state=state, judgment=judgment)
    return {"OK": "OK", "NG": "NG", "无结果": "NO_RESULT", "错误": "ERROR"}[
        display
    ]


def excel_judgment_source(judgment_source: str) -> str:
    """Translate the durable source enum for the workbook."""

    if type(judgment_source) is not str:
        raise TypeError("judgment source must be an exact string")
    try:
        return {"AUTOMATIC": "自动", "MANUAL": "人工", "NONE": ""}[
            judgment_source
        ]
    except KeyError:
        raise ValueError("judgment source is invalid") from None


def sanitize_sn_segment(sn: str) -> str:
    """Normalize an SN using the ordered v2 filesystem-safety rules."""

    if type(sn) is not str:
        raise TypeError("SN must be an exact string")
    value = sn.strip()
    value = "".join(
        "_"
        if character in _INVALID_SN_CHARACTERS
        or unicodedata.category(character) == "Cc"
        else character
        for character in value
    )
    value = re.sub(r"_+", "_", value)
    value = value.rstrip(" .")
    if not value:
        value = "无SN"
    if value.split(".", 1)[0].upper() in _RESERVED_WINDOWS_BASENAMES:
        value = f"_{value}"
    value = value[:48].rstrip(" .")
    return value or "无SN"


def record_directory(sequence: int, sn: str) -> str:
    """Return the package-root-relative directory for one ordered record."""

    if type(sequence) is not int:
        raise TypeError("sequence must be an integer")
    if not 1 <= sequence <= 999_999:
        raise ValueError("sequence must fit a positive six-digit prefix")
    return _validated_member_path(
        f"records/{sequence:06d}_{sanitize_sn_segment(sn)}"
    )


def audio_member_path(directory: str, *, source_channel: int) -> str:
    """Return the WAV member name for a zero-based source channel."""

    base = _validated_member_path(directory)
    if type(source_channel) is not int:
        raise TypeError("source channel must be an integer")
    if source_channel < 0:
        raise ValueError("source channel must be nonnegative")
    return _validated_member_path(
        f"{base}/audio/CH{source_channel + 1}.wav"
    )


def current_member_path(directory: str) -> str:
    """Return the current-sample CSV member name for a record directory."""

    return _validated_member_path(f"{_validated_member_path(directory)}/current.csv")


def _validated_member_path(path: str) -> str:
    if type(path) is not str or not path or "\\" in path or path.startswith("/"):
        raise ValueError("path must be a package-relative POSIX path")
    if any(part in {"", ".", ".."} for part in path.split("/")):
        raise ValueError("path must be a package-relative POSIX path")
    return path
