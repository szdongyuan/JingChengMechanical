"""Production-shaped, machine-readable two-stage analysis benchmark."""

from __future__ import annotations

import json
import os
import platform
import argparse
import hashlib
import importlib.metadata
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import queue
import threading
import time
import uuid
import wave
import zlib
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import numpy as np

from app.service.analysis.performance_diagnostics import (
    BENCHMARK_DURATION_FIELDS,
    BENCHMARK_SIZE_FIELDS,
    BENCHMARK_TIMESTAMP_FIELDS,
    AnalysisTimingBoundary,
    benchmark_percentiles,
    validate_benchmark_report,
)


NAMED_SLA_CPU_FRAGMENT = "AMD Ryzen 5 5600GT"
NAMED_SLA_LOGICAL_PROCESSORS = 12
NAMED_SLA_MIN_MEMORY_BYTES = 30 * 1024**3


def evaluate_sla_host(host: dict) -> dict[str, object]:
    blockers: list[str] = []
    if platform.system() == "Windows" and host.get("os") != "Windows":
        blockers.append("named SLA host must run Windows")
    if NAMED_SLA_CPU_FRAGMENT.lower() not in str(host.get("processor", "")).lower():
        blockers.append(f"CPU is not {NAMED_SLA_CPU_FRAGMENT}")
    if host.get("os") != "Windows" or host.get("os_release") != "11":
        blockers.append("named SLA host must run Windows 11")
    if host.get("cpu_physical_cores") != 6:
        blockers.append("CPU physical core count differs from named host")
    if host.get("cpu_logical_processors") != NAMED_SLA_LOGICAL_PROCESSORS:
        blockers.append("CPU logical processor count differs from named host")
    max_clock = host.get("cpu_max_clock_mhz")
    if type(max_clock) is not int or not 3_400 <= max_clock <= 3_800:
        blockers.append("CPU max clock differs from the named 3.6 GHz host")
    memory = host.get("total_memory_bytes")
    if type(memory) is not int or memory < NAMED_SLA_MIN_MEMORY_BYTES:
        blockers.append("memory is below the named 31 GB host")
    qt_platform = str(host.get("qt_platform", "")).lower()
    if qt_platform in {"offscreen", "minimal", "minimalegl", "not-running"}:
        blockers.append(f"Qt platform {qt_platform or 'unknown'} is functional-only")
    if host.get("interactive_desktop") is not True:
        blockers.append("interactive desktop is unavailable")
    if host.get("result_window_shown") is not True:
        blockers.append("result window was not shown")
    if host.get("result_window_minimized") is not False:
        blockers.append("result window is minimized")
    disk_evidence = " ".join(
        str(host.get(field, "")).lower()
        for field in ("disk_model", "disk_media_type", "disk_bus_type")
    )
    if "nvme" not in disk_evidence and "cn600" not in disk_evidence:
        blockers.append("NVMe storage evidence is unavailable")
    return {"eligible": not blockers, "blockers": blockers}


def write_json_report(path: Path, report: dict) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(
        report,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        indent=2,
    )
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(payload + "\n", encoding="utf-8")
    os.replace(temporary, path)


def _milliseconds(value_ns: int) -> str:
    return f"{value_ns / 1_000_000:.1f}ms"


def format_console_summary(report: dict) -> str:
    lines = [f"two-stage-analysis SLA={report['sla']['status']}"]
    for name, scenario in report.get("scenarios", {}).items():
        iterations = scenario.get("iterations", [])
        scenario_summary = scenario.get("summary", {})
        if not isinstance(iterations, list) or not iterations:
            lines.append(f"{name}: {scenario.get('status', 'NOT_MEASURED')}")
            continue

        def render(label: str, stats: dict, target_ns: int | None = None) -> str:
            text = (
                f"{label} p50={_milliseconds(stats['p50_ns'])} "
                f"p95={_milliseconds(stats['p95_ns'])} "
                f"max={_milliseconds(stats['max_ns'])}"
            )
            if target_ns is not None:
                outcome = "PASS" if stats["max_ns"] <= target_ns else "MISS"
                text += f" target<={_milliseconds(target_ns)} {outcome}"
            return text

        endpoint_parts = []
        visible_values = []
        for iteration in iterations:
            timestamps = iteration.get("timestamps_ns", {})
            frozen = timestamps.get("audio_frozen_ns")
            painted = timestamps.get("decision_painted_ns")
            if type(frozen) is int and type(painted) is int and painted >= frozen:
                visible_values.append(painted - frozen)
        if visible_values:
            endpoint_parts.append(
                render(
                    "decision_visible",
                    benchmark_percentiles(visible_values),
                    500_000_000,
                )
            )
        authoritative = scenario_summary.get("decision_endpoint_ns")
        if isinstance(authoritative, dict):
            endpoint_parts.append(
                render("decision_authoritative", authoritative, 500_000_000)
            )
        charts = scenario_summary.get("charts_endpoint_ns")
        if isinstance(charts, dict):
            endpoint_parts.append(render("charts", charts, 1_500_000_000))
        lines.append(
            f"{name} endpoints n={len(iterations)}: " + "; ".join(endpoint_parts)
        )

        stage_parts = [
            render(field.removesuffix("_ns"), stats)
            for field, stats in sorted(scenario_summary.items())
            if field not in {"decision_endpoint_ns", "charts_endpoint_ns"}
            and isinstance(stats, dict)
        ]
        if stage_parts:
            lines.append(f"{name} stages: " + "; ".join(stage_parts))
    blockers = report.get("sla", {}).get("blockers", [])
    if blockers:
        lines.append("blockers: " + "; ".join(str(item) for item in blockers))
    return "\n".join(lines)


def _utc_now() -> str:
    return (
        datetime.now(timezone.utc)
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )


