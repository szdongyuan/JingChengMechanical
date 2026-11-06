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


def _layout(
    *panels: dict,
    columns: int = 1,
    rows: int = 1,
    version: int = 2,
) -> dict:
    return {
        "version": version,
        "columns": columns,
        "rows": rows,
        "panels": list(panels),
    }


def test_default_layout_is_version_two_chart_only_and_starts_at_origin():
    layout = DEFAULT_RESULT_DASHBOARD_LAYOUT

    assert layout["version"] == 2
    assert layout["columns"] == 3
    assert layout["rows"] == 2
    assert [panel["type"] for panel in layout["panels"]] == [
        "stft",
        "spectrum",
        "octave",
        "vibration_frequency_stability",
        "vibration_pulse",
    ]
    assert layout["panels"][0]["row"] == 0
    assert layout["panels"][0]["column"] == 0
    assert "result_summary" not in PANEL_TITLES
    assert "result_summary" not in PANEL_FAMILIES
    assert PANEL_TITLES["stft"]
    assert PANEL_TITLES["spectrum"]
    assert PANEL_TITLES["octave"]
    assert PANEL_TITLES["spl"]
    assert PANEL_TITLES["vibration_stft"]


def test_blank_layout_normalizes_to_default():
    assert normalize_result_dashboard_layout(None) == DEFAULT_RESULT_DASHBOARD_LAYOUT
    assert normalize_result_dashboard_layout("") == DEFAULT_RESULT_DASHBOARD_LAYOUT
    assert normalize_result_dashboard_layout("  \n\t") == DEFAULT_RESULT_DASHBOARD_LAYOUT


def test_valid_layout_serializes_to_json_string():
    text = serialize_result_dashboard_layout(DEFAULT_RESULT_DASHBOARD_LAYOUT)

    assert isinstance(text, str)
    assert normalize_result_dashboard_layout(text) == DEFAULT_RESULT_DASHBOARD_LAYOUT


def test_malformed_layout_is_rejected_for_save_and_import_paths():
    with pytest.raises(ResultDashboardLayoutError, match="JSON|invalid"):
        normalize_result_dashboard_layout("{bad json")


def test_display_normalizer_falls_back_for_corrupt_runtime_config():
    assert (
        normalize_result_dashboard_layout_for_display("{bad json")
        == DEFAULT_RESULT_DASHBOARD_LAYOUT
    )


def test_panel_families_match_catalog():
    assert PANEL_FAMILIES["spectrum"] == "acoustic"
    assert PANEL_FAMILIES["vibration_pulse"] == "vibration"


def test_normalization_rejects_non_empty_malformed_layout_shape():
    with pytest.raises(ResultDashboardLayoutError, match="layout"):
        normalize_result_dashboard_layout([])


def test_validation_rejects_overlapping_panels():
    layout = _layout(
        _panel("spectrum", "spectrum", channel="primary_acoustic"),
        _panel("copy", "spectrum", channel="selected_1"),
        columns=2,
    )

    with pytest.raises(ResultDashboardLayoutError, match="overlap"):
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
        _panel(
            "spectrum",
            "spectrum",
            row_span=rows,
            channel="primary_acoustic",
        ),
        rows=rows,
    )

    assert validate_result_dashboard_layout(layout) == layout


def test_layout_no_longer_requires_summary_and_allows_empty_panel_list():
    layout = _layout()

    assert validate_result_dashboard_layout(layout) == layout


def test_validation_rejects_duplicate_panel_ids():
    layout = _layout(
        _panel("dup", "spectrum", channel="primary_acoustic"),
        _panel("dup", "spectrum", column=1, channel="selected_1"),
        columns=2,
    )

    with pytest.raises(ResultDashboardLayoutError, match="unique|id"):
        validate_result_dashboard_layout(layout)


def test_validation_rejects_empty_panel_id():
    layout = _layout(_panel("", "spectrum", channel="primary_acoustic"))

    with pytest.raises(ResultDashboardLayoutError, match="id"):
        validate_result_dashboard_layout(layout)


def test_strict_normalize_rejects_version_one():
    layout = _layout(
        _panel("spectrum", "spectrum", channel="primary_acoustic"),
        version=1,
    )

    with pytest.raises(ResultDashboardLayoutError, match="version"):
        normalize_result_dashboard_layout(layout)


