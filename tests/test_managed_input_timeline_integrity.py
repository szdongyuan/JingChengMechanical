from types import SimpleNamespace

import numpy as np
import pytest

from app.service.managed_input_sounddevice_backend import MonotonicSoundDeviceAudioBackend
from app.service.analysis.resident_audio_mvp.contracts import (
    BackendCallbackDirective, BackendCallbackStatus, ResidentStreamSpec,
)
from tests.test_managed_input_sounddevice_backend import _SoundDevice


class Clock:
    monotonic_domain = "python/system-monotonic"
    now_ns = 2_000_000_000

    def monotonic(self):
        return self.now_ns / 1_000_000_000

    def monotonic_ns(self):
        return self.now_ns


def make_handle():
    clock, calls, faults = Clock(), [], []
    handle = MonotonicSoundDeviceAudioBackend(_SoundDevice(), clock).create_input(
        ResidentStreamSpec("managed-input-1", 1, "Input", 0, "ASIO",
                           48_000, 1, (0,), "float32", 480),
        lambda *args: calls.append(args) or BackendCallbackDirective.CONTINUE,
        faults.append,
    )
    return clock, handle, calls, faults


def emit(handle, adc, current=2.0, frames=480):
    info = SimpleNamespace(currentTime=current, inputBufferAdcTime=adc)
    return handle._native_callback(np.zeros((frames, 1), np.float32), frames,
                                   info, BackendCallbackStatus.OK)


@pytest.mark.parametrize("adc, entry", [(2.5, 2_510_000_000), (0.99, 2_010_000_000),
                                        (0.19, 3_200_000_000), (5.0, 2_010_000_000)])
def test_raw_adc_discontinuity_faults_before_clamp_or_rebase(adc, entry):
    clock, handle, calls, faults = make_handle()
    assert emit(handle, 1.99) is BackendCallbackDirective.CONTINUE
    clock.now_ns = entry
    assert emit(handle, adc) is BackendCallbackDirective.STOP
    assert len(calls) == 1
    assert len(faults) == 1
    assert faults[0].status is BackendCallbackStatus.BACKEND_FAULT
    assert "ADC discontinuity" in faults[0].summary


@pytest.mark.parametrize("invalid", [None, float("nan"), float("inf"), 0.0, -1.0])
def test_missing_adc_keeps_sample_timeline_and_validates_resumed_raw_clock(invalid):
    clock, handle, calls, faults = make_handle()
    emit(handle, 1.99)
    clock.now_ns = 2_500_000_000
    emit(handle, invalid)
    emit(handle, 2.01)
    assert [call[2] for call in calls] == [2_000_000_000, 2_010_000_000, 2_020_000_000]
    assert faults == []
    assert emit(handle, 2.5) is BackendCallbackDirective.STOP


def test_burst_delivery_and_bounded_adc_jitter_preserve_exact_sample_positions():
    clock, handle, calls, faults = make_handle()
    emit(handle, 1.99)
    clock.now_ns = 2_500_000_000
    for adc in (2.015, 2.0, 2.02):
        emit(handle, adc)
    assert [call[2] for call in calls] == [2_000_000_000, 2_010_000_000,
                                         2_020_000_000, 2_030_000_000]
    assert faults == []


def test_accumulating_small_adc_skips_fault_within_observation_window():
    clock, handle, calls, faults = make_handle()
    emit(handle, 1.99)
    for index in range(1, 6):
        clock.now_ns += 15_000_000
        directive = emit(handle, 1.99 + index * 0.015)
    assert directive is BackendCallbackDirective.STOP
    assert len(calls) == 5
    assert len(faults) == 1


def test_slow_adc_clock_skew_across_windows_keeps_mapping_bounded():
    clock, handle, calls, faults = make_handle()
    # 100 ppm: ten windows, with 1 ms drift per window, is ordinary clock skew.
    for index in range(10_001):
        adc = 1.99 + index * 0.010001
        clock.now_ns = round((adc + 0.01) * 1e9)
        assert emit(handle, adc, current=adc + 0.01) is BackendCallbackDirective.CONTINUE
        assert abs(calls[-1][2] - clock.now_ns) <= 1_001_000
    assert faults == []


def test_negative_clock_skew_never_emits_future_ingress_timestamp():
    clock, handle, calls, faults = make_handle()
    for index in range(2001):
        adc = 1.99 + index * 0.009999
        clock.now_ns = round((adc + 0.01) * 1e9)
        assert emit(handle, adc, current=adc + 0.01) is BackendCallbackDirective.CONTINUE
        assert calls[-1][2] <= clock.now_ns
    assert faults == []
