from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace

import numpy as np
import pytest

from app.services_container import FactoryControllerState

from ._support import (
    _emit_reviewing,
    _make_module,
    _make_review_payload,
    _show,
)


def _review_with_score_payloads(
    score_payloads: dict[int, dict | None],
    *,
    displayed_config_id: int = 101,
    run_id: str = "run-101",
):
    chart_payload: dict[int, dict] = {}
    for channel_index, score_payload in score_payloads.items():
        channel_payload: dict = {}
        if score_payload is not None:
            channel_payload["stft_score"] = score_payload
        chart_payload[channel_index] = channel_payload
    return _make_review_payload(
        displayed_config_id=displayed_config_id,
        run_id=run_id,
        chart_payload=chart_payload,
    )


def test_current_score_title_defaults_without_score_payload(qapp):
    module, services = _make_module()
    module.selected_display_channel_provider = lambda: 0
    _show(module, qapp)

    assert module.current_score_title() == "得分：--"

    _emit_reviewing(module, services, _review_with_score_payloads({0: None}))
    qapp.processEvents()

    assert module.current_score_title() == "得分：--"


def test_selected_channel_score_payload_updates_title_and_emits(qapp):
    module, services = _make_module()
    module.selected_display_channel_provider = lambda: 1
    emissions: list[str] = []
    module.score_title_changed.connect(emissions.append)
    _show(module, qapp)

    _emit_reviewing(
        module,
        services,
        _review_with_score_payloads(
            {1: {"enabled": True, "summary_score": 87.424}},
        ),
    )
    qapp.processEvents()

    assert module.current_score_title() == "得分：87.42"
    assert emissions[-1] == "得分：87.42"


def test_switching_channel_updates_score_title(qapp):
    module, services = _make_module()
    selected_channel = {"value": 0}
    module.selected_display_channel_provider = lambda: selected_channel["value"]
    emissions: list[str] = []
    module.score_title_changed.connect(emissions.append)
    _show(module, qapp)

    _emit_reviewing(
        module,
        services,
        _review_with_score_payloads(
            {
                0: {"enabled": True, "summary_score": 91.239},
                1: {"enabled": True, "summary_score": 74.0},
            },
        ),
    )
    qapp.processEvents()

    assert module.current_score_title() == "得分：91.24"

    selected_channel["value"] = 1
    module.refresh_selected_display_channel()
    qapp.processEvents()

    assert module.current_score_title() == "得分：74.00"
    assert emissions[-1] == "得分：74.00"


@pytest.mark.parametrize(
    "score_payload",
    [
        {"enabled": False},
        {"enabled": True},
        {"enabled": True, "summary_score": "unavailable"},
    ],
)
def test_disabled_or_unavailable_score_payload_shows_placeholder(
    qapp,
    score_payload,
):
    module, services = _make_module()
    module.selected_display_channel_provider = lambda: 0
    _show(module, qapp)

    _emit_reviewing(module, services, _review_with_score_payloads({0: score_payload}))
    qapp.processEvents()

    assert module.current_score_title() == "得分：--"


@pytest.mark.parametrize("run_phase", ["running", "analyzing"])
def test_running_or_analyzing_clears_previous_completed_score(qapp, run_phase):
    module, services = _make_module()
    module.selected_display_channel_provider = lambda: 0
    emissions: list[str] = []
    module.score_title_changed.connect(emissions.append)
    _show(module, qapp)
    _emit_reviewing(
        module,
        services,
        _review_with_score_payloads({0: {"enabled": True, "summary_score": 88.0}}),
    )
    qapp.processEvents()

    assert module.current_score_title() == "得分：88.00"

    services.bus.factory_state_changed.emit(
        FactoryControllerState(
            displayed_config_id=101,
            run_phase=run_phase,
            trigger_source="manual",
        )
    )
    qapp.processEvents()

    assert module.current_score_title() == "得分：--"
    assert emissions[-1] == "得分：--"


