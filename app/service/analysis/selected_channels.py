"""Selected hardware channel projection helpers."""
from __future__ import annotations

from dataclasses import dataclass
from numbers import Integral
from typing import Iterable

import numpy as np


@dataclass(frozen=True)
class SelectedChannelView:
    buffer: np.ndarray
    source_channels: tuple[int, ...]
    real_to_logical: dict[int, int]


def normalize_selected_channels(
    selected_channels: Iterable[object],
    *,
    hardware_channel_count: int,
) -> tuple[int, ...]:
    """Validate and normalize selected real hardware channel indexes."""
    if isinstance(hardware_channel_count, bool) or not isinstance(
        hardware_channel_count, Integral
    ):
        raise ValueError("hardware_channel_count must be a positive integer")

    hardware_count = int(hardware_channel_count)
    if hardware_count <= 0:
        raise ValueError("hardware_channel_count must be a positive integer")

    try:
        raw_channels = tuple(selected_channels)
    except TypeError as exc:
        raise ValueError(
            "selected_channels must be an iterable of non-bool integer channel indexes"
        ) from exc

    if not raw_channels:
        raise ValueError("selected_channels must contain at least one channel")

    normalized: list[int] = []
    seen: set[int] = set()
    for channel in raw_channels:
        if isinstance(channel, (bool, np.bool_)) or not isinstance(channel, Integral):
            raise ValueError(
                f"selected channel {channel!r} must be a non-bool integer"
            )

        real_channel = int(channel)
        if real_channel < 0:
            raise ValueError(
                f"selected channel {real_channel} must be non-negative"
            )
        if real_channel >= hardware_count:
            raise ValueError(
                f"selected channel {real_channel} is outside hardware channel range "
                f"0..{hardware_count - 1}"
            )
        if real_channel in seen:
            raise ValueError(f"duplicate selected channel {real_channel}")

        seen.add(real_channel)
        normalized.append(real_channel)

    if len(normalized) > 2:
        raise ValueError("selected_channels must contain at most two channels")

    return tuple(normalized)


def project_selected_channels(
    buffer: np.ndarray,
    selected_channels: Iterable[object],
) -> SelectedChannelView:
    """Project a hardware input buffer to selected channels in selected order."""
    array = np.asarray(buffer)
    if array.ndim == 1:
        source_channels = normalize_selected_channels(
            selected_channels,
            hardware_channel_count=1,
        )
        projected = array.reshape(-1, 1).copy()
    elif array.ndim == 2:
        source_channels = normalize_selected_channels(
            selected_channels,
            hardware_channel_count=array.shape[1],
        )
        projected = array[:, source_channels].copy()
    else:
        raise ValueError("buffer must be a 1-D or 2-D array")

    return SelectedChannelView(
        buffer=projected,
        source_channels=source_channels,
        real_to_logical={real: logical for logical, real in enumerate(source_channels)},
    )