def _sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _canonical_json(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _dependency_versions() -> dict[str, str | None]:
    names = (
        "numpy",
        "scipy",
        "PyQt6",
        "pyqtgraph",
        "peewee",
        "python-snap7",
        "mosqito",
    )
    versions: dict[str, str | None] = {}
    for name in names:
        try:
            versions[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            versions[name] = None
    return versions


def _powershell_json(script: str) -> object | None:
    if os.name != "nt":
        return None
    try:
        completed = subprocess.run(
            [
                "powershell",
                "-NoProfile",
                "-NonInteractive",
                "-Command",
                script,
            ],
            check=True,
            capture_output=True,
            text=True,
            timeout=5.0,
        )
        text = completed.stdout.strip()
        return json.loads(text) if text else None
    except Exception:
        return None


def _power_mode() -> str | None:
    if os.name != "nt":
        return None
    try:
        completed = subprocess.run(
            ["powercfg", "/getactivescheme"],
            check=True,
            capture_output=True,
            text=True,
            timeout=5.0,
        )
    except Exception:
        return None
    return completed.stdout.strip() or None


def collect_host_fingerprint() -> dict[str, object]:
    cpu = _powershell_json(
        "Get-CimInstance Win32_Processor | Select-Object -First 1 "
        "Name,NumberOfCores,NumberOfLogicalProcessors,MaxClockSpeed | "
        "ConvertTo-Json -Compress"
    )
    computer = _powershell_json(
        "Get-CimInstance Win32_ComputerSystem | Select-Object -First 1 "
        "TotalPhysicalMemory | ConvertTo-Json -Compress"
    )
    disks = _powershell_json(
        "Get-PhysicalDisk | Select-Object FriendlyName,MediaType,BusType,Size | "
        "ConvertTo-Json -Compress"
    )
    if not isinstance(cpu, dict):
        cpu = {}
    if not isinstance(computer, dict):
        computer = {}
    disk_rows = disks if isinstance(disks, list) else [disks] if isinstance(disks, dict) else []
    disk = disk_rows[0] if disk_rows else {}
    logical = cpu.get("NumberOfLogicalProcessors") or os.cpu_count()
    memory = computer.get("TotalPhysicalMemory")
    try:
        memory = int(memory)
    except (TypeError, ValueError):
        memory = 0
    return {
        "hostname": platform.node(),
        "os": platform.system(),
        "os_release": platform.release(),
        "os_version": platform.version(),
        "architecture": platform.machine(),
        "processor": str(cpu.get("Name") or platform.processor()),
        "cpu_physical_cores": int(cpu.get("NumberOfCores") or 0),
        "cpu_logical_processors": int(logical or 0),
        "cpu_max_clock_mhz": int(cpu.get("MaxClockSpeed") or 0),
        "total_memory_bytes": memory,
        "disk_model": str(
            disk.get("FriendlyName") or disk.get("Model") or "unknown"
        ),
        "disk_media_type": str(disk.get("MediaType") or "unknown"),
        "disk_bus_type": str(disk.get("BusType") or "unknown"),
        "disk_size_bytes": int(disk.get("Size") or 0),
        "python_version": platform.python_version(),
        "python_executable": sys.executable,
        "dependencies": _dependency_versions(),
        "power_mode": _power_mode(),
        "qt_platform": "not-running",
        "interactive_desktop": False,
        "result_window_shown": False,
        "result_window_minimized": False,
    }


def _main_workspace_root(worktree: Path) -> Path:
    try:
        completed = subprocess.run(
            ["git", "rev-parse", "--git-common-dir"],
            cwd=worktree,
            check=True,
            capture_output=True,
            text=True,
            timeout=5.0,
        )
        common = Path(completed.stdout.strip())
        if not common.is_absolute():
            common = (worktree / common).resolve()
        return common.parent
    except Exception:
        return worktree


@dataclass(frozen=True)
class _AudioInput:
    path: Path
    samples: np.ndarray
    sample_rate_hz: int
    channel_count: int
    frame_count: int
    dtype: str
    source: str
    source_sha256: str

    @property
    def duration_seconds(self) -> float:
        return self.frame_count / self.sample_rate_hz

    def report(self) -> dict[str, object]:
        return {
            "source": self.source,
            "path": str(self.path),
            "sha256": _sha256_file(self.path),
            "source_sha256": self.source_sha256,
            "duration_seconds": self.duration_seconds,
            "sample_rate_hz": self.sample_rate_hz,
            "channel_count": self.channel_count,
            "frame_count": self.frame_count,
            "dtype": self.dtype,
        }


def _read_pcm16_wav(path: Path, *, source: str) -> _AudioInput:
    with wave.open(str(path), "rb") as reader:
        if reader.getcomptype() != "NONE" or reader.getsampwidth() != 2:
            raise ValueError("benchmark input must be uncompressed PCM16 WAV")
        rate = int(reader.getframerate())
        channels = int(reader.getnchannels())
        frames = int(reader.getnframes())
        payload = reader.readframes(frames)
    expected = frames * channels * 2
    if len(payload) != expected:
        raise ValueError("benchmark WAV payload is truncated")
    samples = np.frombuffer(payload, dtype="<i2").reshape(frames, channels).copy()
    return _AudioInput(
        path=path.resolve(),
        samples=samples,
        sample_rate_hz=rate,
        channel_count=channels,
        frame_count=frames,
        dtype="<i2",
        source=source,
        source_sha256=_sha256_file(path),
    )


def _write_two_channel_profile(source: _AudioInput, target: Path) -> _AudioInput:
    target_rate = 48_000
    target_frames = 8 * target_rate
    mono = source.samples[:, 0].astype(np.float64)
    source_positions = np.arange(mono.size, dtype=np.float64) / source.sample_rate_hz
    target_positions = np.arange(target_frames, dtype=np.float64) / target_rate
    left = np.interp(target_positions, source_positions, mono)
    right = np.interp(target_positions, source_positions, mono * 0.985)
    samples = np.column_stack((left, right))
    samples = np.clip(np.rint(samples), -32768, 32767).astype("<i2")
    with wave.open(str(target), "wb") as writer:
        writer.setnchannels(2)
        writer.setsampwidth(2)
        writer.setframerate(target_rate)
        writer.writeframes(samples.tobytes(order="C"))
    return _AudioInput(
        path=target.resolve(),
        samples=samples,
        sample_rate_hz=target_rate,
        channel_count=2,
        frame_count=target_frames,
        dtype="<i2",
        source="production_wav_resampled_8s_48khz_two_channel",
        source_sha256=source.source_sha256,
    )


def _load_production_facts(database_path: Path, config_id: int):
    os.environ["APP_DB_PATH"] = str(database_path.resolve())
    from app.services_container import EventBus
    from app.service.ai_service import AiService
    from app.service.analysis.scheduler import (
        AiToolchainSnapshot,
        canonical_toolchain_lane_key,
    )
    from app.service.config_service import ConfigService
    from model.base_mode import db

    bus = EventBus()
    db.connect(reuse_if_open=True)
    try:
        config = ConfigService(bus).load_config(config_id)
        frozen_toolchain = AiService().freeze_bound_toolchain(config_id)
    finally:
        if not db.is_closed():
            db.close()
    with closing(sqlite3.connect(database_path)) as connection:
        hardware_id = config.get("input_hardware_device_id")
        row = connection.execute(
            "SELECT mic_cal FROM input_hardware_table WHERE hardware_id = ?",
            (hardware_id,),
        ).fetchone()
    if row is None:
        raise ValueError("production calibration row is unavailable")
    calibration = json.loads(row[0])
    toolchain = None
    if frozen_toolchain is not None:
        toolchain = AiToolchainSnapshot(
            toolchain_id=frozen_toolchain["toolchain_id"],
            runtime_lane_key=canonical_toolchain_lane_key(
                frozen_toolchain["toolchain_id"]
            ),
            execution_revision=frozen_toolchain["execution_revision"],
            execution_mode=frozen_toolchain["execution_mode"],
            entrypoint=frozen_toolchain["entrypoint"],
            package_dir=frozen_toolchain["package_dir"],
            params_json=_canonical_json(frozen_toolchain["params"]),
            timeout_seconds=float(frozen_toolchain["timeout_seconds"]),
        )
    return config, calibration, toolchain, frozen_toolchain


def _scenario_config(
    production_config: dict,
    audio: _AudioInput,
    *,
    two_channel: bool,
) -> dict:
    config = json.loads(json.dumps(production_config, ensure_ascii=False))
    selected = [0, 1] if two_channel else [0]
    rows = {
        row.get("channel_index"): row
        for row in config.get("channels", [])
        if isinstance(row, dict)
    }
    if not set(selected).issubset(rows):
        raise ValueError("production config lacks channels required by scenario")
    config["selected_channels"] = selected
    config["channels"] = [rows[index] for index in selected]
    config["input_device_samplerate"] = float(audio.sample_rate_hz)
    if two_channel:
        config["record_duration"] = 8.0
        config["analysis_duration"] = 8.0
        config["analysis_window_offset_ms"] = 0
        config["result_dashboard_layout"] = {
            "version": 2,
            "columns": 3,
            "rows": 2,
            "panels": [
                {
                    "id": "stft_acoustic",
                    "type": "stft",
                    "row": 0,
                    "column": 0,
                    "row_span": 1,
                    "column_span": 1,
                    "channel": "selected_1",
                },
                {
                    "id": "spectrum_acoustic",
                    "type": "spectrum",
                    "row": 0,
                    "column": 1,
                    "row_span": 1,
                    "column_span": 1,
                    "channel": "selected_1",
                },
                {
                    "id": "octave_acoustic",
                    "type": "octave",
                    "row": 0,
                    "column": 2,
                    "row_span": 1,
                    "column_span": 1,
                    "channel": "selected_1",
                },
            ],
        }
    return config


class _LoadedAudio:
    def __init__(self, audio: _AudioInput) -> None:
        self.samples = audio.samples
        self.sample_rate_hz = audio.sample_rate_hz
        self.channel_count = audio.channel_count
        self.frame_count = audio.frame_count

    def close(self) -> None:
        return None


class _CancelToken:
    is_cancelled = False
    reason = None

    @staticmethod
    def wait(timeout_seconds=None) -> bool:
        del timeout_seconds
        return False


class _LegacyResidentRuntime:
    """Functional-only adapter for the deployed pre-readiness JSONL runner."""

    def __init__(self, toolchain) -> None:
        command = [toolchain.entrypoint.strip('"')]
        self._process = subprocess.Popen(
            command,
            cwd=toolchain.package_dir,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
        )
        self._stdout: queue.Queue[object] = queue.Queue()
        self._stderr: list[str] = []
        threading.Thread(target=self._drain_stdout, daemon=True).start()
        threading.Thread(target=self._drain_stderr, daemon=True).start()

    def _drain_stdout(self) -> None:
        stream = self._process.stdout
        assert stream is not None
        for line in stream:
            self._stdout.put(line.rstrip("\r\n"))
        self._stdout.put(EOFError("legacy Resident stdout closed"))

    def _drain_stderr(self) -> None:
        stream = self._process.stderr
        assert stream is not None
        for line in stream:
            self._stderr.append(line.rstrip("\r\n"))
            del self._stderr[:-20]

    def analyze(self, **kwargs) -> dict:
        stdin = self._process.stdin
        if stdin is None or self._process.poll() is not None:
            raise RuntimeError("legacy Resident process is unavailable")
        stdin.write(
            json.dumps(
                {
                    "message_type": "analyze",
                    "request_id": kwargs["request_id"],
                    "request": kwargs["request_payload"],
                    "artifacts_dir": kwargs["artifacts_dir"],
                },
                ensure_ascii=False,
            )
            + "\n"
        )
        stdin.flush()
        try:
            line = self._stdout.get(timeout=kwargs["timeout_ms"] / 1000.0)
        except queue.Empty as exc:
            raise TimeoutError("legacy Resident analysis timed out") from exc
        if isinstance(line, Exception):
            raise line
        try:
            envelope = json.loads(line)
            response = envelope["response"]
        except (KeyError, TypeError, ValueError) as exc:
            raise RuntimeError("legacy Resident returned invalid JSONL") from exc
        if (
            envelope.get("message_type") != "result"
            or envelope.get("request_id") != kwargs["request_id"]
            or not isinstance(response, dict)
        ):
            raise RuntimeError("legacy Resident response identity differs")
        return {
            "module_status": "success",
            "response_payload": response,
            "stderr_excerpt": "\n".join(self._stderr[-20:]),
        }

    def shutdown_all(self) -> None:
        if self._process.poll() is not None:
            return
        try:
            stdin = self._process.stdin
            if stdin is not None:
                stdin.write('{"message_type":"shutdown"}\n')
                stdin.flush()
            self._process.wait(timeout=3.0)
        except Exception:
            self._process.kill()
            self._process.wait(timeout=3.0)


def _supports_readiness_protocol(toolchain, timeout_seconds: float = 5.0) -> bool:
    process = subprocess.Popen(
        [toolchain.entrypoint.strip('"')],
        cwd=toolchain.package_dir,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
    )
    lines: queue.Queue[object] = queue.Queue()

    def read_one() -> None:
        try:
            assert process.stdout is not None
            lines.put(process.stdout.readline())
        except Exception as exc:
            lines.put(exc)

    threading.Thread(target=read_one, daemon=True).start()
    try:
        assert process.stdin is not None
        process.stdin.write(
            json.dumps(
                {
                    "message_type": "ready",
                    "protocol_version": 1,
                    "execution_revision": toolchain.execution_revision,
                }
            )
            + "\n"
        )
        process.stdin.flush()
        try:
            line = lines.get(timeout=timeout_seconds)
        except queue.Empty:
            return False
        if not isinstance(line, str) or not line:
            return False
        response = json.loads(line)
        return (
            response.get("message_type") == "ready"
            and response.get("protocol_version") == 1
            and response.get("execution_revision")
            == toolchain.execution_revision
        )
    except Exception:
        return False
    finally:
        if process.poll() is None:
            try:
                assert process.stdin is not None
                process.stdin.write('{"message_type":"shutdown"}\n')
                process.stdin.flush()
                process.wait(timeout=2.0)
            except Exception:
                process.kill()
                process.wait(timeout=2.0)


class _DurableCommitProbe:
    """FULL-sync SQLite commit against a temporary copy of production DB."""

    def __init__(self, production_database: Path, directory: Path) -> None:
        self.path = directory / "benchmark-run-v2.db"
        shutil.copy2(production_database, self.path)
        with closing(sqlite3.connect(self.path)) as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA synchronous=FULL")
            connection.execute(
                "CREATE TABLE IF NOT EXISTS benchmark_decision_commit ("
                "attempt_id TEXT PRIMARY KEY, judgment TEXT NOT NULL, "
                "payload_json TEXT NOT NULL, committed_ns INTEGER NOT NULL)"
            )
            connection.commit()

    def commit(self, attempt_id: str, judgment: str, payload: str) -> int:
        started = time.perf_counter_ns()
        with closing(sqlite3.connect(self.path)) as connection:
            connection.execute("PRAGMA synchronous=FULL")
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "INSERT INTO benchmark_decision_commit VALUES (?, ?, ?, ?)",
                (attempt_id, judgment, payload, started),
            )
            connection.commit()
        return time.perf_counter_ns()


class _EnrichmentCollector:
    def __init__(self) -> None:
        self._condition = threading.Condition()
        self._reports: dict[str, tuple[object, int]] = {}

    def __call__(self, report) -> None:
        attempt_id = report.identity.analysis_attempt_id
        with self._condition:
            self._reports[attempt_id] = (report, time.perf_counter_ns())
            self._condition.notify_all()

    def wait(self, attempt_id: str, timeout_seconds: float = 15.0):
        deadline = time.monotonic() + timeout_seconds
        with self._condition:
            while attempt_id not in self._reports:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("enrichment delivery timed out")
                self._condition.wait(remaining)
            return self._reports.pop(attempt_id)


class _ChartPaintMonitor:
    def __init__(self, dashboard) -> None:
        from PyQt6.QtCore import QEvent, QObject

        class _Filter(QObject):
            def __init__(self, owner) -> None:
                super().__init__()
                self.owner = owner

            def eventFilter(self, watched, event):
                if event.type() == QEvent.Type.Paint:
                    self.owner._painted(watched)
                return False

        self._dashboard = dashboard
        self._filter = _Filter(self)
        self._watched: dict[object, tuple[str, object]] = {}
        self._expected_ids: set[str] = set()
        self._painted_at: dict[str, int] = {}
        self._armed = False

    def arm(self) -> None:
        self.close()
        for panel_id, card in self._dashboard.cards_by_panel_id().items():
            panel = self._dashboard.panel_widget(panel_id)
            if panel is None:
                continue
            if callable(getattr(panel, "has_committed_plot_data", None)):
                from PyQt6.QtWidgets import QWidget

                self._expected_ids.add(panel_id)
                watched = (panel, *panel.findChildren(QWidget))
                for widget in watched:
                    widget.installEventFilter(self._filter)
                    self._watched[widget] = (panel_id, panel)
        self._painted_at.clear()
        self._armed = True

    def _painted(self, panel) -> None:
        if not self._armed or panel not in self._watched:
            return
        panel_id, owner = self._watched[panel]
        try:
            committed = owner.has_committed_plot_data()
        except Exception:
            return
        if committed is True:
            self._painted_at.setdefault(
                panel_id, time.perf_counter_ns()
            )

    @property
    def complete(self) -> bool:
        return bool(self._expected_ids) and set(self._painted_at) == self._expected_ids

    @property
    def painted_at_ns(self) -> int | None:
        return max(self._painted_at.values()) if self.complete else None

    def close(self) -> None:
        for panel in tuple(self._watched):
            try:
                panel.removeEventFilter(self._filter)
            except Exception:
                pass
        self._watched.clear()
        self._expected_ids.clear()
        self._armed = False


class _QtEndpoint:
    def __init__(self, config: dict) -> None:
        from PyQt6.QtWidgets import QApplication, QMainWindow
        from app.services_container import (
            EventBus,
            FactoryControllerState,
            FactoryReviewPayload,
        )
        from app.service.multi_run_integration.history_qt_adapter import HistoryQtAdapter
        from ui.modules.result_dashboard import ResultDashboardModule

        self.app = QApplication.instance() or QApplication([])

        class _Config:
            def __init__(self, frozen: dict) -> None:
                self.frozen = frozen

            def load_config(self, config_id: int) -> dict:
                if config_id != self.frozen.get("config_id"):
                    raise KeyError(config_id)
                return json.loads(json.dumps(self.frozen, ensure_ascii=False))

            load_config_for_result_dashboard = load_config

        factory_mode = SimpleNamespace(
            state=FactoryControllerState(
                selected_config_ids=(config["config_id"],),
                displayed_config_id=config["config_id"],
                run_phase="reviewing",
                trigger_source="manual",
            ),
            review_payload=FactoryReviewPayload(),
        )
        services = SimpleNamespace(
            bus=EventBus(),
            factory_mode=factory_mode,
            config_service=_Config(config),
            factory_selection_settings=SimpleNamespace(
                load_selected_config_ids=lambda: (config["config_id"],)
            ),
        )
        self.dashboard = ResultDashboardModule(services)
        self.diagnostics: list[object] = []
        self.adapter = HistoryQtAdapter(
            self.dashboard,
            diagnostic_callback=self.diagnostics.append,
        )
        self.window = QMainWindow()
        self.window.setWindowTitle("Two-stage analysis SLA benchmark")
        self.window.resize(1280, 900)
        self.window.setCentralWidget(self.dashboard)
        self.decision_paints: dict[str, int] = {}
        self.dashboard.decisionPainted.connect(self._on_decision_painted)
        self.chart_monitor = _ChartPaintMonitor(self.dashboard)

    def _on_decision_painted(self, revision, painted_at) -> None:
        if type(revision) is str and type(painted_at) is int:
            self.decision_paints[revision] = painted_at

    def show(self) -> None:
        self.window.showNormal()
        self.window.raise_()
        self.window.activateWindow()
        self.app.processEvents()

    def wait_until(self, predicate, timeout_seconds: float, label: str) -> None:
        deadline = time.monotonic() + timeout_seconds
        while not predicate():
            self.app.processEvents()
            if time.monotonic() >= deadline:
                raise TimeoutError(f"Qt benchmark endpoint timed out: {label}")
            time.sleep(0.001)
        self.app.processEvents()

    def project_decision(
        self,
        *,
        run_id: str,
        attempt_id: str,
        judgment: str,
        canonical: str,
        config: dict,
    ) -> tuple[str, int]:
        from app.service.fault_types import encode_history_labels_payload
        from app.service.multi_run_coordinator_mvp import (
            FrozenPayload,
            HistoryFactKind,
            ProjectedHistoryEvent,
            ResultProjection,
            ResultProjectionPhase,
            TerminalConclusion,
        )

        revision = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        value_id, labels = encode_history_labels_payload(
            config_id=config["config_id"],
            name=str(config.get("config_name") or "benchmark"),
            fault_types=tuple(config.get("fault_types") or ()),
            manual_judgment_buttons_visible=config.get(
                "manual_judgment_buttons_visible",
                False,
            ),
            details=({"label": "scenario", "value": "latency"},),
            sn=f"BENCH-{attempt_id[:8]}",
            trigger_source="manual",
        )
        self.adapter.publish(
            ProjectedHistoryEvent(
                kind=HistoryFactKind.RUN_TERMINATED,
                run_id=run_id,
                expected_revision=3,
                operation_id=attempt_id,
                proposed_revision=4,
                projection=ResultProjection(
                    phase=ResultProjectionPhase.TERMINAL,
                    conclusion=(
                        TerminalConclusion.OK
                        if judgment == "PASS"
                        else TerminalConclusion.NG
                    ),
                    error_category=None,
                    reason="",
                    history_labels=FrozenPayload(value_id, labels),
                    result_payload=FrozenPayload(
                        f"bridge-result:{attempt_id}",
                        canonical.encode("utf-8"),
                    ),
                    terminal_revision=revision,
                ),
            )
        )
        self.wait_until(
            lambda: revision in self.decision_paints or bool(self.diagnostics),
            5.0,
            "ResultModule.decisionPainted",
        )
        if self.diagnostics:
            raise RuntimeError(f"history projection failed: {self.diagnostics[-1]}")
        return revision, self.decision_paints.pop(revision)

    def project_enrichment(
        self,
        *,
        run_id: str,
        attempt_id: str,
        terminal_revision: str,
        canonical: str,
    ) -> int:
        from app.service.multi_run_coordinator_mvp import (
            FrozenPayload,
            ResultEnrichmentProjection,
        )

        if self.dashboard.selected_run_id() != run_id:
            self.dashboard.select_run(run_id)
            self.wait_until(
                lambda: self.dashboard.selected_run_id() == run_id,
                5.0,
                "historical run selection",
            )
        self.chart_monitor.arm()
        self.adapter.publish_enrichment(
            ResultEnrichmentProjection(
                run_id=run_id,
                attempt_id=attempt_id,
                terminal_revision=terminal_revision,
                expected_revision=4,
                result_payload=FrozenPayload(
                    f"bridge-result:{attempt_id}",
                    canonical.encode("utf-8"),
                ),
            )
        )
        self.wait_until(
            lambda: self.chart_monitor.complete or bool(self.diagnostics),
            8.0,
            "production chart paintEvent",
        )
        if self.diagnostics:
            raise RuntimeError(f"enrichment projection failed: {self.diagnostics[-1]}")
        painted = self.chart_monitor.painted_at_ns
        self.chart_monitor.close()
        if painted is None:
            raise RuntimeError("no production chart widget acknowledged paint")
        return painted

    def host_state(self) -> dict[str, object]:
        from PyQt6.QtCore import Qt
        from PyQt6.QtGui import QGuiApplication

        return {
            "qt_platform": QGuiApplication.platformName(),
            "interactive_desktop": QGuiApplication.platformName().lower()
            not in {"offscreen", "minimal", "minimalegl"},
            "result_window_shown": self.window.isVisible(),
            "result_window_minimized": bool(
                self.window.windowState() & Qt.WindowState.WindowMinimized
            ),
        }

    def close(self) -> None:
        self.chart_monitor.close()
        self.window.close()
        self.app.processEvents()


def _build_job(
    *,
    audio: _AudioInput,
    config: dict,
    calibration: dict,
    toolchain,
    run_id: str,
    attempt_id: str,
):
    from app.service.analysis.scheduler import (
        AnalysisInputSnapshot,
        AnalysisJob,
        AnalysisKind,
        AudioStorageKind,
        PersistedAudioReference,
    )

    offset = float(config.get("analysis_window_offset_ms") or 0) / 1000.0
    duration = float(config.get("analysis_duration") or audio.duration_seconds)
    end = min(audio.duration_seconds, offset + duration)
    if end <= offset:
        raise ValueError("analysis window does not fit benchmark input")
    selected = list(config["selected_channels"])
    snapshot = AnalysisInputSnapshot(
        config_snapshot_json=_canonical_json(config),
        calibration_snapshot_json=_canonical_json(calibration),
        workorder_snapshot_json=_canonical_json(
            {"sn": f"BENCH-{attempt_id[:8]}", "model": "SLA"}
        ),
        channel_mapping_json=_canonical_json(
            {"schema": "audio-channel-map/v1", "channel_order": selected}
        ),
        analysis_window_start_seconds=offset,
        analysis_window_end_seconds=end,
        algorithm_revision="legacy-judgment/v1",
        ai_toolchain=toolchain,
    )
    reference = PersistedAudioReference(
        reference_id=str(uuid.uuid4()),
        storage_kind=AudioStorageKind.CAPTURED_FINAL_WAV,
        absolute_path=str(audio.path),
        sample_rate_hz=audio.sample_rate_hz,
        channel_count=audio.channel_count,
        frame_count=audio.frame_count,
        dtype=audio.dtype,
        content_revision=f"sha256:{_sha256_file(audio.path)}",
    )
    return AnalysisJob(
        run_id=run_id,
        analysis_attempt_id=attempt_id,
        kind=AnalysisKind.AUTOMATIC,
        audio=reference,
        snapshot=snapshot,
        execution_timeout_seconds=120.0,
    )


def _event(timings, boundary: AnalysisTimingBoundary):
    matches = [event for event in timings.events if event.boundary is boundary]
    if len(matches) != 1:
        raise RuntimeError(f"missing exact timing boundary: {boundary.value}")
    return matches[0]


def _matching_descendant_pids(
    entries: tuple[tuple[int, int, str], ...],
    root_pid: int,
    executable_name: str,
) -> set[int]:
    descendants: set[int] = set()
    frontier = {root_pid}
    while frontier:
        children = {
            pid
            for pid, parent_pid, _name in entries
            if parent_pid in frontier and pid not in descendants
        }
        descendants.update(children)
        frontier = children
    needle = executable_name.lower()
    names = {pid: name.lower() for pid, _parent, name in entries}
    return {pid for pid in descendants if names.get(pid) == needle}


def _windows_process_entries() -> tuple[tuple[int, int, str], ...]:
    if platform.system() != "Windows":
        raise RuntimeError("Windows process snapshot is unavailable")
    import ctypes
    from ctypes import wintypes

    class PROCESSENTRY32W(ctypes.Structure):
        _fields_ = (
            ("dwSize", wintypes.DWORD),
            ("cntUsage", wintypes.DWORD),
            ("th32ProcessID", wintypes.DWORD),
            ("th32DefaultHeapID", ctypes.c_size_t),
            ("th32ModuleID", wintypes.DWORD),
            ("cntThreads", wintypes.DWORD),
            ("th32ParentProcessID", wintypes.DWORD),
            ("pcPriClassBase", wintypes.LONG),
            ("dwFlags", wintypes.DWORD),
            ("szExeFile", wintypes.WCHAR * 260),
        )

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    create_snapshot = kernel32.CreateToolhelp32Snapshot
    create_snapshot.argtypes = (wintypes.DWORD, wintypes.DWORD)
    create_snapshot.restype = wintypes.HANDLE
    process_first = kernel32.Process32FirstW
    process_first.argtypes = (wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W))
    process_first.restype = wintypes.BOOL
    process_next = kernel32.Process32NextW
    process_next.argtypes = (wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W))
    process_next.restype = wintypes.BOOL
    close_handle = kernel32.CloseHandle
    close_handle.argtypes = (wintypes.HANDLE,)
    close_handle.restype = wintypes.BOOL

    snapshot = create_snapshot(0x00000002, 0)
    if snapshot == ctypes.c_void_p(-1).value:
        raise RuntimeError("CreateToolhelp32Snapshot failed")
    try:
        entry = PROCESSENTRY32W()
        entry.dwSize = ctypes.sizeof(PROCESSENTRY32W)
        if not process_first(snapshot, ctypes.byref(entry)):
            raise RuntimeError("Process32FirstW failed")
        rows = []
        while True:
            rows.append(
                (
                    int(entry.th32ProcessID),
                    int(entry.th32ParentProcessID),
                    str(entry.szExeFile),
                )
            )
            if not process_next(snapshot, ctypes.byref(entry)):
                break
        return tuple(rows)
    finally:
        close_handle(snapshot)


