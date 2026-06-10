from __future__ import annotations

from copy import deepcopy
import json
from typing import Any

LAYOUT_VERSION = 1
MIN_COLUMNS = 1
MAX_COLUMNS = 3
MIN_ROWS = 1

PANEL_RESULT_SUMMARY = "result_summary"
PANEL_STFT = "stft"
PANEL_SPECTRUM = "spectrum"
PANEL_OCTAVE = "octave"
PANEL_SPL = "spl"
PANEL_LOUDNESS = "loudness"
PANEL_VIBRATION_STFT = "vibration_stft"
PANEL_VIBRATION_SPECTRUM = "vibration_spectrum"
PANEL_VIBRATION_OCTAVE = "vibration_octave"
PANEL_VIBRATION_FREQUENCY_STABILITY = "vibration_frequency_stability"
PANEL_VIBRATION_PULSE = "vibration_pulse"

CHANNEL_NONE = "none"
CHANNEL_PRIMARY_ACOUSTIC = "primary_acoustic"
CHANNEL_VIBRATION = "vibration"
CHANNEL_SELECTED_1 = "selected_1"
CHANNEL_SELECTED_2 = "selected_2"

PANEL_FAMILY_SUMMARY = "summary"
PANEL_FAMILY_ACOUSTIC = "acoustic"
PANEL_FAMILY_VIBRATION = "vibration"

PANEL_TITLES = {
    PANEL_RESULT_SUMMARY: "结果摘要",
    PANEL_STFT: "时频图",
    PANEL_SPECTRUM: "频谱",
    PANEL_OCTAVE: "倍频程",
    PANEL_SPL: "声压级",
    PANEL_LOUDNESS: "响度",
    PANEL_VIBRATION_STFT: "时频图（振动）",
    PANEL_VIBRATION_SPECTRUM: "频谱（振动）",
    PANEL_VIBRATION_OCTAVE: "倍频程（振动）",
    PANEL_VIBRATION_FREQUENCY_STABILITY: "频率稳定性（振动）",
    PANEL_VIBRATION_PULSE: "脉冲强度（振动）",
}

PANEL_FAMILIES = {
    PANEL_RESULT_SUMMARY: PANEL_FAMILY_SUMMARY,
    PANEL_STFT: PANEL_FAMILY_ACOUSTIC,
    PANEL_SPECTRUM: PANEL_FAMILY_ACOUSTIC,
    PANEL_OCTAVE: PANEL_FAMILY_ACOUSTIC,
    PANEL_SPL: PANEL_FAMILY_ACOUSTIC,
    PANEL_LOUDNESS: PANEL_FAMILY_ACOUSTIC,
    PANEL_VIBRATION_STFT: PANEL_FAMILY_VIBRATION,
    PANEL_VIBRATION_SPECTRUM: PANEL_FAMILY_VIBRATION,
    PANEL_VIBRATION_OCTAVE: PANEL_FAMILY_VIBRATION,
    PANEL_VIBRATION_FREQUENCY_STABILITY: PANEL_FAMILY_VIBRATION,
    PANEL_VIBRATION_PULSE: PANEL_FAMILY_VIBRATION,
}

ALLOWED_CHANNELS_BY_PANEL_FAMILY = {
    PANEL_FAMILY_SUMMARY: {CHANNEL_NONE},
    PANEL_FAMILY_ACOUSTIC: {
        CHANNEL_PRIMARY_ACOUSTIC,
        CHANNEL_SELECTED_1,
        CHANNEL_SELECTED_2,
    },
    PANEL_FAMILY_VIBRATION: {
        CHANNEL_VIBRATION,
        CHANNEL_SELECTED_1,
        CHANNEL_SELECTED_2,
    },
}

DEFAULT_RESULT_DASHBOARD_LAYOUT = {
    "version": LAYOUT_VERSION,
    "columns": 3,
    "rows": 2,
    "panels": [
        {
            "id": "summary",
            "type": PANEL_RESULT_SUMMARY,
            "row": 0,
            "column": 0,
            "row_span": 1,
            "column_span": 1,
            "channel": CHANNEL_NONE,
        },
        {
            "id": "stft_acoustic",
            "type": PANEL_STFT,
            "row": 0,
            "column": 1,
            "row_span": 1,
            "column_span": 1,
            "channel": CHANNEL_PRIMARY_ACOUSTIC,
        },
        {
            "id": "spectrum_acoustic",
            "type": PANEL_SPECTRUM,
            "row": 0,
            "column": 2,
            "row_span": 1,
            "column_span": 1,
            "channel": CHANNEL_PRIMARY_ACOUSTIC,
        },
        {
            "id": "octave_acoustic",
            "type": PANEL_OCTAVE,
            "row": 1,
            "column": 0,
            "row_span": 1,
            "column_span": 1,
            "channel": CHANNEL_PRIMARY_ACOUSTIC,
        },
        {
            "id": "vibration_frequency",
            "type": PANEL_VIBRATION_FREQUENCY_STABILITY,
            "row": 1,
            "column": 1,
            "row_span": 1,
            "column_span": 1,
            "channel": CHANNEL_VIBRATION,
        },
        {
            "id": "vibration_pulse",
            "type": PANEL_VIBRATION_PULSE,
            "row": 1,
            "column": 2,
            "row_span": 1,
            "column_span": 1,
            "channel": CHANNEL_VIBRATION,
        },
    ],
}


