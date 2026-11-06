"""Pure validation shared by torque editors, imports, and PLC processing."""
from __future__ import annotations

from decimal import Decimal, InvalidOperation
import math
import re
import struct

from app.service.torque.types import TorqueConfig


def normalize_key(value: str) -> str:
    """Trim only the surrounding whitespace of an exact text match key."""
    if not isinstance(value, str) or not value.strip():
        raise ValueError("匹配值必须为非空文本")
    return value.strip()


def validate_match_type(value: str) -> str:
    if not isinstance(value, str) or value not in ("model", "sn"):
        raise ValueError("匹配类型必须为型号或 SN")
    return value


def validate_torque(value: str | int | float) -> float:
    """Return the finite float32 value that will actually be written as REAL."""
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        raise ValueError("扭矩必须为数值")
    try:
        original = Decimal(value.strip() if isinstance(value, str) else value)
        if not original.is_finite():
            raise ValueError("扭矩必须为有限数值")
        raw = float(original)
        converted = struct.unpack(">f", struct.pack(">f", raw))[0]
    except (InvalidOperation, ValueError, TypeError, OverflowError, struct.error) as exc:
        raise ValueError("扭矩不是有效 REAL") from exc
    # Keep the Decimal until here: float("1e-1000") has already lost its nonzero value.
    if not math.isfinite(converted) or (original != 0 and converted == 0.0):
        raise ValueError("扭矩超出 REAL 范围")
    return converted


def parse_offset(value: str | int | float) -> int:
    """Accept a nonnegative byte address, optionally written as N.0."""
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        raise ValueError("字节偏移必须为非负整数或 N.0")
    text = str(value).strip()
    if re.fullmatch(r"[0-9]+(?:\.0)?", text) is None:
        raise ValueError("字节偏移必须为非负整数或 N.0，位偏移只能为 0")
    return int(text.split(".", 1)[0])


def validate_db_number(value: str | int | None) -> int | None:
    """None requests inheritance; supplied DB numbers must be positive integers."""
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise ValueError("DB 块号必须为正整数或留空")
    text = str(value).strip()
    if re.fullmatch(r"[0-9]+", text) is None or int(text) <= 0:
        raise ValueError("DB 块号必须为正整数或留空")
    return int(text)


def resolve_addresses(
    config: TorqueConfig, inherited_db: int,
) -> tuple[tuple[int, int | None], tuple[int, int | None]]:
    """Validate config and resolve DB inheritance before checking REAL overlap.

    Disabled configurations may retain incomplete addresses; any supplied fields
    must still have valid syntax. Callers must check enabled before performing I/O.
    """
    validate_match_type(config.match_type)
    read_db = validate_db_number(config.read_db_number)
    write_db = validate_db_number(config.write_db_number)
    if read_db is None or write_db is None:
        inherited_db = validate_db_number(inherited_db)
        if inherited_db is None:
            raise ValueError("继承的 DB 块号必须为正整数")
    read_db = inherited_db if read_db is None else read_db
    write_db = inherited_db if write_db is None else write_db
    read_offset = None if config.read_byte_offset is None else parse_offset(config.read_byte_offset)
    write_offset = None if config.write_byte_offset is None else parse_offset(config.write_byte_offset)
    if config.enabled:
        if read_offset is None or write_offset is None:
            raise ValueError("启用扭矩功能时读取和回传字节偏移必须完整")
        if read_db == write_db and abs(read_offset - write_offset) < 4:
            raise ValueError("扭矩读取和回传地址的 4 字节范围不能重叠")
    return (read_db, read_offset), (write_db, write_offset)
