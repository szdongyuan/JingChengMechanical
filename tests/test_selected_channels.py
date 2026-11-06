from __future__ import annotations

import numpy as np
import pytest

from app.service.analysis.selected_channels import (
    normalize_selected_channels,
    project_selected_channels,
)


def test_validate_rejects_duplicate_channels():
    with pytest.raises(ValueError, match="duplicate"):
        normalize_selected_channels([1, 1], hardware_channel_count=6)


def test_validate_rejects_out_of_range_channel():
    with pytest.raises(ValueError, match="outside"):
        normalize_selected_channels([1, 6], hardware_channel_count=6)


@pytest.mark.parametrize("channel", [True, False])
def test_validate_rejects_bool_channels(channel):
    with pytest.raises(ValueError, match="non-bool integer"):
        normalize_selected_channels([channel], hardware_channel_count=6)


@pytest.mark.parametrize("channel", [1.0, "1", None])
def test_validate_rejects_non_integer_channels(channel):
    with pytest.raises(ValueError, match="non-bool integer"):
        normalize_selected_channels([channel], hardware_channel_count=6)


def test_validate_rejects_negative_channels():
    with pytest.raises(ValueError, match="non-negative"):
        normalize_selected_channels([-1], hardware_channel_count=6)


def test_validate_rejects_empty_selected_channels():
    with pytest.raises(ValueError, match="at least one"):
        normalize_selected_channels([], hardware_channel_count=6)


def test_validate_rejects_more_than_two_selected_channels():
    with pytest.raises(ValueError, match="at most two|最多"):
        normalize_selected_channels([0, 1, 2], hardware_channel_count=6)


@pytest.mark.parametrize("hardware_channel_count", [0, -1])
def test_validate_rejects_invalid_hardware_channel_count(hardware_channel_count):
    with pytest.raises(ValueError, match="positive"):
        normalize_selected_channels([0], hardware_channel_count=hardware_channel_count)


def test_project_single_real_channel_to_one_logical_column():
    hardware = np.column_stack([np.full(4, i) for i in range(6)]).astype(np.float32)

    view = project_selected_channels(hardware, [3])

    assert view.buffer.shape == (4, 1)
    np.testing.assert_array_equal(view.buffer[:, 0], hardware[:, 3])
    assert view.source_channels == (3,)
    assert view.real_to_logical == {3: 0}


def test_project_preserves_selected_channel_order():
    hardware = np.column_stack([np.full(4, i) for i in range(6)]).astype(np.float32)

    view = project_selected_channels(hardware, [4, 1])

    assert view.buffer.shape == (4, 2)
    np.testing.assert_array_equal(view.buffer[:, 0], hardware[:, 4])
    np.testing.assert_array_equal(view.buffer[:, 1], hardware[:, 1])
    assert view.source_channels == (4, 1)
    assert view.real_to_logical == {4: 0, 1: 1}


def test_project_returns_copy_isolated_from_hardware_buffer_mutation():
    hardware = np.column_stack([np.full(4, i) for i in range(3)]).astype(np.float32)

    view = project_selected_channels(hardware, [2])
    hardware[:, 2] = 99

    np.testing.assert_array_equal(view.buffer[:, 0], np.full(4, 2, dtype=np.float32))


def test_project_treats_one_dimensional_buffer_as_one_hardware_channel():
    hardware = np.arange(4, dtype=np.float32)

    view = project_selected_channels(hardware, [0])

    assert view.buffer.shape == (4, 1)
    np.testing.assert_array_equal(view.buffer[:, 0], hardware)
    assert view.source_channels == (0,)
    assert view.real_to_logical == {0: 0}
