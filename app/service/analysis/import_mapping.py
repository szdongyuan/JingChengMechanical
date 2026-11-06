"""Explicit original-column import maps; v1 retains production channel order."""
from collections.abc import Iterable, Mapping

IMPORT_MAPPING_SCHEMA = "audio-import-map/v2"


def is_import_mapping(mapping: Mapping[str, object]) -> bool:
    return mapping.get("schema") == IMPORT_MAPPING_SCHEMA


def validate_import_mapping(
    mapping: Mapping[str, object], *, selected_channels: Iterable[int] | None = None,
    channel_count: int | None = None,
) -> None:
    if (set(mapping) != {"schema", "source_channel_count", "assignments"}
            or not is_import_mapping(mapping)):
        raise ValueError("invalid explicit import mapping")
    count = mapping["source_channel_count"]
    if (type(count) is not int or count <= 0
            or (channel_count is not None and count != channel_count)):
        raise ValueError("import source channel count differs from archive")
    assignments = mapping["assignments"]
    if not isinstance(assignments, (tuple, list)):
        raise ValueError("import assignments must be an array")
    columns, targets = set(), set()
    active = False
    for row in assignments:
        if (not isinstance(row, Mapping)
                or set(row) != {"wav_channel_index", "mode", "channel_index"}):
            raise ValueError("invalid import assignment")
        column, mode, target = row["wav_channel_index"], row["mode"], row["channel_index"]
        if type(column) is not int or not 0 <= column < count or column in columns:
            raise ValueError("invalid or duplicate original WAV column")
        columns.add(column)
        if mode == "config":
            if type(target) is not int or target < 0 or target in targets:
                raise ValueError("invalid or duplicate mapped configuration channel")
            if selected_channels is not None and target not in selected_channels:
                raise ValueError("mapped target is not a selected configuration channel")
            targets.add(target)
            active = True
        elif mode in ("chart", "ignore") and target is None:
            active |= mode == "chart"
        else:
            raise ValueError("invalid import channel mode")
    if not active:
        raise ValueError("at least one analysis or chart channel is required")


def replay_channel_order(mapping: Mapping[str, object]) -> tuple[int, ...]:
    """Replay IDs for explicit imports are original zero-based WAV columns."""
    if is_import_mapping(mapping):
        validate_import_mapping(mapping)
        return tuple(range(mapping["source_channel_count"]))
    return tuple(mapping["channel_order"])


def shape_import_config(
    config: dict, mapping: Mapping[str, object], sample_rate: float, channel_count: int,
    *, frozen_golden_channels: frozenset[int] = frozenset(),
) -> dict:
    """Reuse legacy import shaping, preserving judgment/chart maps separately."""
    from app.service.analysis.imported_audio import (
        normalize_channel_assignments, _shape_import_configs,
    )
    selected = tuple(config.get("selected_channels", ()))
    validate_import_mapping(mapping, selected_channels=selected, channel_count=channel_count)
    assignments = normalize_channel_assignments(
        mapping["assignments"], wav_channel_count=channel_count,
        allowed_config_channels=selected,
    )
    warnings = []
    shaped = _shape_import_configs(
        source_config=config, assignments=assignments, sample_rate=float(sample_rate),
        warnings=warnings, frozen_golden_channels=frozen_golden_channels,
    )
    shaped["warnings"] = warnings
    return shaped


def import_calibration(
    calibration: dict, mapped_channels: Iterable[int], *, enabled: bool = True,
) -> dict:
    """Freeze only mapped target sensitivities; generated chart IDs never calibrate."""
    from app.service.analysis.imported_audio import _effective_import_mic_cal
    return _effective_import_mic_cal(
        calibration, mapped_config_channels=tuple(mapped_channels),
        use_mic_calibration=enabled, warnings=[],
    )