def _ai_descendant_pids(entrypoint: str) -> set[int]:
    needle = Path(entrypoint.strip('"')).name.lower()
    try:
        import psutil
    except ImportError:
        return _matching_descendant_pids(
            _windows_process_entries(),
            os.getpid(),
            needle,
        )
    try:
        descendants = psutil.Process().children(recursive=True)
        result = set()
        for process in descendants:
            try:
                command = " ".join(process.cmdline()).lower()
                name = process.name().lower()
            except Exception:
                continue
            if needle and (needle in command or needle == name):
                result.add(process.pid)
        return result
    except Exception:
        return _matching_descendant_pids(
            _windows_process_entries(),
            os.getpid(),
            needle,
        )


def _plc_payload(decision) -> dict[str, object]:
    root = json.loads(decision.canonical_result_json)
    result = root["result"]
    judgment = result.get("judgment_payload") or {}
    channels = judgment.get("channel_results") or []

    def maximum(key: str) -> float:
        values = [
            item.get(key)
            for item in channels
            if isinstance(item, dict)
            and type(item.get(key)) in (int, float)
        ]
        return float(max(values)) if values else 0.0

    return {
        "finished": True,
        "error_code": 0,
        "sn_code": "BENCHMARK",
        "test_result": decision.judgment == "PASS",
        "score": 0.0,
        "spl": maximum("spl_max_db"),
        "loudness": maximum("loudness_max_sone"),
        "vibrate": 0.0,
        "ai_score": float(result.get("ai_score") or 0.0),
    }