class ResultDashboardLayoutError(ValueError):
    pass


def normalize_result_dashboard_layout(
    raw: object,
    *,
    selected_channels: list[int] | None = None,
) -> dict[str, Any]:
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        return deepcopy(DEFAULT_RESULT_DASHBOARD_LAYOUT)
    return validate_result_dashboard_layout(
        _decode_layout(raw),
        selected_channels=selected_channels,
    )


def normalize_result_dashboard_layout_for_display(
    raw: object,
    *,
    selected_channels: list[int] | None = None,
) -> dict[str, Any]:
    try:
        return normalize_result_dashboard_layout(
            raw,
            selected_channels=selected_channels,
        )
    except ResultDashboardLayoutError:
        return deepcopy(DEFAULT_RESULT_DASHBOARD_LAYOUT)


def serialize_result_dashboard_layout(layout: dict[str, Any]) -> str:
    normalized = normalize_result_dashboard_layout(layout)
    return json.dumps(normalized, ensure_ascii=False, separators=(",", ":"))


def validate_result_dashboard_layout(
    layout: dict[str, Any],
    *,
    selected_channels: list[int] | None = None,
) -> dict[str, Any]:
    if not isinstance(layout, dict):
        raise ResultDashboardLayoutError("结果展示布局 layout 必须是 JSON object")

    normalized = {
        "version": _coerce_int(layout.get("version"), "version"),
        "columns": _coerce_int(layout.get("columns"), "columns"),
        "rows": _coerce_int(layout.get("rows"), "rows"),
        "panels": [],
    }
    _validate_version(normalized["version"])
    _validate_grid_size(normalized["columns"], normalized["rows"])

    raw_panels = layout.get("panels")
    if not isinstance(raw_panels, list):
        raise ResultDashboardLayoutError("结果展示布局 panels 必须是列表")

    seen_ids: set[str] = set()
    placed_panels: list[dict[str, Any]] = []
    summary_count = 0

    for index, raw_panel in enumerate(raw_panels):
        panel = _normalize_panel(raw_panel, index)
        panel_id = panel["id"]
        panel_type = panel["type"]
        panel_channel = panel["channel"]

        if panel_id in seen_ids:
            raise ResultDashboardLayoutError(f"结果展示布局面板 id 必须唯一 unique: {panel_id}")
        seen_ids.add(panel_id)

        if panel_type not in PANEL_TITLES:
            raise ResultDashboardLayoutError(f"未知结果展示面板 panel type: {panel_type}")

        family = PANEL_FAMILIES[panel_type]
        allowed_channels = ALLOWED_CHANNELS_BY_PANEL_FAMILY[family]
        if panel_channel not in allowed_channels:
            raise ResultDashboardLayoutError(
                f"结果展示布局面板通道 channel 无效: {panel_channel}"
            )
        if panel_type == PANEL_RESULT_SUMMARY:
            summary_count += 1
            if panel_channel != CHANNEL_NONE:
                raise ResultDashboardLayoutError("结果摘要面板 channel 通道必须为 none")

        _validate_selected_channel_shape(panel_channel, selected_channels)
        _validate_panel_bounds(panel, normalized["rows"], normalized["columns"])
        _validate_panel_does_not_overlap(panel, placed_panels)
        placed_panels.append(panel)
        normalized["panels"].append(panel)

    if summary_count != 1:
        raise ResultDashboardLayoutError("结果展示布局必须包含且仅包含一个结果摘要 exactly one summary")

    return normalized


def resolve_layout_channel(
    channel_key: str,
    *,
    selected_channels: list[int],
    vibration_view_channel_index: int | None,
    panel_family: str,
) -> int | None:
    if channel_key == CHANNEL_NONE:
        resolved = None
    elif channel_key == CHANNEL_SELECTED_1:
        resolved = selected_channels[0] if len(selected_channels) >= 1 else None
    elif channel_key == CHANNEL_SELECTED_2:
        resolved = selected_channels[1] if len(selected_channels) >= 2 else None
    elif channel_key == CHANNEL_VIBRATION:
        resolved = vibration_view_channel_index
    elif channel_key == CHANNEL_PRIMARY_ACOUSTIC:
        resolved = _first_non_vibration_channel(
            selected_channels,
            vibration_view_channel_index,
        )
    else:
        raise ResultDashboardLayoutError(f"未知结果展示通道 channel: {channel_key}")

    if (
        panel_family == PANEL_FAMILY_VIBRATION
        and resolved != vibration_view_channel_index
    ):
        return None
    return resolved


