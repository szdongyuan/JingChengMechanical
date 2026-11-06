import json

import pytest

from app.service.analysis.vibration_requirements import resolve_vibration_requirements
from app.service.result_dashboard_layout import (
    normalize_result_dashboard_layout_for_display,
)


def _layout(*panels):
    return {
        "version": 2, "columns": 1, "rows": max(1, len(panels)),
        "panels": [
            {"id": f"panel-{i}", "type": kind, "channel": binding,
             "row": i, "column": 0, "row_span": 1, "column_span": 1}
            for i, (kind, binding) in enumerate(panels)
        ],
    }


@pytest.mark.parametrize("serialized", [False, True])
@pytest.mark.parametrize(("panels", "expected"), [
    ([("vibration_stft", "vibration")], (False, False)),
    ([("vibration_spectrum", "vibration"), ("vibration_octave", "vibration"),
      ("spl", "primary_acoustic")], (False, False)),
    ([("vibration_pulse", "vibration")], (False, True)),
    ([("vibration_frequency_stability", "vibration")], (True, False)),
    ([("vibration_frequency_stability", "vibration"),
      ("vibration_pulse", "vibration")], (True, True)),
    ([], (False, False)),
    ([("vibration_pulse", "selected_1"),
      ("vibration_frequency_stability", "selected_1")], (False, False)),
    ([("vibration_pulse", "selected_2")], (False, True)),
    ([("vibration_frequency_stability", "selected_2")], (True, False)),
    ([("vibration_pulse", "vibration"),
      ("vibration_pulse", "selected_2")], (False, True)),
])
def test_layout_resolves_only_trends_bound_to_selected_vibration(panels, expected, serialized):
    layout = _layout(*panels)
    # Check the fixture is accepted, rather than silently falling back to defaults.
    assert normalize_result_dashboard_layout_for_display(
        layout, selected_channels=[0, 2]
    ) == layout
    config = {"selected_channels": [0, 2], "vibration_view_channel_index": 2,
              "result_dashboard_layout": json.dumps(layout) if serialized else layout}
    assert resolve_vibration_requirements(config) == expected


@pytest.mark.parametrize("raw", [None, "", "  ", "{broken", {}, [], 42])
def test_missing_blank_or_invalid_layout_preserves_display_default(raw):
    config = {"selected_channels": [0, 2], "vibration_view_channel_index": 2}
    expected = resolve_vibration_requirements({
        **config, "result_dashboard_layout": normalize_result_dashboard_layout_for_display(raw)
    })
    assert expected == (True, True)
    assert resolve_vibration_requirements({**config, "result_dashboard_layout": raw}) == expected
    assert resolve_vibration_requirements(config) == expected


@pytest.mark.parametrize("channel", [None, True, False, -1, "2", 2.0, 1, 3])
def test_invalid_or_unselected_vibration_channel_requests_no_trends(channel):
    assert resolve_vibration_requirements({
        "selected_channels": [0, 2], "vibration_view_channel_index": channel,
    }) == (False, False)


def test_boolean_selected_channel_does_not_validate_vibration_identity():
    assert resolve_vibration_requirements({
        "selected_channels": [True], "vibration_view_channel_index": 1,
    }) == (False, False)
