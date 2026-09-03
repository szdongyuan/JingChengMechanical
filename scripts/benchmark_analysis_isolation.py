"""Measure whether completed-recording analysis stalls other Python threads."""

from __future__ import annotations

import json
import multiprocessing
import threading
import time
import uuid
from pathlib import PureWindowsPath

import numpy as np

from app.service.analysis.process_executor import ProcessIsolatedAnalysisExecutor
from app.service.analysis.production_executor import ProductionAnalysisExecutor
from app.service.analysis.scheduler import (
    AnalysisInputSnapshot,
    AnalysisJob,
    AnalysisKind,
    AudioStorageKind,
    PersistedAudioReference,
)


class _Audio:
    def __init__(self, duration_seconds: int = 8) -> None:
        self.sample_rate_hz = 48_000
        self.channel_count = 2
        self.frame_count = self.sample_rate_hz * duration_seconds
        axis = np.arange(self.frame_count, dtype=np.float64) / self.sample_rate_hz
        signal = (
            0.05 * np.sin(2.0 * np.pi * 1_000.0 * axis)
            + 0.01 * np.sin(2.0 * np.pi * 240.0 * axis)
        ).astype(np.float32)
        self.samples = np.column_stack((signal, signal))

    def close(self) -> None:
        return None


class _Token:
    is_cancelled = False
    reason = None

    @staticmethod
    def wait(timeout_seconds=None) -> bool:
        del timeout_seconds
        return False


def _job(audio: _Audio, *, attempt_id: str | None = None) -> AnalysisJob:
    config = {
        "config_id": 1,
        "selected_channels": [0, 1],
        "input_device_samplerate": float(audio.sample_rate_hz),
        "channels": [
            {"channel_index": 0, "spl_weighting": "A"},
            {"channel_index": 1, "spl_weighting": "A"},
        ],
    }
    snapshot = AnalysisInputSnapshot(
        config_snapshot_json=json.dumps(config),
        calibration_snapshot_json=json.dumps(
            {
                "In1": {"deviation_value": "0.0"},
                "In2": {"deviation_value": "0.0"},
            }
        ),
        workorder_snapshot_json='{"sn":"benchmark"}',
        channel_mapping_json=json.dumps(
            {
                "schema": "audio-channel-map/v1",
                "channel_order": [0, 1],
            }
        ),
        analysis_window_start_seconds=0.0,
        analysis_window_end_seconds=8.0,
        algorithm_revision="benchmark",
    )
    return AnalysisJob(
        run_id="benchmark-run",
        analysis_attempt_id=attempt_id or str(uuid.uuid4()),
        kind=AnalysisKind.AUTOMATIC,
        audio=PersistedAudioReference(
            reference_id="benchmark-audio",
            storage_kind=AudioStorageKind.CAPTURED_FINAL_WAV,
            absolute_path=str(PureWindowsPath("C:/benchmark.wav")),
            sample_rate_hz=audio.sample_rate_hz,
            channel_count=audio.channel_count,
            frame_count=audio.frame_count,
            dtype="float32",
            content_revision="benchmark",
        ),
        snapshot=snapshot,
        execution_timeout_seconds=120.0,
    )


def _measure(label: str, execute) -> dict[str, object]:
    gaps: list[float] = []
    stop = threading.Event()

    def heartbeat() -> None:
        previous = time.perf_counter()
        while not stop.is_set():
            current = time.perf_counter()
            gaps.append(current - previous)
            previous = current
            time.sleep(0.001)

    thread = threading.Thread(target=heartbeat, daemon=True)
    thread.start()
    started = time.perf_counter()
    execute()
    elapsed = time.perf_counter() - started
    stop.set()
    thread.join()
    result = {
        "label": label,
        "elapsed_seconds": elapsed,
        "max_heartbeat_gap_seconds": max(gaps, default=0.0),
        "gaps_over_20ms": sum(gap > 0.02 for gap in gaps),
    }
    print(
        label,
        f"elapsed={result['elapsed_seconds']:.3f}s",
        f"max_heartbeat_gap={result['max_heartbeat_gap_seconds']:.3f}s",
        f"gaps_over_20ms={result['gaps_over_20ms']}",
    )
    return result


def main() -> None:
    multiprocessing.freeze_support()
    audio = _Audio()
    token = _Token()
    measurements = []

    local = ProductionAnalysisExecutor()
    local.execute(_job(audio), audio, token)
    measurements.append(
        _measure(
            "in_process_full",
            lambda: local.execute(_job(audio), audio, token),
        )
    )

    isolated = ProcessIsolatedAnalysisExecutor()
    isolated.start()

    def isolated_decision() -> None:
        job = _job(audio)
        isolated.execute(job, audio, token)
        isolated.discard_attempt(
            job.analysis_attempt_id,
            reason_code="BENCHMARK_DECISION_COMPLETE",
        )

    try:
        measurements.append(_measure("isolated_cold_decision", isolated_decision))
        measurements.append(_measure("isolated_warm_decision", isolated_decision))
    finally:
        isolated.close()
    print(
        json.dumps(
            {
                "schema": "analysis-isolation-benchmark/v2",
                "input": {
                    "duration_seconds": audio.frame_count / audio.sample_rate_hz,
                    "sample_rate_hz": audio.sample_rate_hz,
                    "channel_count": audio.channel_count,
                },
                "measurements": measurements,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
    )


if __name__ == "__main__":
    main()