class _LivePlcEndpoint:
    def __init__(self, database_path: Path, plc_id: int) -> None:
        from app.services_container import EventBus
        from app.service.plc_service import PlcService
        from app.service.plc_result_sequencer import PlcResultSequencer

        with closing(sqlite3.connect(database_path)) as connection:
            connection.row_factory = sqlite3.Row
            row = connection.execute(
                "SELECT * FROM plc_table WHERE id = ?", (plc_id,)
            ).fetchone()
            offsets = connection.execute(
                "SELECT * FROM plc_field_offset_table WHERE plc_id = ?",
                (plc_id,),
            ).fetchone()
        if row is None or offsets is None:
            raise ValueError("production PLC configuration is unavailable")
        offsets_dict = dict(offsets)
        self.bus = EventBus()
        self.service = PlcService(
            self.bus,
            lambda *_args: None,
            ip=row["plc_ip"],
            rack=row["plc_rack"],
            slot=row["plc_slot"],
            db_number=row["plc_db_number"],
            field_offsets_dict=offsets_dict,
            plc_id=plc_id,
        )
        self.service._connect_once()
        if self.service.plc_client is None:
            raise RuntimeError("production PLC did not connect")
        self.reports: dict[str, tuple[object, int]] = {}
        self.sequencer = PlcResultSequencer(
            write_result=self.service.write_result,
            report_attempt=lambda report: self.reports.__setitem__(
                report.run_id, (report, time.perf_counter_ns())
            ),
        )
        self.plc_id = plc_id

    def write(self, run_id: str, payload: dict[str, object]) -> int:
        from app.service.plc_result_sequencer import (
            PlcAttemptOutcome,
            PlcMutationDisposition,
            PlcOperationKind,
        )

        registration = self.sequencer.register(
            run_id=run_id,
            trigger_source="plc",
            operation_kind=PlcOperationKind.INITIAL,
            plc_id=self.plc_id,
        )
        if registration.ticket is None:
            raise RuntimeError("PLC sequencer rejected benchmark registration")
        if self.sequencer.mark_accepted(registration.ticket) is not PlcMutationDisposition.APPLIED:
            raise RuntimeError("PLC sequencer rejected benchmark acceptance")
        if self.sequencer.publish_terminal(registration.ticket, payload) is not PlcMutationDisposition.APPLIED:
            raise RuntimeError("PLC sequencer rejected benchmark terminal")
        report, acknowledged_at = self.reports.pop(run_id)
        if report.outcome is not PlcAttemptOutcome.BOUNDARY_RETURNED:
            raise RuntimeError(f"PLC physical write failed: {report.reason}")
        return acknowledged_at

    def close(self) -> None:
        try:
            self.sequencer.shutdown(2.0)
        finally:
            self.service._disconnect()


