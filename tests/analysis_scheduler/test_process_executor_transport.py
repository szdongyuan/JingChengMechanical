from __future__ import annotations

from types import SimpleNamespace
import threading
import zlib

import pytest

from app.service.analysis import process_executor as process_executor_module
from app.service.analysis.process_executor import (
    ProcessIsolatedAnalysisExecutor,
    _pack_result_for_transport,
)
from app.service.analysis.scheduler import (
    AnalysisExecutionResult,
    AnalysisExecutionStatus,
    FrozenResultObject,
)


class _FakeConnection:
    def __init__(self) -> None:
        self.entered = threading.Event()
        self.release = threading.Event()
        self._ready_delivered = False
        self.closed = False

    def send(self, message) -> None:
        if message[0] == "execute":
            self.entered.set()

    def poll(self, _timeout: float) -> bool:
        if not self._ready_delivered:
            return True
        return self.release.is_set()

    def recv(self):
        if not self._ready_delivered:
            self._ready_delivered = True
            return "ready", None
        return (
            "result",
            AnalysisExecutionResult(
                AnalysisExecutionStatus.NO_RESULT,
                None,
            ),
        )

    def close(self) -> None:
        self.closed = True


class _FakeChildConnection:
    def close(self) -> None:
        return None


class _FakeProcess:
    def __init__(self) -> None:
        self.started = False
        self.alive = True

    def start(self) -> None:
        self.started = True

    def is_alive(self) -> bool:
        return self.alive

    def join(self, _timeout: float) -> None:
        self.alive = False

    def terminate(self) -> None:
        self.alive = False


class _FakeProcessContext:
    def __init__(self) -> None:
        self.connections: list[_FakeConnection] = []
        self.processes: list[_FakeProcess] = []

    def Pipe(self, *, duplex: bool):
        assert duplex is True
        connection = _FakeConnection()
        self.connections.append(connection)
        return connection, _FakeChildConnection()

    def Process(self, **_kwargs):
        process = _FakeProcess()
        self.processes.append(process)
        return process
from app.service.run_persistence import (
    Judgment,
    decode_analysis_result_payload,
)


def test_process_transport_returns_canonical_result_without_float_tree() -> None:
    source = FrozenResultObject(
        (
            ("judgment", "PASS"),
            (
                "result",
                FrozenResultObject(
                    (
                        ("stft", tuple(float(index) for index in range(20_000))),
                        ("summary", "ok"),
                    )
                ),
            ),
        )
    )

    packed = _pack_result_for_transport(
        AnalysisExecutionResult(
            AnalysisExecutionStatus.SUCCEEDED,
            source,
        )
    )
    decoded = decode_analysis_result_payload(packed.result_payload)

    assert decoded.judgment is Judgment.PASS
    assert '"summary":"ok"' in decoded.result_payload_json
    assert decoded.result_payload_zlib is not None
    assert zlib.decompress(decoded.result_payload_zlib).decode("utf-8") == (
        decoded.result_payload_json
    )
    assert len(packed.result_payload.items) == 3
    assert len(dict(packed.result_payload.items)["chunks"]) < 10


def test_default_process_pool_runs_second_call_before_first_finishes(
    monkeypatch,
) -> None:
    context = _FakeProcessContext()
    monkeypatch.setattr(
        process_executor_module.multiprocessing,
        "get_context",
        lambda method: context if method == "spawn" else None,
    )
    executor = ProcessIsolatedAnalysisExecutor()
    audio = SimpleNamespace(
        samples=(0.0,),
        sample_rate_hz=48_000,
        channel_count=1,
        frame_count=1,
    )
    results = []
    errors = []

    def execute() -> None:
        try:
            results.append(executor.execute(object(), audio, object()))
        except BaseException as exc:
            errors.append(exc)

    executor.start()
    first = threading.Thread(target=execute)
    second = threading.Thread(target=execute)
    first.start()
    assert context.connections[0].entered.wait(1.0)
    second.start()
    assert context.connections[1].entered.wait(1.0)
    assert first.is_alive()

    for connection in context.connections:
        connection.release.set()
    first.join(1.0)
    second.join(1.0)
    executor.close()

    assert errors == []
    assert len(results) == 2
    assert len(context.processes) == 2
    assert all(process.started for process in context.processes)


@pytest.mark.parametrize("process_count", [0, 7, True, 1.5, "2"])
def test_process_pool_rejects_invalid_process_count(process_count) -> None:
    with pytest.raises((TypeError, ValueError)):
        ProcessIsolatedAnalysisExecutor(process_count=process_count)