def test_strict_normalize_rejects_result_summary_panel_type():
    layout = _layout(_panel("summary", "result_summary"))

    with pytest.raises(ResultDashboardLayoutError, match="panel|type"):
        normalize_result_dashboard_layout(layout)


def test_display_normalize_replaces_entire_legacy_layout_with_v2_default():
    legacy = {
        "version": 1,
        "columns": 2,
        "rows": 1,
        "panels": [
            _panel("summary", "result_summary"),
            _panel(
                "legacy-spectrum",
                "spectrum",
                column=1,
                channel="primary_acoustic",
            ),
        ],
    }

    normalized = normalize_result_dashboard_layout_for_display(legacy)

    assert normalized == DEFAULT_RESULT_DASHBOARD_LAYOUT
    assert normalized["version"] == 2
    assert "legacy-spectrum" not in {
        panel["id"] for panel in normalized["panels"]
    }


@pytest.mark.parametrize(
    ("columns", "rows"),
    [
        (0, 1),
        (4, 1),
        (1, 0),
    ],
)
def test_validation_rejects_invalid_grid_size(columns, rows):
    layout = _layout(
        _panel("spectrum", "spectrum", channel="primary_acoustic"),
        columns=columns,
        rows=rows,
    )

    with pytest.raises(ResultDashboardLayoutError):
        validate_result_dashboard_layout(layout)


def test_validation_rejects_unknown_panel_type():
    layout = _layout(
        _panel("custom", "custom_widget", channel="primary_acoustic"),
    )

    with pytest.raises(ResultDashboardLayoutError, match="panel type"):
        validate_result_dashboard_layout(layout)


def test_validation_rejects_invalid_channel_key():
    layout = _layout(_panel("spectrum", "spectrum", channel="bad_channel"))

    with pytest.raises(ResultDashboardLayoutError, match="channel"):
        validate_result_dashboard_layout(layout)


def test_validation_rejects_acoustic_panel_with_vibration_channel():
    layout = _layout(_panel("spectrum", "spectrum", channel="vibration"))

    with pytest.raises(ResultDashboardLayoutError, match="channel"):
        validate_result_dashboard_layout(layout)


def test_validation_rejects_vibration_panel_with_acoustic_channel():
    layout = _layout(
        _panel(
            "vibration",
            "vibration_pulse",
            channel="primary_acoustic",
        ),
    )

    with pytest.raises(ResultDashboardLayoutError, match="channel"):
        validate_result_dashboard_layout(layout)


def test_selected_2_channel_is_rejected_when_only_one_selected_channel():
    layout = _layout(_panel("spectrum", "spectrum", channel="selected_2"))

    with pytest.raises(ResultDashboardLayoutError):
        validate_result_dashboard_layout(layout, selected_channels=[0])


def test_validation_rejects_panel_span_out_of_bounds():
    layout = _layout(
        _panel(
            "wide",
            "spectrum",
            column_span=2,
            channel="primary_acoustic",
        ),
    )

    with pytest.raises(ResultDashboardLayoutError, match="bounds"):
        validate_result_dashboard_layout(layout)


def test_normalization_preserves_panel_order_and_coerces_integer_fields():
    layout = {
        "version": 2,
        "columns": "2",
        "rows": "1",
        "panels": [
            _panel(
                "spectrum",
                "spectrum",
                row="0",
                column="0",
                channel="primary_acoustic",
            ),
            _panel(
                "spectrum-selected",
                "spectrum",
                row="0",
                column="1",
                row_span="1",
                column_span="1",
                channel="selected_1",
            ),
        ],
    }

    normalized = normalize_result_dashboard_layout(layout)

    assert normalized["columns"] == 2
    assert normalized["rows"] == 1
    assert [panel["id"] for panel in normalized["panels"]] == [
        "spectrum",
        "spectrum-selected",
    ]
    assert normalized["panels"][1]["column"] == 1


def test_normalization_does_not_mutate_input():
    layout = _layout(
        _panel("spectrum", "spectrum", channel="primary_acoustic"),
    )

    normalized = normalize_result_dashboard_layout(layout)
    normalized["panels"][0]["id"] = "changed"

    assert layout["panels"][0]["id"] == "spectrum"


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