def _empty_measurements() -> tuple[dict[str, object], dict[str, object], dict[str, int]]:
    return (
        {field: None for field in BENCHMARK_TIMESTAMP_FIELDS},
        {field: None for field in BENCHMARK_DURATION_FIELDS},
        {field: 0 for field in BENCHMARK_SIZE_FIELDS},
    )


def _parity(decision, baseline: dict[str, object] | None) -> tuple[dict, dict]:
    root = json.loads(decision.canonical_result_json)
    result = root["result"]
    snapshot = {
        "judgment": decision.judgment,
        "limit_pass": result.get("limit_pass"),
        "ai_pass": result.get("ai_pass"),
        "ai_score": result.get("ai_score"),
        "channel_results": result.get("channel_results"),
    }
    if baseline is None:
        return snapshot, {
            "judgment_equal": True,
            "limits_equal": True,
            "numeric_within_tolerance": True,
        }
    judgment_equal = snapshot["judgment"] == baseline["judgment"]
    limits_equal = (
        snapshot["limit_pass"] == baseline["limit_pass"]
        and snapshot["ai_pass"] == baseline["ai_pass"]
        and snapshot["channel_results"] == baseline["channel_results"]
    )
    ai_a = snapshot.get("ai_score")
    ai_b = baseline.get("ai_score")
    numeric_equal = (
        ai_a == ai_b
        if ai_a is None or ai_b is None
        else bool(np.isclose(ai_a, ai_b, rtol=1e-7, atol=1e-9))
    )
    return snapshot, {
        "judgment_equal": judgment_equal,
        "limits_equal": limits_equal,
        "numeric_within_tolerance": numeric_equal,
    }


def _summarize(iterations: list[dict]) -> dict[str, dict[str, int]]:
    summary: dict[str, dict[str, int]] = {}
    for field in BENCHMARK_DURATION_FIELDS:
        values = [
            item["durations_ns"][field]
            for item in iterations
            if type(item["durations_ns"].get(field)) is int
        ]
        if values:
            summary[field] = benchmark_percentiles(values)
    return summary


def _run_six_overlap(
    *,
    set_capture_active,
    prepare_iteration,
    preregister_iteration,
    begin_iteration,
    complete_decision,
    finish_iteration,
) -> list:
    """Freeze all six Decisions before releasing the quiet Enrichment gate."""

    prepared = [prepare_iteration(index) for index in range(6)]
    for item in prepared:
        preregister_iteration(item)
    frozen_by_index = {}
    completed_indexes = set()
    frozen_lock = threading.Lock()
    capture_released = False
    inflight_at_final_freeze = 0

    def on_frozen(index: int, frozen_ns: int) -> None:
        nonlocal capture_released, inflight_at_final_freeze
        with frozen_lock:
            frozen_by_index[index] = frozen_ns
            if len(frozen_by_index) == 6 and not capture_released:
                inflight_at_final_freeze = 6 - len(completed_indexes)
                set_capture_active(False)
                capture_released = True

    def run_decision(index: int, item):
        result = begin_iteration(
            item,
            lambda frozen_ns: on_frozen(index, frozen_ns),
        )
        with frozen_lock:
            completed_indexes.add(index)
        return result

    set_capture_active(True)
    try:
        with ThreadPoolExecutor(
            max_workers=2,
            thread_name_prefix="benchmark-overlap-decision",
        ) as pool:
            futures = [
                pool.submit(
                    run_decision,
                    index,
                    item,
                )
                for index, item in enumerate(prepared)
            ]
            pending_by_index = {}
            for future in as_completed(futures):
                item = complete_decision(future.result())
                pending_by_index[item["index"]] = item
            pending = [pending_by_index[index] for index in range(6)]
    finally:
        if not capture_released:
            set_capture_active(False)
    if len(frozen_by_index) != 6:
        raise RuntimeError("six-run overlap did not freeze every accepted Decision")
    quiet_origin_ns = max(frozen_by_index.values())
    results = [
        finish_iteration(item, quiet_origin_ns)
        for item in pending
    ]
    return results, {
        "preregistered_attempts": 6,
        "inflight_decisions_at_final_freeze": inflight_at_final_freeze,
        "final_freeze_ns": quiet_origin_ns,
    }


def _freeze_new_capture_epoch(
    *,
    set_capture_active,
    observe_capture_state,
    perf_counter_ns=time.perf_counter_ns,
) -> int:
    """Create one observable capture transition around the exact freeze."""

    set_capture_active(True)
    try:
        observe_capture_state()
        frozen_ns = perf_counter_ns()
    finally:
        set_capture_active(False)
    observe_capture_state()
    return frozen_ns


