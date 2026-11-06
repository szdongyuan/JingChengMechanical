from __future__ import annotations

from app.service.result_dashboard_layout import (
    PANEL_FAMILY_VIBRATION,
    PANEL_VIBRATION_FREQUENCY_STABILITY,
    PANEL_VIBRATION_PULSE,
    normalize_result_dashboard_layout_for_display,
    resolve_layout_channel,
)


def configured_vibration_channel(config: dict) -> int | None:
    value = config.get("vibration_view_channel_index")
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        return None
    selected = {
        int(channel)
        for channel in config.get("selected_channels", [])
        if isinstance(channel, int) and not isinstance(channel, bool) and channel >= 0
    }
    return value if value in selected else None


def resolve_vibration_requirements(config: dict) -> tuple[bool, bool]:
    """Return frequency and pulse trend demand from the frozen display layout."""
    vibration_channel = configured_vibration_channel(config)
    if vibration_channel is None:
        return False, False
    selected = config.get("selected_channels", [])
    layout = normalize_result_dashboard_layout_for_display(
        config.get("result_dashboard_layout"), selected_channels=selected,
    )
    frequency = pulse = False
    for panel in layout["panels"]:
        panel_type = panel["type"]
        if panel_type not in (PANEL_VIBRATION_FREQUENCY_STABILITY, PANEL_VIBRATION_PULSE):
            continue
        channel = resolve_layout_channel(
            panel["channel"], selected_channels=selected,
            vibration_view_channel_index=vibration_channel,
            panel_family=PANEL_FAMILY_VIBRATION,
        )
        if channel == vibration_channel:
            frequency |= panel_type == PANEL_VIBRATION_FREQUENCY_STABILITY
            pulse |= panel_type == PANEL_VIBRATION_PULSE
    return frequency, pulse
