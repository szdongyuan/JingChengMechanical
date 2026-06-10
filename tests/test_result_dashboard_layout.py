from __future__ import annotations

import pytest

import app.service.result_dashboard_layout as layout_module
from app.service.result_dashboard_layout import (
    DEFAULT_RESULT_DASHBOARD_LAYOUT,
    PANEL_FAMILIES,
    PANEL_TITLES,
    ResultDashboardLayoutError,
    normalize_result_dashboard_layout,
    normalize_result_dashboard_layout_for_display,
    resolve_layout_channel,
    serialize_result_dashboard_layout,
    validate_result_dashboard_layout,
)


def test_default_layout_matches_spec():
    layout = DEFAULT_RESULT_DASHBOARD_LAYOUT

    assert layout["columns"] == 3
    assert layout["rows"] == 2
    assert [panel["type"] for panel in layout["panels"]] == [
        "result_summary",
        "stft",
        "spectrum",
        "octave",
        "vibration_frequency_stability",
        "vibration_pulse",
    ]
    assert PANEL_TITLES["vibration_stft"] == "时频图（振动）"


def test_blank_layout_normalizes_to_default():
    assert normalize_result_dashboard_layout(None) == DEFAULT_RESULT_DASHBOARD_LAYOUT
    assert normalize_result_dashboard_layout("") == DEFAULT_RESULT_DASHBOARD_LAYOUT
    assert normalize_result_dashboard_layout("  \n\t") == DEFAULT_RESULT_DASHBOARD_LAYOUT


def test_valid_layout_serializes_to_json_string():
    text = serialize_result_dashboard_layout(DEFAULT_RESULT_DASHBOARD_LAYOUT)

    assert isinstance(text, str)
    assert normalize_result_dashboard_layout(text) == DEFAULT_RESULT_DASHBOARD_LAYOUT


def test_malformed_layout_is_rejected_for_save_and_import_paths():
    with pytest.raises(ResultDashboardLayoutError, match="结果展示布局|JSON|invalid"):
        normalize_result_dashboard_layout("{bad json")


def test_display_normalizer_falls_back_for_corrupt_runtime_config():
    assert (
        normalize_result_dashboard_layout_for_display("{bad json")
        == DEFAULT_RESULT_DASHBOARD_LAYOUT
    )


def _panel(
    panel_id: str,
    panel_type: str,
    *,
    row: int = 0,
    column: int = 0,
    row_span: int = 1,
    column_span: int = 1,
    channel: str = "none",
) -> dict:
    return {
        "id": panel_id,
        "type": panel_type,
        "row": row,
        "column": column,
        "row_span": row_span,
        "column_span": column_span,
        "channel": channel,
    }


def _layout(*panels: dict, columns: int = 1, rows: int = 1, version: int = 1) -> dict:
    return {
        "version": version,
        "columns": columns,
        "rows": rows,
        "panels": list(panels),
    }


def test_panel_families_match_catalog():
    assert PANEL_FAMILIES["result_summary"] == "summary"
    assert PANEL_FAMILIES["spectrum"] == "acoustic"
    assert PANEL_FAMILIES["vibration_pulse"] == "vibration"


def test_normalization_rejects_non_empty_malformed_layout_shape():
    with pytest.raises(ResultDashboardLayoutError, match="结果展示布局|layout"):
        normalize_result_dashboard_layout([])


def test_validation_rejects_overlapping_panels():
    layout = _layout(
        _panel("summary", "result_summary"),
        _panel("copy", "spectrum", channel="primary_acoustic"),
        columns=2,
    )

    with pytest.raises(ResultDashboardLayoutError, match="重叠|overlap"):
        validate_result_dashboard_layout(layout)


def test_validation_of_large_valid_spans_does_not_iterate_occupied_cells(monkeypatch):
    def fail_if_cell_ranges_are_materialized(*_args):
        raise AssertionError("layout validation must not iterate every occupied cell")

    monkeypatch.setattr(
        layout_module,
        "range",
        fail_if_cell_ranges_are_materialized,
        raising=False,
    )
    rows = 1_000_000_000
    layout = _layout(
        _panel("summary", "result_summary", row_span=rows),
        columns=1,
        rows=rows,
    )

    assert validate_result_dashboard_layout(layout) == layout


def test_validation_rejects_missing_summary():
    layout = _layout(columns=1, rows=1)

    with pytest.raises(ResultDashboardLayoutError, match="结果摘要|summary"):
        validate_result_dashboard_layout(layout)


def test_validation_rejects_duplicate_panel_ids():
    layout = _layout(
        _panel("dup", "result_summary"),
        _panel("dup", "spectrum", column=1, channel="primary_acoustic"),
        columns=2,
    )

    with pytest.raises(ResultDashboardLayoutError, match="唯一|unique|id"):
        validate_result_dashboard_layout(layout)


def test_validation_rejects_empty_panel_id():
    layout = _layout(_panel("", "result_summary"))

    with pytest.raises(ResultDashboardLayoutError, match="id"):
        validate_result_dashboard_layout(layout)


def test_validation_rejects_unknown_layout_version():
    layout = _layout(_panel("summary", "result_summary"), version=2)

    with pytest.raises(ResultDashboardLayoutError, match="版本|version"):
        validate_result_dashboard_layout(layout)