def _begin_iteration(
    *,
    index: int,
    executor,
    audio: _AudioInput,
    config: dict,
    calibration: dict,
    toolchain,
    durable: _DurableCommitProbe,
    qt: _QtEndpoint,
    plc: _LivePlcEndpoint | None,
    frozen_ns: int | None = None,
    prepared: dict | None = None,
    on_frozen=None,
    defer_projection: bool = False,
) -> dict:
    if prepared is None:
        run_id = str(uuid.uuid4())
        attempt_id = str(uuid.uuid4())
        job = _build_job(
            audio=audio,
            config=config,
            calibration=calibration,
            toolchain=toolchain,
            run_id=run_id,
            attempt_id=attempt_id,
        )
    else:
        run_id = prepared["run_id"]
        attempt_id = prepared["attempt_id"]
        job = prepared["job"]
    register = getattr(executor, "register_staged_attempt", None)
    if callable(register) and not (
        isinstance(prepared, dict) and prepared.get("preregistered") is True
    ):
        if register(job) is not True:
            raise RuntimeError("benchmark attempt preregistration was rejected")
    loaded = _LoadedAudio(audio)
    timestamps, durations, sizes = _empty_measurements()
    frozen = time.perf_counter_ns() if frozen_ns is None else frozen_ns
    if callable(on_frozen):
        on_frozen(frozen)
    scheduled = time.perf_counter_ns()
    ai_before = _ai_descendant_pids(toolchain.entrypoint) if toolchain else set()
    decision = executor.execute_decision(job, loaded, _CancelToken())
    decision_returned = time.perf_counter_ns()
    ai_after = _ai_descendant_pids(toolchain.entrypoint) if toolchain else set()

    dsp_event = _event(decision.timings, AnalysisTimingBoundary.LEGACY_DECISION_DSP)
    features_event = _event(
        decision.timings,
        AnalysisTimingBoundary.DECISION_FEATURE_EXTRACTION,
    )
    judgment_event = _event(
        decision.timings,
        AnalysisTimingBoundary.DECISION_JUDGMENT,
    )
    ai_event = _event(decision.timings, AnalysisTimingBoundary.RESIDENT_AI_RESPONSE)
    projection_event = _event(
        decision.timings, AnalysisTimingBoundary.DECISION_PROJECTION
    )
    timestamps.update(
        {
            "audio_frozen_ns": frozen,
            "decision_scheduled_ns": scheduled,
            "audio_transport_ready_ns": dsp_event.started_ns,
            "decision_features_finished_ns": features_event.finished_ns,
            "decision_dsp_finished_ns": dsp_event.finished_ns,
            "ai_finished_ns": ai_event.finished_ns,
            "decision_projection_finished_ns": projection_event.finished_ns,
        }
    )
    durations.update(
        {
            "decision_scheduling_ns": scheduled - frozen,
            "audio_transport_ns": max(0, dsp_event.started_ns - scheduled),
            "decision_feature_extraction_ns": features_event.duration_ns,
            "decision_judgment_ns": judgment_event.duration_ns,
            "decision_dsp_ns": dsp_event.duration_ns,
            "ai_ns": ai_event.duration_ns,
            "decision_projection_ns": projection_event.duration_ns,
            "transport_ns": max(0, decision_returned - projection_event.finished_ns),
        }
    )
    sizes["bundle_peak_bytes"] = projection_event.byte_count
    pending = {
        "index": index,
        "run_id": run_id,
        "attempt_id": attempt_id,
        "frozen_ns": frozen,
        "decision": decision,
        "decision_returned_ns": decision_returned,
        "timestamps": timestamps,
        "durations": durations,
        "sizes": sizes,
        "config": config,
        "steady_ai_process_spawned": bool(ai_after - ai_before),
    }
    if defer_projection:
        return pending
    return _complete_decision_phase(
        pending,
        executor=executor,
        durable=durable,
        qt=qt,
        plc=plc,
    )


def _complete_decision_phase(
    pending: dict,
    *,
    executor,
    durable: _DurableCommitProbe,
    qt: _QtEndpoint,
    plc: _LivePlcEndpoint | None,
) -> dict:
    run_id = pending["run_id"]
    attempt_id = pending["attempt_id"]
    frozen = pending["frozen_ns"]
    decision = pending["decision"]
    decision_returned = pending["decision_returned_ns"]
    timestamps = pending["timestamps"]
    durations = pending["durations"]
    db_finished = durable.commit(
        attempt_id, decision.judgment, decision.canonical_result_json
    )
    timestamps["durable_db_committed_ns"] = db_finished
    durations["durable_db_ns"] = db_finished - decision_returned
    mark_committed = getattr(executor, "mark_decision_committed", None)
    if callable(mark_committed) and mark_committed(attempt_id) is not True:
        raise RuntimeError("Decision could not enter committed staged state")

    terminal_revision, painted_at = qt.project_decision(
        run_id=run_id,
        attempt_id=attempt_id,
        judgment=decision.judgment,
        canonical=decision.canonical_result_json,
        config=pending["config"],
    )
    timestamps["decision_painted_ns"] = painted_at
    durations["decision_paint_ns"] = max(0, painted_at - db_finished)
    if plc is not None:
        plc_started = time.perf_counter_ns()
        plc_at = plc.write(run_id, _plc_payload(decision))
        timestamps["plc_acknowledged_ns"] = plc_at
        durations["plc_ns"] = plc_at - plc_started
        durations["decision_endpoint_ns"] = max(painted_at, plc_at) - frozen

    pending["terminal_revision"] = terminal_revision
    return pending


def _finish_iteration(
    pending: dict,
    *,
    executor,
    artifact_store,
    qt: _QtEndpoint,
    baseline: dict[str, object] | None,
    enforce_quiet_fence: bool,
    enrichment_collector: _EnrichmentCollector | None,
    quiet_origin_ns: int,
    endpoint_origin_ns: int,
) -> tuple[dict, dict]:
    index = pending["index"]
    run_id = pending["run_id"]
    attempt_id = pending["attempt_id"]
    decision = pending["decision"]
    terminal_revision = pending["terminal_revision"]
    timestamps = pending["timestamps"]
    durations = pending["durations"]
    sizes = pending["sizes"]

    if enforce_quiet_fence:
        quiet_deadline_ns = quiet_origin_ns + 250_000_000
        remaining = quiet_deadline_ns - time.perf_counter_ns()
        if remaining > 0:
            time.sleep(remaining / 1_000_000_000)
    if enrichment_collector is None:
        enrichment = executor.execute_enrichment(decision.bundle_token)
        transport_finished = time.perf_counter_ns()
    else:
        enrichment, transport_finished = enrichment_collector.wait(attempt_id)
    enrichment_event = _event(
        enrichment.timings, AnalysisTimingBoundary.ENRICHMENT_DSP
    )
    encoding_event = _event(
        enrichment.timings, AnalysisTimingBoundary.CANONICAL_ENCODING
    )
    compression_event = _event(
        enrichment.timings, AnalysisTimingBoundary.CANONICAL_COMPRESSION
    )
    quiet_events = [
        event
        for event in enrichment.timings.events
        if event.boundary is AnalysisTimingBoundary.QUIET_GATE_RELEASED
    ]
    quiet_released = (
        quiet_events[0].finished_ns
        if len(quiet_events) == 1
        else enrichment_event.started_ns
    )
    timestamps.update(
        {
            "quiet_fence_released_ns": quiet_released,
            "enrichment_dsp_finished_ns": enrichment_event.finished_ns,
            "canonical_encoded_ns": encoding_event.finished_ns,
            "compressed_ns": compression_event.finished_ns,
            "transport_finished_ns": transport_finished,
        }
    )
    durations.update(
        {
            "quiet_fence_ns": max(0, quiet_released - quiet_origin_ns),
            "enrichment_dsp_ns": enrichment_event.duration_ns,
            "canonical_encoding_ns": encoding_event.duration_ns,
            "compression_ns": compression_event.duration_ns,
            "transport_ns": max(0, transport_finished - compression_event.finished_ns),
        }
    )
    sizes["canonical_json_bytes"] = encoding_event.byte_count
    sizes["zlib_bytes"] = compression_event.byte_count

    artifact_started = time.perf_counter_ns()
    published = artifact_store.publish(
        attempt_id,
        enrichment.judgment,
        enrichment.canonical_result_json,
        precompressed_zlib=enrichment.result_payload_zlib,
        prevalidated_json=True,
    )
    artifact_store.settle_publication(published, retain=True)
    loaded_payload = artifact_store.load_reference(
        published.reference_json,
        expected_judgment=enrichment.judgment,
    )
    if loaded_payload != enrichment.canonical_result_json:
        raise RuntimeError("published full artifact is not readable exactly")
    artifact_finished = time.perf_counter_ns()
    timestamps["artifact_fsync_finished_ns"] = artifact_finished
    durations["artifact_fsync_ns"] = artifact_finished - artifact_started

    chart_started = time.perf_counter_ns()
    charts_painted = qt.project_enrichment(
        run_id=run_id,
        attempt_id=attempt_id,
        terminal_revision=terminal_revision,
        canonical=enrichment.canonical_result_json,
    )
    timestamps["charts_painted_ns"] = charts_painted
    durations["chart_paint_ns"] = charts_painted - chart_started
    durations["charts_endpoint_ns"] = charts_painted - endpoint_origin_ns

    parity_snapshot, parity = _parity(decision, baseline)
    return (
        {
            "iteration": index,
            "identity": {
                "run_id": run_id,
                "analysis_attempt_id": attempt_id,
            },
            "timestamps_ns": timestamps,
            "durations_ns": durations,
            "sizes_bytes": sizes,
            "steady_ai_process_spawned": pending["steady_ai_process_spawned"],
            "parity": parity,
        },
        parity_snapshot,
    )