def _decode_layout(raw: object) -> dict[str, Any]:
    if isinstance(raw, str):
        try:
            decoded = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ResultDashboardLayoutError(
                f"结果展示布局 JSON invalid: {exc.msg}"
            ) from exc
        raw = decoded

    if not isinstance(raw, dict):
        raise ResultDashboardLayoutError("结果展示布局 layout 必须是 JSON object")
    return deepcopy(raw)


def _coerce_int(value: object, field_name: str) -> int:
    if isinstance(value, bool):
        raise ResultDashboardLayoutError(f"结果展示布局 {field_name} 必须是整数")
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        try:
            return int(value)
        except ValueError as exc:
            raise ResultDashboardLayoutError(
                f"结果展示布局 {field_name} 必须是整数"
            ) from exc
    raise ResultDashboardLayoutError(f"结果展示布局 {field_name} 必须是整数")


def _require_text(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ResultDashboardLayoutError(f"结果展示布局面板 {field_name} 不能为空")
    return value


def _validate_version(version: int) -> None:
    if version != LAYOUT_VERSION:
        raise ResultDashboardLayoutError(
            f"结果展示布局版本 version 必须为 {LAYOUT_VERSION}"
        )


def _validate_grid_size(columns: int, rows: int) -> None:
    if columns < MIN_COLUMNS or columns > MAX_COLUMNS:
        raise ResultDashboardLayoutError(
            f"结果展示布局列 columns 必须在 {MIN_COLUMNS} 到 {MAX_COLUMNS} 之间"
        )
    if rows < MIN_ROWS:
        raise ResultDashboardLayoutError(f"结果展示布局行 rows 必须至少为 {MIN_ROWS}")


def _normalize_panel(raw_panel: object, index: int) -> dict[str, Any]:
    if not isinstance(raw_panel, dict):
        raise ResultDashboardLayoutError(f"结果展示布局面板 panel #{index + 1} 必须是对象")
    return {
        "id": _require_text(raw_panel.get("id"), "id"),
        "type": _require_text(raw_panel.get("type"), "type"),
        "row": _coerce_int(raw_panel.get("row"), "row"),
        "column": _coerce_int(raw_panel.get("column"), "column"),
        "row_span": _coerce_int(raw_panel.get("row_span"), "row_span"),
        "column_span": _coerce_int(raw_panel.get("column_span"), "column_span"),
        "channel": _require_text(raw_panel.get("channel"), "channel"),
    }


def _validate_selected_channel_shape(
    channel_key: str,
    selected_channels: list[int] | None,
) -> None:
    if selected_channels is None:
        return
    if channel_key == CHANNEL_SELECTED_1 and len(selected_channels) < 1:
        raise ResultDashboardLayoutError("通道1需要至少一个选中通道")
    if channel_key == CHANNEL_SELECTED_2 and len(selected_channels) < 2:
        raise ResultDashboardLayoutError("通道2需要至少两个选中通道")


def _validate_panel_bounds(panel: dict[str, Any], rows: int, columns: int) -> None:
    if panel["row"] < 0 or panel["column"] < 0:
        raise ResultDashboardLayoutError("结果展示布局面板越界 bounds")
    if panel["row_span"] < 1 or panel["column_span"] < 1:
        raise ResultDashboardLayoutError("结果展示布局面板跨度必须为正整数")
    if panel["row"] + panel["row_span"] > rows:
        raise ResultDashboardLayoutError("结果展示布局面板越界 bounds")
    if panel["column"] + panel["column_span"] > columns:
        raise ResultDashboardLayoutError("结果展示布局面板越界 bounds")


def _validate_panel_does_not_overlap(
    panel: dict[str, Any],
    placed_panels: list[dict[str, Any]],
) -> None:
    for existing_panel in placed_panels:
        if _panels_overlap(existing_panel, panel):
            raise ResultDashboardLayoutError(
                f"结果展示布局面板重叠 overlap: {existing_panel['id']}, {panel['id']}"
            )


def _panels_overlap(first: dict[str, Any], second: dict[str, Any]) -> bool:
    return (
        first["row"] < second["row"] + second["row_span"]
        and second["row"] < first["row"] + first["row_span"]
        and first["column"] < second["column"] + second["column_span"]
        and second["column"] < first["column"] + first["column_span"]
    )


def _first_non_vibration_channel(
    selected_channels: list[int],
    vibration_view_channel_index: int | None,
) -> int | None:
    for channel_index in selected_channels:
        if channel_index != vibration_view_channel_index:
            return channel_index
    return None