def test_display_reset_clears_score_title(qapp):
    module, services = _make_module()
    module.selected_display_channel_provider = lambda: 0
    _show(module, qapp)
    _emit_reviewing(
        module,
        services,
        _review_with_score_payloads({0: {"enabled": True, "summary_score": 90.0}}),
    )
    qapp.processEvents()

    assert module.current_score_title() == "得分：90.00"

    services.bus.factory_display_reset.emit("selection_changed")
    qapp.processEvents()

    assert module.current_score_title() == "得分：--"


def test_stale_review_payload_is_ignored_until_config_and_run_match(qapp):
    module, services = _make_module()
    module.selected_display_channel_provider = lambda: 0
    _show(module, qapp)

    services.bus.factory_state_changed.emit(
        FactoryControllerState(
            displayed_config_id=101,
            run_phase="running",
            trigger_source="manual",
        )
    )
    module._on_factory_runtime_context(
        SimpleNamespace(config_id=101, run_id="run-current")
    )
    services.bus.factory_state_changed.emit(
        FactoryControllerState(
            displayed_config_id=101,
            run_phase="reviewing",
            trigger_source="manual",
        )
    )

    services.bus.factory_review_changed.emit(
        _review_with_score_payloads(
            {0: {"enabled": True, "summary_score": 55.0}},
            run_id="run-stale",
        )
    )
    qapp.processEvents()

    assert module.current_score_title() == "得分：--"

    services.bus.factory_review_changed.emit(
        _review_with_score_payloads(
            {0: {"enabled": True, "summary_score": 93.333}},
            run_id="run-current",
        )
    )
    qapp.processEvents()

    assert module.current_score_title() == "得分：93.33"

    services.bus.factory_review_changed.emit(
        _review_with_score_payloads(
            {0: {"enabled": True, "summary_score": 10.0}},
            displayed_config_id=202,
            run_id="run-current",
        )
    )
    qapp.processEvents()

    assert module.current_score_title() == "得分：--"


def test_newer_same_config_review_replaces_previous_review_score_context(qapp):
    module, services = _make_module()
    module.selected_display_channel_provider = lambda: 0
    _show(module, qapp)

    _emit_reviewing(
        module,
        services,
        _review_with_score_payloads(
            {0: {"enabled": True, "summary_score": 72.0}},
            run_id="run-old",
        ),
    )
    qapp.processEvents()

    assert module.current_score_title() == "得分：72.00"

    services.bus.factory_review_changed.emit(
        _review_with_score_payloads(
            {0: {"enabled": True, "summary_score": 96.25}},
            run_id="run-new",
        )
    )
    qapp.processEvents()

    assert module.current_score_title() == "得分：96.25"


def test_newer_same_config_imported_review_replaces_previous_review_context(qapp):
    module, services = _make_module()
    module.selected_display_channel_provider = lambda: 0
    _show(module, qapp)

    _emit_reviewing(
        module,
        services,
        _review_with_score_payloads(
            {0: {"enabled": True, "summary_score": 70.0}},
            run_id="run-old",
        ),
    )
    qapp.processEvents()

    imported_review = _review_with_score_payloads(
        {0: {"enabled": True, "summary_score": 98.5}},
        run_id="import-new",
    )
    imported_review = replace(
        imported_review,
        trigger_source="import_audio",
        manual_reanalysis_allowed=False,
        chart_payload={
            0: {
                "stft": {
                    "times_sec": [0.1],
                    "frequencies_hz": [100.0],
                    "magnitude_db": np.asarray([[9.0]]),
                },
                "stft_score": {"enabled": True, "summary_score": 98.5},
            }
        },
    )

    services.bus.factory_review_changed.emit(imported_review)
    qapp.processEvents()

    assert module.current_score_title() == "得分：98.50"
    assert module.current_render_cache_key() == (101, "import-new", 0)