def _run_iteration(
    *,
    index: int,
    executor,
    audio: _AudioInput,
    config: dict,
    calibration: dict,
    toolchain,
    durable: _DurableCommitProbe,
    artifact_store,
    qt: _QtEndpoint,
    plc: _LivePlcEndpoint | None,
    baseline: dict[str, object] | None,
    enforce_quiet_fence: bool,
    enrichment_collector: _EnrichmentCollector | None,
    set_capture_active,
) -> tuple[dict, dict]:
    observe_capture_state = getattr(executor, "quiet_epoch_id", None)
    if not callable(observe_capture_state):
        observe_capture_state = lambda: None
    frozen_ns = _freeze_new_capture_epoch(
        set_capture_active=set_capture_active,
        observe_capture_state=observe_capture_state,
    )
    pending = _begin_iteration(
        index=index,
        executor=executor,
        audio=audio,
        config=config,
        calibration=calibration,
        toolchain=toolchain,
        durable=durable,
        qt=qt,
        plc=plc,
        frozen_ns=frozen_ns,
    )
    return _finish_iteration(
        pending,
        executor=executor,
        artifact_store=artifact_store,
        qt=qt,
        baseline=baseline,
        enforce_quiet_fence=enforce_quiet_fence,
        enrichment_collector=enrichment_collector,
        quiet_origin_ns=pending["frozen_ns"],
        endpoint_origin_ns=pending["frozen_ns"],
    )

def _scenario_report(
    *,
    name: str,
    audio: _AudioInput,
    config: dict,
    toolchain,
    iterations: list[dict],
    status: str = "MEASURED",
    executor_mode: str = "production_process_pool",
) -> dict[str, object]:
    entrypoint_path = (
        Path(toolchain.entrypoint.strip('"')) if toolchain is not None else None
    )
    return {
        "name": name,
        "status": status,
        "executor_mode": executor_mode,
        "parity_basis": "optimized_repeatability_only",
        "legacy_oracle": {
            "status": "NOT_EVALUATED",
            "reason": (
                "independent legacy AI response and full boundary fixtures "
                "were not available in the production benchmark process"
            ),
        },
        "input": audio.report(),
        "config": {
            "config_id": config["config_id"],
            "config_sha256": _sha256_bytes(
                _canonical_json(config).encode("utf-8")
            ),
            "algorithm_revision": "legacy-judgment/v1",
        },
        "toolchain": {
            "toolchain_id": toolchain.toolchain_id if toolchain else None,
            "execution_revision": toolchain.execution_revision if toolchain else None,
            "entrypoint_sha256": (
                _sha256_file(entrypoint_path)
                if entrypoint_path is not None and entrypoint_path.is_file()
                else None
            ),
        },
        "iterations": iterations,
        "summary": _summarize(iterations),
    }


def _blocked_scenario(name: str, reason: str) -> dict[str, object]:
    return {
        "name": name,
        "status": "BLOCKED",
        "blocker": reason,
        "input": {},
        "config": {},
        "toolchain": {},
        "iterations": [],
        "summary": {},
    }


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--iterations", type=int, default=30)
    parser.add_argument("--audio", type=Path, required=True)
    parser.add_argument("--json-output", type=Path, required=True)
    parser.add_argument("--config-db", type=Path)
    parser.add_argument("--run-db", type=Path)
    parser.add_argument("--config-id", type=int, default=1)
    parser.add_argument(
        "--functional",
        action="store_true",
        help="allow hidden/offscreen or short runs; never evaluates the SLA",
    )
    parser.add_argument(
        "--live-plc",
        action="store_true",
        help="write actual production result fields to the configured PLC",
    )
    parser.add_argument("--plc-id", type=int, default=1)
    parser.add_argument(
        "--skip-overlap",
        action="store_true",
        help="diagnostic-only; records an explicit overlap blocker",
    )
    parser.add_argument(
        "--overlap-only",
        action="store_true",
        help="diagnostic-only six-run snapshot; skips sequential scenarios",
    )
    args = parser.parse_args(argv)
    if args.iterations <= 0:
        parser.error("--iterations must be positive")
    if not args.functional and args.iterations < 30:
        parser.error("hard benchmark requires at least 30 iterations")
    return args