@pytest.mark.parametrize(
    ("columns", "rows", "expected"),
    [
        (0, 1, "列|columns"),
        (4, 1, "列|columns"),
        (1, 0, "行|rows"),
    ],
)
def test_validation_rejects_invalid_grid_size(columns, rows, expected):
    layout = _layout(_panel("summary", "result_summary"), columns=columns, rows=rows)

    with pytest.raises(ResultDashboardLayoutError, match=expected):
        validate_result_dashboard_layout(layout)


def test_validation_rejects_unknown_panel_type():
    layout = _layout(
        _panel("summary", "result_summary"),
        _panel("custom", "custom_widget", column=1, channel="primary_acoustic"),
        columns=2,
    )

    with pytest.raises(ResultDashboardLayoutError, match="面板|panel|type"):
        validate_result_dashboard_layout(layout)


def test_validation_rejects_invalid_channel_key():
    layout = _layout(
        _panel("summary", "result_summary"),
        _panel("spectrum", "spectrum", column=1, channel="bad_channel"),
        columns=2,
    )

    with pytest.raises(ResultDashboardLayoutError, match="通道|channel"):
        validate_result_dashboard_layout(layout)


def test_validation_rejects_summary_channel_other_than_none():
    layout = _layout(
        _panel("summary", "result_summary", channel="primary_acoustic"),
    )

    with pytest.raises(ResultDashboardLayoutError, match="结果摘要|通道|channel"):
        validate_result_dashboard_layout(layout)


def test_validation_rejects_acoustic_panel_with_vibration_channel():
    layout = _layout(
        _panel("summary", "result_summary"),
        _panel("spectrum", "spectrum", column=1, channel="vibration"),
        columns=2,
    )

    with pytest.raises(ResultDashboardLayoutError, match="通道|channel"):
        validate_result_dashboard_layout(layout)


def test_validation_rejects_vibration_panel_with_acoustic_channel():
    layout = _layout(
        _panel("summary", "result_summary"),
        _panel(
            "vibration",
            "vibration_pulse",
            column=1,
            channel="primary_acoustic",
        ),
        columns=2,
    )

    with pytest.raises(ResultDashboardLayoutError, match="通道|channel"):
        validate_result_dashboard_layout(layout)


def test_selected_2_channel_is_rejected_when_only_one_selected_channel():
    layout = _layout(
        _panel("summary", "result_summary"),
        _panel("spectrum", "spectrum", column=1, channel="selected_2"),
        columns=2,
    )

    with pytest.raises(ResultDashboardLayoutError, match="通道2"):
        validate_result_dashboard_layout(layout, selected_channels=[0])


def test_validation_rejects_panel_span_out_of_bounds():
    layout = _layout(
        _panel("summary", "result_summary"),
        _panel(
            "wide",
            "spectrum",
            column=1,
            column_span=2,
            channel="primary_acoustic",
        ),
        columns=2,
    )

    with pytest.raises(ResultDashboardLayoutError, match="越界|bounds"):
        validate_result_dashboard_layout(layout)


def test_validation_rejects_duplicate_summary_panels():
    layout = _layout(
        _panel("summary_a", "result_summary"),
        _panel("summary_b", "result_summary", column=1),
        columns=2,
    )

    with pytest.raises(ResultDashboardLayoutError, match="一个|exactly one|结果摘要"):
        validate_result_dashboard_layout(layout)


def test_normalization_preserves_panel_order_and_coerces_integer_fields():
    layout = {
        "version": 1,
        "columns": "2",
        "rows": "1",
        "panels": [
            _panel("summary", "result_summary", row="0", column="0"),
            _panel(
                "spectrum",
                "spectrum",
                row="0",
                column="1",
                row_span="1",
                column_span="1",
                channel="primary_acoustic",
            ),
        ],
    }

    normalized = normalize_result_dashboard_layout(layout)

    assert normalized["columns"] == 2
    assert normalized["rows"] == 1
    assert [panel["id"] for panel in normalized["panels"]] == [
        "summary",
        "spectrum",
    ]
    assert normalized["panels"][1]["column"] == 1


def test_resolve_primary_acoustic_uses_first_non_vibration_channel():
    assert (
        resolve_layout_channel(
            "primary_acoustic",
            selected_channels=[1, 0],
            vibration_view_channel_index=1,
            panel_family="acoustic",
        )
        == 0
    )


def test_resolve_vibration_selected_binding_returns_none_for_non_vibration_channel():
    assert (
        resolve_layout_channel(
            "selected_1",
            selected_channels=[0, 1],
            vibration_view_channel_index=1,
            panel_family="vibration",
        )
        is None
    )


def test_resolve_vibration_selected_binding_returns_vibration_channel_when_matched():
    assert (
        resolve_layout_channel(
            "selected_2",
            selected_channels=[0, 1],
            vibration_view_channel_index=1,
            panel_family="vibration",
        )
        == 1
    )


def test_resolve_vibration_binding_returns_none_without_configured_vibration_channel():
    assert (
        resolve_layout_channel(
            "vibration",
            selected_channels=[0],
            vibration_view_channel_index=None,
            panel_family="vibration",
        )
        is None
    )


def test_resolve_vibration_selected_binding_returns_none_without_configured_vibration_channel():
    assert (
        resolve_layout_channel(
            "selected_1",
            selected_channels=[0],
            vibration_view_channel_index=None,
            panel_family="vibration",
        )
        is None
    )