def run_benchmark(args: argparse.Namespace) -> dict[str, object]:
    worktree = Path(__file__).resolve().parents[1]
    main_root = _main_workspace_root(worktree)
    audio_path = args.audio
    if not audio_path.is_absolute():
        candidate = (worktree / audio_path).resolve()
        audio_path = candidate if candidate.is_file() else (main_root / audio_path).resolve()
    config_db = (
        args.config_db.resolve()
        if args.config_db is not None
        else (main_root / "data" / "jingchen.db").resolve()
    )
    run_db = (
        args.run_db.resolve()
        if args.run_db is not None
        else (main_root / "data" / "run_v2.db").resolve()
    )
    host = collect_host_fingerprint()
    blockers: list[str] = []
    scenarios: dict[str, object] = {}
    endpoints = {
        "plc": {
            "evaluated": False,
            "source": "not enabled",
        },
        "decision_paint": {
            "evaluated": False,
            "source": "ResultModule.decisionPainted",
        },
        "chart_paint": {
            "evaluated": False,
            "source": "production chart widget paintEvent",
        },
        "durable_db": {
            "evaluated": False,
            "source": "diagnostic-only temporary commit probe; not production persistence",
        },
    }
    if not audio_path.is_file():
        blockers.append(f"requested production WAV is unavailable: {audio_path}")
    if not config_db.is_file():
        blockers.append(f"production config DB is unavailable: {config_db}")
    if not run_db.is_file():
        blockers.append(f"production run DB is unavailable: {run_db}")
    if blockers:
        for name in ("single_channel", "six_run_overlap", "two_channel_48khz"):
            scenarios[name] = _blocked_scenario(name, blockers[0])
        return {
            "schema": "analysis-latency-benchmark/v1",
            "generated_at_utc": _utc_now(),
            "host": host,
            "endpoints": endpoints,
            "scenarios": scenarios,
            "sla": {
                "status": "SLA_NOT_EVALUATED",
                "evaluated": False,
                "blockers": blockers,
                "decision_limit_ns": 500_000_000,
                "charts_limit_ns": 1_500_000_000,
            },
        }

    with tempfile.TemporaryDirectory(prefix="two-stage-analysis-") as temp_text:
        temp = Path(temp_text)
        single_path = temp / audio_path.name
        shutil.copy2(audio_path, single_path)
        single = _read_pcm16_wav(single_path, source="production_wav")
        if (
            single.sample_rate_hz != 44_100
            or single.channel_count != 1
            or not np.isclose(single.duration_seconds, 8.85, atol=0.001)
        ):
            blockers.append("requested production WAV shape is not 8.85s/44.1kHz/mono")
        two_channel = _write_two_channel_profile(single, temp / "two-channel-48khz.wav")
        production_config, calibration, toolchain, _toolchain_mapping = (
            _load_production_facts(config_db, args.config_id)
        )
        if toolchain is None:
            blockers.append("production config has no enabled Resident AI toolchain")
        elif not Path(toolchain.entrypoint.strip('"')).is_file():
            blockers.append("production Resident AI entrypoint is unavailable")
        single_config = _scenario_config(
            production_config, single, two_channel=False
        )
        two_config = _scenario_config(
            production_config, two_channel, two_channel=True
        )

        qt = _QtEndpoint(single_config)
        qt.show()
        host.update(qt.host_state())
        endpoints["decision_paint"]["evaluated"] = True
        endpoints["chart_paint"]["evaluated"] = True
        durable = _DurableCommitProbe(run_db, temp)
        endpoints["durable_db"]["evaluated"] = False
        artifact_store = None
        executor = None
        executor_mode = "production_process_pool"
        functional_fence = False
        enrichment_collector = _EnrichmentCollector()
        plc = None
        capture_active = {"value": False}
        try:
            from app.service.analysis.process_executor import ProcessIsolatedAnalysisExecutor
            from app.service.run_persistence.analysis_result_artifact import (
                AnalysisResultArtifactStore,
            )

            artifact_store = AnalysisResultArtifactStore(temp / "analysis_results")
            artifact_store.ensure_ready()
            executor = ProcessIsolatedAnalysisExecutor(
                capture_active=lambda: capture_active["value"],
                process_count=2,
                selected_toolchains=(toolchain,) if toolchain is not None else (),
                enrichment_sink=enrichment_collector,
            )
            try:
                executor.start()
            except Exception as startup_error:
                executor.close()
                if not args.functional or toolchain is None:
                    raise
                blockers.append(
                    "production process pool failed; functional fallback used: "
                    f"{type(startup_error).__name__}: {startup_error}"
                )
                from app.service.analysis.production_executor import (
                    ProductionAnalysisExecutor,
                )

                executor = ProductionAnalysisExecutor(
                    ai_runtime=_LegacyResidentRuntime(toolchain)
                )
                executor_mode = "functional_in_process_legacy_resident_protocol"
                functional_fence = True
                enrichment_collector = None
            revision_ready = getattr(executor, "revision_ready", None)
            if (
                executor_mode == "production_process_pool"
                and toolchain is not None
                and (not callable(revision_ready) or not revision_ready(toolchain))
            ):
                raise RuntimeError("Resident AI revision is not ready in every slot")
            if args.live_plc:
                plc = _LivePlcEndpoint(config_db, args.plc_id)
                endpoints["plc"] = {
                    "evaluated": True,
                    "source": "PlcResultAttemptReport.BOUNDARY_RETURNED",
                }
            else:
                blockers.append(
                    "live PLC result writes were not enabled; use --live-plc only on a safe line"
                )
            blockers.extend(
                (
                    "production AnalysisPersistenceBoundary/coordinator terminal path was not evaluated; diagnostic commit timing is not a production endpoint",
                    "independent legacy AI/full-payload oracle was not evaluated",
                    "complete production-owned AI acquire/send and persistence/UI diagnostics were not available",
                )
            )

            # Warm both process slots and all numerical backends without sampling.
            for warm_index in range(2):
                warm_job = _build_job(
                    audio=single,
                    config=single_config,
                    calibration=calibration,
                    toolchain=toolchain,
                    run_id=str(uuid.uuid4()),
                    attempt_id=str(uuid.uuid4()),
                )
                warm = executor.execute_decision(warm_job, _LoadedAudio(single), _CancelToken())
                durable.commit(
                    warm.identity.analysis_attempt_id,
                    warm.judgment,
                    warm.canonical_result_json,
                )
                mark_committed = getattr(executor, "mark_decision_committed", None)
                if callable(mark_committed):
                    mark_committed(warm.identity.analysis_attempt_id)
                if enrichment_collector is None:
                    executor.execute_enrichment(warm.bundle_token)
                else:
                    enrichment_collector.wait(warm.identity.analysis_attempt_id)

            single_iterations: list[dict] = []
            baseline = None
            for index in range(0 if args.overlap_only else args.iterations):
                measurement, parity_snapshot = _run_iteration(
                    index=index,
                    executor=executor,
                    audio=single,
                    config=single_config,
                    calibration=calibration,
                    toolchain=toolchain,
                    durable=durable,
                    artifact_store=artifact_store,
                    qt=qt,
                    plc=plc,
                    baseline=baseline,
                    enforce_quiet_fence=functional_fence,
                    enrichment_collector=enrichment_collector,
                    set_capture_active=lambda value: capture_active.__setitem__(
                        "value", value
                    ),
                )
                if baseline is None:
                    baseline = parity_snapshot
                single_iterations.append(measurement)
            scenarios["single_channel"] = _scenario_report(
                name="single_channel",
                audio=single,
                config=single_config,
                toolchain=toolchain,
                iterations=single_iterations,
                status=(
                    "MEASURED"
                    if executor_mode == "production_process_pool"
                    else "MEASURED_FUNCTIONAL_LEGACY_PROTOCOL"
                ),
                executor_mode=executor_mode,
            )
            if args.overlap_only:
                scenarios["single_channel"] = _blocked_scenario(
                    "single_channel", "overlap-only diagnostic"
                )
                blockers.append("overlap-only diagnostic skipped sequential scenarios")

            overlap_iterations: list[dict] = []
            if args.skip_overlap:
                blocker = "six-run overlap was explicitly skipped"
                blockers.append(blocker)
                scenarios["six_run_overlap"] = _blocked_scenario(
                    "six_run_overlap", blocker
                )
            else:
                def prepare_overlap(index):
                    run_id = str(uuid.uuid4())
                    attempt_id = str(uuid.uuid4())
                    return {
                        "index": index,
                        "run_id": run_id,
                        "attempt_id": attempt_id,
                        "job": _build_job(
                            audio=single,
                            config=single_config,
                            calibration=calibration,
                            toolchain=toolchain,
                            run_id=run_id,
                            attempt_id=attempt_id,
                        ),
                    }

                def preregister_overlap(prepared):
                    register = getattr(executor, "register_staged_attempt", None)
                    if not callable(register):
                        return
                    if register(prepared["job"]) is not True:
                        raise RuntimeError(
                            "six-run overlap preregistration was rejected"
                        )
                    prepared["preregistered"] = True

                def begin_overlap(prepared, on_frozen):
                    return _begin_iteration(
                        index=prepared["index"],
                        executor=executor,
                        audio=single,
                        config=single_config,
                        calibration=calibration,
                        toolchain=toolchain,
                        durable=durable,
                        qt=qt,
                        plc=plc,
                        prepared=prepared,
                        on_frozen=on_frozen,
                        defer_projection=True,
                    )

                overlap_results, overlap_snapshot = _run_six_overlap(
                    set_capture_active=lambda value: capture_active.__setitem__(
                        "value", value
                    ),
                    prepare_iteration=prepare_overlap,
                    preregister_iteration=preregister_overlap,
                    begin_iteration=begin_overlap,
                    complete_decision=lambda pending: _complete_decision_phase(
                        pending,
                        executor=executor,
                        durable=durable,
                        qt=qt,
                        plc=plc,
                    ),
                    finish_iteration=lambda pending, quiet_origin_ns: _finish_iteration(
                        pending,
                        executor=executor,
                        artifact_store=artifact_store,
                        qt=qt,
                        baseline=baseline,
                        enforce_quiet_fence=functional_fence,
                        enrichment_collector=enrichment_collector,
                        quiet_origin_ns=quiet_origin_ns,
                        endpoint_origin_ns=quiet_origin_ns,
                    ),
                )
                for measurement, _ in overlap_results:
                    overlap_iterations.append(measurement)
                scenarios["six_run_overlap"] = _scenario_report(
                    name="six_run_overlap",
                    audio=single,
                    config=single_config,
                    toolchain=toolchain,
                    iterations=overlap_iterations,
                    status="MEASURED_WITH_CAPTURE_GATE",
                    executor_mode=executor_mode,
                )
                scenarios["six_run_overlap"]["capture_snapshot"] = (
                    overlap_snapshot
                )
                blockers.append(
                    "six-run overlap used the production capture-active gate, not six live hardware captures"
                )

            qt.close()
            qt = _QtEndpoint(two_config)
            qt.show()
            host.update(qt.host_state())
            two_iterations: list[dict] = []
            two_baseline = None
            for index in range(0 if args.overlap_only else args.iterations):
                measurement, parity_snapshot = _run_iteration(
                    index=index,
                    executor=executor,
                    audio=two_channel,
                    config=two_config,
                    calibration=calibration,
                    toolchain=toolchain,
                    durable=durable,
                    artifact_store=artifact_store,
                    qt=qt,
                    plc=plc,
                    baseline=two_baseline,
                    enforce_quiet_fence=functional_fence,
                    enrichment_collector=enrichment_collector,
                    set_capture_active=lambda value: capture_active.__setitem__(
                        "value", value
                    ),
                )
                if two_baseline is None:
                    two_baseline = parity_snapshot
                two_iterations.append(measurement)
            scenarios["two_channel_48khz"] = _scenario_report(
                name="two_channel_48khz",
                audio=two_channel,
                config=two_config,
                toolchain=toolchain,
                iterations=two_iterations,
                status=(
                    "MEASURED"
                    if executor_mode == "production_process_pool"
                    else "MEASURED_FUNCTIONAL_LEGACY_PROTOCOL"
                ),
                executor_mode=executor_mode,
            )
            if args.overlap_only:
                scenarios["two_channel_48khz"] = _blocked_scenario(
                    "two_channel_48khz", "overlap-only diagnostic"
                )
        except Exception as exc:
            blockers.append(f"benchmark execution failed: {type(exc).__name__}: {exc}")
            for name in ("single_channel", "six_run_overlap", "two_channel_48khz"):
                scenarios.setdefault(name, _blocked_scenario(name, blockers[-1]))
        finally:
            if executor is not None:
                executor.close()
            if plc is not None:
                plc.close()
            qt.close()

    host_eligibility = evaluate_sla_host(host)
    blockers.extend(
        item for item in host_eligibility["blockers"] if item not in blockers
    )
    if args.functional:
        blockers.append("--functional mode never evaluates the hard SLA")
    measured = all(
        scenarios.get(name, {}).get("status", "").startswith("MEASURED")
        for name in ("single_channel", "six_run_overlap", "two_channel_48khz")
    )
    hard_evaluated = (
        not blockers
        and measured
        and args.iterations >= 30
        and endpoints["plc"]["evaluated"] is True
    )
    pass_sla = False
    if hard_evaluated:
        all_iterations = [
            item
            for scenario in scenarios.values()
            for item in scenario["iterations"]
        ]
        pass_sla = all(
            item["durations_ns"]["decision_endpoint_ns"] <= 500_000_000
            and item["durations_ns"]["charts_endpoint_ns"] <= 1_500_000_000
            and item["steady_ai_process_spawned"] is False
            and all(item["parity"].values())
            for item in all_iterations
        )
    sla = {
        "status": (
            "PASS"
            if hard_evaluated and pass_sla
            else "FAIL"
            if hard_evaluated
            else "SLA_NOT_EVALUATED"
        ),
        "evaluated": hard_evaluated,
        "blockers": blockers,
        "decision_limit_ns": 500_000_000,
        "charts_limit_ns": 1_500_000_000,
    }
    report = {
        "schema": "analysis-latency-benchmark/v1",
        "generated_at_utc": _utc_now(),
        "host": host,
        "endpoints": endpoints,
        "scenarios": scenarios,
        "sla": sla,
    }
    validate_benchmark_report(report, require_hard_sla=hard_evaluated)
    return report


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    report = run_benchmark(args)
    write_json_report(args.json_output, report)
    print(format_console_summary(report))
    return 0 if report["sla"]["status"] != "FAIL" else 2


if __name__ == "__main__":
    raise SystemExit(main())
