"""AI toolchain service.

Orchestrates: toolchain registry queries, binding resolution, preflight checks,
request building, subprocess execution, response validation, result persistence,
and UI ViewModel adaptation.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import secrets
import shlex
import subprocess
import sys
import time
import unicodedata
import uuid
from contextlib import nullcontext
from datetime import datetime, timedelta, timezone

from peewee import DoesNotExist, IntegrityError

from app.service.ai_resident_runtime import ResidentToolchainRuntime
from app.service.ai_subprocess import ai_subprocess_creation_flags
from model.ai_toolchain_table import AiToolchainTable
from model.test_config_ai_binding_table import TestConfigAiBindingTable
from model.ai_execution_table import AiExecutionTable
from model.test_config_table import TestConfigTable
from app.service.run_record_export.contracts import AiExportValue

_SUPPORTED_PROTOCOL_VERSION = 1
_DEFAULT_EXECUTION_MODE = "resident_process"
_SUPPORTED_EXECUTION_MODES = {
    "resident_process",
    "oneshot_process",
}
_GENERATED_TOOLCHAIN_PREFIX_FALLBACK = "toolchain"
_GENERATED_TOOLCHAIN_PREFIX_MAX_LEN = 24
_GENERATED_TOOLCHAIN_RANDOM_HEX_LEN = 8

_TZ8 = timezone(timedelta(hours=8))


def _now_iso() -> str:
    return datetime.now(_TZ8).isoformat()


def _gen_request_id() -> str:
    ts = datetime.now(_TZ8).strftime("%Y%m%d_%H%M%S")
    short = uuid.uuid4().hex[:8]
    return f"req_{ts}_{short}"


def _coerce_probability(value) -> float | None:
    if isinstance(value, bool):
        return None
    if not isinstance(value, (int, float)):
        return None
    probability = float(value)
    if not math.isfinite(probability):
        return None
    if 0.0 <= probability <= 1.0:
        return probability
    return None


def _resolve_effective_decision_threshold(
    ai_result: dict, request_params: dict
) -> tuple[float, str]:
    extra = ai_result.get("extra")
    response_threshold = None
    if isinstance(extra, dict):
        response_threshold = _coerce_probability(extra.get("decision_threshold"))
    if response_threshold is not None:
        return response_threshold, "response_extra"

    request_threshold = None
    if isinstance(request_params, dict):
        request_threshold = _coerce_probability(
            request_params.get("decision_threshold")
        )
    if request_threshold is not None:
        return request_threshold, "request_params"

    return 0.5, "default"


class AiService:
    """AI toolchain integration service."""

    def __init__(
        self,
        resident_runtime: ResidentToolchainRuntime | None = None,
    ) -> None:
        self._resident_runtime = resident_runtime or ResidentToolchainRuntime()

    def shutdown(self) -> None:
        self._resident_runtime.shutdown_all()

    @staticmethod
    def _is_resident_toolchain(toolchain: AiToolchainTable | None) -> bool:
        return bool(
            toolchain is not None
            and toolchain.execution_mode == "resident_process"
        )

    # ── Toolchain Registry ──────────────────────────────────

    def list_toolchains(self) -> list[dict]:
        rows = (
            AiToolchainTable.select()
            .order_by(AiToolchainTable.toolchain_id)
            .dicts()
        )
        return list(rows)

    def list_enabled_toolchains(self) -> list[dict]:
        rows = (
            AiToolchainTable.select()
            .where(AiToolchainTable.enabled == True)
            .order_by(AiToolchainTable.toolchain_id)
            .dicts()
        )
        return list(rows)

    def get_toolchain(self, toolchain_id: str) -> AiToolchainTable | None:
        try:
            return AiToolchainTable.get_by_id(toolchain_id)
        except DoesNotExist:
            return None

    def list_toolchain_bindings(self, toolchain_id: str) -> list[dict]:
        rows = (
            TestConfigAiBindingTable.select(
                TestConfigAiBindingTable.config_id.alias("config_id"),
                TestConfigTable.config_name,
            )
            .join(TestConfigTable)
            .where(TestConfigAiBindingTable.toolchain == toolchain_id)
            .order_by(TestConfigAiBindingTable.config_id)
            .dicts()
        )
        return list(rows)

    def _normalize_toolchain_text(
        self, value: str | None, field_name: str, *, allow_empty: bool
    ) -> str:
        if value is None:
            value = ""
        if not isinstance(value, str):
            raise ValueError(f"{field_name} must be a string")
        normalized = value.strip()
        if not allow_empty and not normalized:
            raise ValueError(f"{field_name} must not be empty")
        return normalized

    @classmethod
    def generated_toolchain_prefix(cls, display_name: str) -> str:
        value = display_name.strip().lower()
        value = unicodedata.normalize("NFKD", value)
        value = value.encode("ascii", "ignore").decode("ascii")
        value = re.sub(r"[^a-z0-9]+", "_", value).strip("_")
        if not value:
            return _GENERATED_TOOLCHAIN_PREFIX_FALLBACK
        value = value[:_GENERATED_TOOLCHAIN_PREFIX_MAX_LEN].strip("_")
        return value or _GENERATED_TOOLCHAIN_PREFIX_FALLBACK

    def generate_toolchain_id_from_prefix(self, prefix: str) -> str:
        normalized_prefix = self.generated_toolchain_prefix(prefix)
        while True:
            suffix = secrets.token_hex(_GENERATED_TOOLCHAIN_RANDOM_HEX_LEN // 2)
            toolchain_id = f"{normalized_prefix}_{suffix}"
            if self.get_toolchain(toolchain_id) is None:
                return toolchain_id

    def generate_toolchain_id(self, display_name: str) -> str:
        return self.generate_toolchain_id_from_prefix(
            self.generated_toolchain_prefix(display_name)
        )

    @staticmethod
    def _is_toolchain_id_uniqueness_error(exc: IntegrityError) -> bool:
        message = str(exc).lower()
        if "toolchain_id" not in message:
            return False
        return "unique" in message or "primary key" in message

    @staticmethod
    def _normalize_params_json(value) -> str:
        if value is None:
            value = "{}"
        if not isinstance(value, str):
            value = json.dumps(value, ensure_ascii=False)
        normalized = value.strip() or "{}"
        try:
            parsed = json.loads(normalized)
        except (json.JSONDecodeError, TypeError) as exc:
            raise ValueError("params_json must be valid JSON") from exc
        if not isinstance(parsed, dict):
            raise ValueError("params_json must be a JSON object")
        return json.dumps(parsed, ensure_ascii=False, separators=(",", ":"))

    def _normalize_toolchain_payload(
        self, original_toolchain_id: str | None, data: dict
    ) -> dict:
        if "execution_mode" in data:
            execution_mode = data["execution_mode"]
        elif original_toolchain_id is None:
            execution_mode = _DEFAULT_EXECUTION_MODE
        else:
            existing = self.get_toolchain(original_toolchain_id)
            if existing is None:
                raise ValueError("toolchain_id not found")
            execution_mode = existing.execution_mode
        if not isinstance(execution_mode, str):
            raise ValueError("execution_mode must be a string")
        execution_mode = execution_mode.strip()
        if execution_mode not in _SUPPORTED_EXECUTION_MODES:
            raise ValueError("execution_mode must be one of resident_process or oneshot_process")

        normalized = {
            "toolchain_id": self._normalize_toolchain_text(
                data.get("toolchain_id"), "toolchain_id", allow_empty=False
            ),
            "display_name": self._normalize_toolchain_text(
                data.get("display_name"), "display_name", allow_empty=False
            ),
            "version": self._normalize_toolchain_text(
                data.get("version", ""), "version", allow_empty=True
            ),
            "entrypoint": self._normalize_toolchain_text(
                data.get("entrypoint"), "entrypoint", allow_empty=False
            ),
            "description": self._normalize_toolchain_text(
                data.get("description", ""), "description", allow_empty=True
            ),
            "params_json": self._normalize_params_json(data.get("params_json", "{}")),
            "enabled": bool(data.get("enabled", True)),
            "protocol_version": _SUPPORTED_PROTOCOL_VERSION,
            "execution_mode": execution_mode,
        }

        package_dir = data.get("package_dir")
        if not isinstance(package_dir, str):
            raise ValueError("package_dir must be a string")
        package_dir = package_dir.strip()
        if not package_dir:
            raise ValueError("package_dir must not be empty")
        package_dir = os.path.abspath(package_dir)
        if not os.path.isdir(package_dir):
            raise ValueError("package_dir must be an existing directory")
        normalized["package_dir"] = package_dir

        timeout_ms = data.get("default_timeout_ms")
        if isinstance(timeout_ms, bool) or not isinstance(timeout_ms, int) or timeout_ms <= 0:
            raise ValueError("default_timeout_ms must be a positive integer")
        normalized["default_timeout_ms"] = timeout_ms

        generated_id_prefix = data.get("generated_id_prefix")
        if generated_id_prefix is not None:
            if not isinstance(generated_id_prefix, str):
                raise ValueError("generated_id_prefix must be a string")
            if generated_id_prefix.strip():
                normalized["generated_id_prefix"] = self.generated_toolchain_prefix(
                    generated_id_prefix
                )

        existing = self.get_toolchain(normalized["toolchain_id"])
        if existing is not None and normalized["toolchain_id"] != original_toolchain_id:
            raise ValueError("toolchain_id must be unique")

        return normalized

    def _rewrite_toolchain_binding_ids(self, old_toolchain_id: str, new_toolchain_id: str):
        from consts.time_consts import now_str

        (
            TestConfigAiBindingTable.update(
                toolchain=new_toolchain_id,
                updated_time=now_str(),
            )
            .where(TestConfigAiBindingTable.toolchain == old_toolchain_id)
            .execute()
        )

    def _clear_toolchain_bindings(self, toolchain_id: str):
        from consts.time_consts import now_str

        (
            TestConfigAiBindingTable.update(
                enabled=False,
                toolchain=None,
                updated_time=now_str(),
            )
            .where(TestConfigAiBindingTable.toolchain == toolchain_id)
            .execute()
        )

    def _delete_toolchain_row(self, toolchain_id: str):
        (
            AiToolchainTable.delete()
            .where(AiToolchainTable.toolchain_id == toolchain_id)
            .execute()
        )

    def save_toolchain(self, original_toolchain_id: str | None, data: dict) -> str:
        from consts.time_consts import now_str

        database = AiToolchainTable._meta.database
        existing = None
        resident_mutation_toolchain_id = None
        if original_toolchain_id is not None:
            existing = self.get_toolchain(original_toolchain_id)
            if existing is None:
                raise ValueError("toolchain_id not found")
            if self._is_resident_toolchain(existing):
                resident_mutation_toolchain_id = original_toolchain_id
        mutation_lease = (
            self._resident_runtime.hold_mutation_lease(resident_mutation_toolchain_id)
            if resident_mutation_toolchain_id is not None
            else nullcontext()
        )
        with mutation_lease:
            normalized = self._normalize_toolchain_payload(original_toolchain_id, data)
            generated_id_prefix = normalized.pop("generated_id_prefix", None)
            timestamp = now_str()

            with database.atomic():
                if original_toolchain_id is None:
                    create_payload = {
                        **normalized,
                        "created_time": timestamp,
                        "updated_time": timestamp,
                    }
                    try:
                        row = AiToolchainTable.create(**create_payload)
                    except IntegrityError as exc:
                        if (
                            generated_id_prefix is None
                            or not self._is_toolchain_id_uniqueness_error(exc)
                        ):
                            raise
                        normalized["toolchain_id"] = self.generate_toolchain_id_from_prefix(
                            generated_id_prefix
                        )
                        create_payload["toolchain_id"] = normalized["toolchain_id"]
                        row = AiToolchainTable.create(**create_payload)
                    return row.toolchain_id

                assert existing is not None

                if normalized["toolchain_id"] == original_toolchain_id:
                    existing.display_name = normalized["display_name"]
                    existing.version = normalized["version"]
                    existing.entrypoint = normalized["entrypoint"]
                    existing.package_dir = normalized["package_dir"]
                    existing.default_timeout_ms = normalized["default_timeout_ms"]
                    existing.params_json = normalized["params_json"]
                    existing.enabled = normalized["enabled"]
                    existing.description = normalized["description"]
                    existing.protocol_version = _SUPPORTED_PROTOCOL_VERSION
                    existing.execution_mode = normalized["execution_mode"]
                    existing.updated_time = timestamp
                    existing.save()
                    saved_toolchain_id = existing.toolchain_id
                else:
                    AiToolchainTable.create(
                        **normalized,
                        created_time=existing.created_time,
                        updated_time=timestamp,
                    )
                    self._rewrite_toolchain_binding_ids(
                        original_toolchain_id, normalized["toolchain_id"]
                    )
                    self._delete_toolchain_row(original_toolchain_id)
                    saved_toolchain_id = normalized["toolchain_id"]

            if resident_mutation_toolchain_id is not None:
                self._resident_runtime.invalidate(resident_mutation_toolchain_id)
            return saved_toolchain_id

    def delete_toolchain(self, toolchain_id: str):
        database = AiToolchainTable._meta.database
        existing = self.get_toolchain(toolchain_id)
        if existing is None:
            raise ValueError("toolchain_id not found")
        should_invalidate_resident = self._is_resident_toolchain(existing)
        mutation_lease = (
            self._resident_runtime.hold_mutation_lease(toolchain_id)
            if should_invalidate_resident
            else nullcontext()
        )

        with mutation_lease:
            with database.atomic():
                self._clear_toolchain_bindings(toolchain_id)
                self._delete_toolchain_row(toolchain_id)
            if should_invalidate_resident:
                self._resident_runtime.invalidate(toolchain_id)

    # ── Binding Resolver ────────────────────────────────────

    def resolve_binding(self, config_id: int) -> dict | None:
        try:
            binding = TestConfigAiBindingTable.get(
                TestConfigAiBindingTable.config == config_id
            )
        except DoesNotExist:
            return None

        tc_id = binding.toolchain_id
        tc = self.get_toolchain(tc_id) if tc_id else None
        effective_timeout = binding.timeout_ms
        if effective_timeout is None and tc is not None:
            effective_timeout = tc.default_timeout_ms

        return {
            "binding_id": binding.binding_id,
            "config_id": binding.config_id,
            "enabled": bool(binding.enabled),
            "toolchain_id": tc_id,
            "timeout_ms": binding.timeout_ms,
            "effective_timeout_ms": effective_timeout,
            "params_json": tc.params_json if tc is not None else "{}",
        }

    # ── Binding CRUD (for config UI) ────────────────────────

    def freeze_bound_toolchain(self, config_id: int) -> dict | None:
        """Freeze an enabled binding into the durable analysis envelope shape.

        Production runs must not re-read a mutable binding after acceptance.
        Returning ``None`` keeps AI optional; an enabled but invalid binding
        fails closed instead of silently skipping the configured toolchain.
        """
        binding = self.resolve_binding(config_id)
        if binding is None or not binding["enabled"]:
            return None

        toolchain_id = binding["toolchain_id"]
        if not isinstance(toolchain_id, str) or not toolchain_id.strip():
            raise ValueError("Enabled AI binding has no toolchain selected")
        toolchain = self.get_toolchain(toolchain_id)
        if toolchain is None or not toolchain.enabled:
            raise ValueError("Bound AI toolchain was not found or is disabled")
        if toolchain.protocol_version != _SUPPORTED_PROTOCOL_VERSION:
            raise ValueError("Bound AI toolchain protocol version is unsupported")

        execution_mode = str(toolchain.execution_mode or "").strip()
        if execution_mode not in _SUPPORTED_EXECUTION_MODES:
            raise ValueError("Bound AI toolchain execution mode is unsupported")
        entrypoint = str(toolchain.entrypoint or "").strip()
        if not entrypoint:
            raise ValueError("Bound AI toolchain entrypoint is empty")
        raw_package_dir = str(toolchain.package_dir or "").strip()
        if not raw_package_dir:
            raise ValueError("Bound AI toolchain package directory is invalid")
        package_dir = os.path.abspath(raw_package_dir)
        if not os.path.isdir(package_dir):
            raise ValueError("Bound AI toolchain package directory is invalid")

        timeout_ms = binding["effective_timeout_ms"]
        if (
            isinstance(timeout_ms, bool)
            or not isinstance(timeout_ms, int)
            or timeout_ms <= 0
        ):
            raise ValueError("Bound AI toolchain timeout is invalid")
        params = json.loads(self._normalize_params_json(binding["params_json"]))

        revision_payload = {
            "toolchain_id": toolchain.toolchain_id,
            "updated_time": str(toolchain.updated_time or ""),
            "version": str(toolchain.version or ""),
            "execution_mode": execution_mode,
            "entrypoint": entrypoint,
            "package_dir": package_dir,
            "params": params,
            "timeout_ms": timeout_ms,
        }
        revision_bytes = json.dumps(
            revision_payload,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return {
            "toolchain_id": toolchain.toolchain_id,
            "execution_revision": (
                "sha256:" + hashlib.sha256(revision_bytes).hexdigest()
            ),
            "execution_mode": execution_mode,
            "entrypoint": entrypoint,
            "package_dir": package_dir,
            "params": params,
            "timeout_seconds": float(timeout_ms) / 1000.0,
        }

    def _binding_table_has_legacy_offset_column(self, database) -> bool:
        cursor = database.execute_sql("PRAGMA table_info(test_config_ai_binding_table)")
        return any(row[1] == "analysis_window_offset_ms" for row in cursor.fetchall())

    def _create_binding_with_legacy_offset(self, config_id: int, data: dict) -> int:
        from consts.time_consts import now_str

        database = TestConfigAiBindingTable._meta.database
        cursor = database.execute_sql(
            """
            INSERT INTO test_config_ai_binding_table (
                config_id,
                enabled,
                toolchain_id,
                timeout_ms,
                analysis_window_offset_ms,
                params_json,
                updated_time
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                config_id,
                data.get("enabled", False),
                data.get("toolchain_id"),
                data.get("timeout_ms"),
                0,
                "{}",
                now_str(),
            ),
        )
        return cursor.lastrowid

    def save_binding(self, config_id: int, data: dict) -> int:
        """Create or update the AI binding for a test config.
        """
        if "score_threshold" in data:
            raise ValueError("score_threshold has been removed")

        database = TestConfigAiBindingTable._meta.database
        with database.atomic():
            try:
                existing = TestConfigAiBindingTable.get(
                    TestConfigAiBindingTable.config == config_id
                )
                existing.enabled = data.get("enabled", False)
                existing.toolchain_id = data.get("toolchain_id")
                existing.timeout_ms = data.get("timeout_ms")
                existing.params_json = "{}"
                from consts.time_consts import now_str
                existing.updated_time = now_str()
                existing.save()
                return existing.binding_id
            except DoesNotExist:
                if self._binding_table_has_legacy_offset_column(database):
                    return self._create_binding_with_legacy_offset(config_id, data)
                row = TestConfigAiBindingTable.create(
                    config=config_id,
                    enabled=data.get("enabled", False),
                    toolchain=data.get("toolchain_id"),
                    timeout_ms=data.get("timeout_ms"),
                    params_json="{}",
                )
                return row.binding_id

    def clear_binding(self, config_id: int):
        """Clear toolchain selection and disable AI for a config.

        Preserves the binding row (never deletes it) so UI state round-trips.
        """
        try:
            existing = TestConfigAiBindingTable.get(
                TestConfigAiBindingTable.config == config_id
            )
        except DoesNotExist:
            return  # no binding to clear

        existing.enabled = False
        existing.toolchain_id = None
        from consts.time_consts import now_str
        existing.updated_time = now_str()
        existing.save()

    # ── Preflight Checker ───────────────────────────────────

    def preflight_check(
        self, config_id: int, segments: list[dict]
    ) -> dict:
        """Run preflight checks. Returns dict with 'status' and optional 'error'."""
        binding = self.resolve_binding(config_id)
        if binding is None:
            return {"status": "not_configured", "error": "No AI binding for this config"}

        if not binding["enabled"]:
            return {"status": "not_enabled", "error": "AI is disabled for this config"}

        tc_id = binding["toolchain_id"]
        if tc_id is None:
            return {"status": "not_configured", "error": "No toolchain selected"}

        tc = self.get_toolchain(tc_id)
        if tc is None or not tc.enabled:
            return {
                "status": "not_configured",
                "error": "Toolchain not found or disabled",
            }
        if not getattr(tc, "package_dir", "").strip() or not os.path.isdir(tc.package_dir):
            return {
                "status": "preflight_error",
                "error": "Toolchain package_dir is missing or invalid",
            }

        # Validate params_json
        try:
            self._normalize_params_json(binding["params_json"])
        except ValueError as exc:
            return {
                "status": "preflight_error",
                "error": str(exc),
            }

        # Validate segments
        if not segments:
            return {"status": "preflight_error", "error": "segments is empty"}

        for seg in segments:
            audio_path = seg.get("audio_path", "")
            if not audio_path or not os.path.isfile(audio_path):
                return {
                    "status": "preflight_error",
                    "error": f"Audio file not found: {audio_path}",
                }
            if not os.access(audio_path, os.R_OK):
                return {
                    "status": "preflight_error",
                    "error": f"Audio file not readable: {audio_path}",
                }

        return {"status": "ok", "binding": binding, "toolchain": tc}

    # ── Request Builder ─────────────────────────────────────

    def build_request(
        self,
        request_id: str,
        test_run_id: str,
        toolchain_id: str,
        segments: list[dict],
        context: dict,
        params: dict,
        window_info: dict | None = None,
    ) -> dict:
        return {
            "protocol_version": 1,
            "request_id": request_id,
            "test_run_id": test_run_id,
            "toolchain_id": toolchain_id,
            "segments": segments,
            "context": context,
            "params": params,
            "window_info": window_info or {},
        }

    # ── Executor (oneshot process) ──────────────────────────

    def execute_toolchain(
        self,
        *,
        entrypoint: str,
        request_json_path: str,
        response_json_path: str,
        artifacts_dir: str,
        timeout_ms: int,
        package_dir: str | None = None,
    ) -> dict:
        """Execute external toolchain process. Returns execution envelope dict."""
        started_at = _now_iso()
        t0 = time.monotonic()

        # Use shlex.split with posix=False on Windows for correct path handling
        posix = sys.platform != "win32"
        cmd = [*shlex.split(entrypoint, posix=posix),
               "--request", request_json_path,
               "--response", response_json_path,
               "--artifacts-dir", artifacts_dir]

        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=timeout_ms / 1000.0,
                cwd=package_dir or None,
                creationflags=ai_subprocess_creation_flags(),
            )
            elapsed_ms = int((time.monotonic() - t0) * 1000)
            finished_at = _now_iso()

            return {
                "started_at": started_at,
                "finished_at": finished_at,
                "duration_ms": elapsed_ms,
                "exit_code": result.returncode,
                "stdout_excerpt": (result.stdout or "")[:2000],
                "stderr_excerpt": (result.stderr or "")[:2000],
            }
        except subprocess.TimeoutExpired:
            elapsed_ms = int((time.monotonic() - t0) * 1000)
            return {
                "started_at": started_at,
                "finished_at": _now_iso(),
                "duration_ms": elapsed_ms,
                "exit_code": None,
                "stdout_excerpt": None,
                "stderr_excerpt": None,
                "timeout": True,
            }
        except Exception as e:
            elapsed_ms = int((time.monotonic() - t0) * 1000)
            return {
                "started_at": started_at,
                "finished_at": _now_iso(),
                "duration_ms": elapsed_ms,
                "exit_code": None,
                "stdout_excerpt": None,
                "stderr_excerpt": str(e)[:2000],
                "process_error": True,
            }

    # ── Result Validator ────────────────────────────────────

    def _validate_response_payload(self, data: object, request_id: str) -> dict:
        if not isinstance(data, dict):
            return {"status": "invalid_output", "error": "Root must be a JSON object"}

        # Protocol consistency
        if data.get("protocol_version") != 1:
            return {"status": "protocol_error", "error": "protocol_version mismatch"}
        if data.get("request_id") != request_id:
            return {"status": "protocol_error", "error": "request_id mismatch"}

        # ai_result presence (required field => protocol_error if missing)
        ai_result = data.get("ai_result")
        if not isinstance(ai_result, dict):
            return {"status": "protocol_error", "error": "ai_result is required and must be a dict"}

        if "ai_label" not in ai_result:
            return {"status": "protocol_error", "error": "ai_result.ai_label is required"}

        # ai_label must be a string
        if not isinstance(ai_result["ai_label"], str):
            return {"status": "invalid_output", "error": "ai_result.ai_label must be a string"}

        # Type checks on optional numeric fields (reject bool — bool is subclass of int)
        for field in ("score", "confidence"):
            if field in ai_result:
                val = ai_result[field]
                if isinstance(val, bool) or not isinstance(val, (int, float)):
                    return {"status": "invalid_output", "error": f"ai_result.{field} must be numeric (not bool)"}

        # Validate artifact paths — must be relative, no path traversal
        artifacts = ai_result.get("artifacts", [])
        if not isinstance(artifacts, list):
            return {"status": "invalid_output", "error": "ai_result.artifacts must be a list"}
        for art in artifacts:
            if not isinstance(art, dict):
                return {"status": "invalid_output", "error": "artifact must be a dict"}
            rel = art.get("relative_path", "")
            if not isinstance(rel, str) or not rel:
                return {
                    "status": "invalid_output",
                    "error": f"artifact relative_path must be a non-empty string",
                }
            if ".." in rel.replace("\\", "/").split("/"):
                return {
                    "status": "invalid_output",
                    "error": f"artifact relative_path is invalid or escapes artifacts dir: {rel}",
                }
            # Reject absolute paths and Windows drive-relative paths (e.g. "C:file.txt")
            if os.path.isabs(rel) or os.path.splitdrive(rel)[0]:
                return {
                    "status": "invalid_output",
                    "error": f"artifact relative_path must not be absolute: {rel}",
                }

        # Validate optional string fields from ai_result
        if "summary" in ai_result and not isinstance(ai_result["summary"], str):
            return {"status": "invalid_output", "error": "ai_result.summary must be a string"}

        # Validate top-level optional string fields
        for field in ("toolchain_name", "toolchain_version"):
            if field in data and not isinstance(data[field], str):
                return {"status": "invalid_output", "error": f"{field} must be a string"}

        return {
            "status": "success",
            "raw_response": data,
            "ai_result": ai_result,
            "toolchain_name": data.get("toolchain_name"),
            "toolchain_version": data.get("toolchain_version"),
        }

    def validate_response(
        self, response_json_path: str, request_id: str
    ) -> dict:
        """Validate response.json. Returns dict with 'status' and optional 'ai_result'."""
        if not os.path.isfile(response_json_path):
            return {"status": "protocol_error", "error": "response.json not found"}

        try:
            with open(response_json_path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (json.JSONDecodeError, UnicodeDecodeError) as e:
            return {"status": "invalid_output", "error": f"Invalid JSON: {e}"}

        return self._validate_response_payload(data, request_id)

    # ── Persistence ─────────────────────────────────────────

    def save_execution(self, envelope: dict) -> AiExecutionTable:
        """Persist an AI execution record."""
        persisted_fields = {
            key: value
            for key, value in envelope.items()
            if key in AiExecutionTable._meta.fields
        }
        return AiExecutionTable.create(**persisted_fields)

    def mark_previous_ineffective(self, test_run_id: str):
        """Set is_current_effective=False for all records of a test run."""
        (
            AiExecutionTable.update(is_current_effective=False)
            .where(AiExecutionTable.test_run_id == test_run_id)
            .execute()
        )

    def get_current_effective(self, test_run_id: str) -> AiExecutionTable | None:
        """Get the current effective AI result for a test run, if any."""
        try:
            return AiExecutionTable.get(
                AiExecutionTable.test_run_id == test_run_id,
                AiExecutionTable.is_current_effective == True,
            )
        except DoesNotExist:
            return None

    def get_current_effective_export_values(
        self, run_ids: tuple[str, ...]
    ) -> dict[str, AiExportValue]:
        """Return current successful AI display values for selected runs."""
        if type(run_ids) is not tuple or not run_ids:
            raise ValueError("run_ids must be a nonempty tuple")
        for run_id in run_ids:
            if type(run_id) is not str:
                raise ValueError("run_ids must contain canonical UUIDv4 strings")
            try:
                parsed = uuid.UUID(run_id)
            except ValueError:
                raise ValueError(
                    "run_ids must contain canonical UUIDv4 strings"
                ) from None
            if parsed.version != 4 or str(parsed) != run_id:
                raise ValueError(
                    "run_ids must contain canonical UUIDv4 strings"
                )
        if len(set(run_ids)) != len(run_ids):
            raise ValueError("run_ids must be unique")

        unavailable = AiExportValue(None, None)
        values = {run_id: unavailable for run_id in run_ids}
        found_run_ids: set[str] = set()
        rows = (
            AiExecutionTable.select(
                AiExecutionTable.test_run_id,
                AiExecutionTable.score,
                AiExecutionTable.ai_label,
            )
            .where(
                AiExecutionTable.test_run_id.in_(run_ids),
                AiExecutionTable.is_current_effective == True,
                AiExecutionTable.module_status == "success",
            )
            .order_by(
                AiExecutionTable.started_time.desc(),
                AiExecutionTable.request_id.desc(),
            )
        )
        for row in rows:
            if row.test_run_id in found_run_ids:
                continue
            found_run_ids.add(row.test_run_id)

            score = row.score
            if isinstance(score, bool) or not isinstance(score, (int, float)):
                score = None
            else:
                try:
                    score = float(score)
                except (TypeError, ValueError, OverflowError):
                    score = None
                else:
                    if not math.isfinite(score):
                        score = None

            label = row.ai_label
            if not isinstance(label, str) or not label.strip():
                label = None
            values[row.test_run_id] = AiExportValue(score, label)
        return values

    # ── Orchestrator (full run) ─────────────────────────────

    def _execute_ai_transport(
        self,
        *,
        toolchain: AiToolchainTable,
        binding: dict,
        request_id: str,
        request_data: dict,
        request_json_path: str,
        response_json_path: str,
        artifacts_dir: str,
    ) -> tuple[dict, dict | None, str]:
        if toolchain.execution_mode == "resident_process":
            resident_result = self._resident_runtime.analyze(
                toolchain_id=toolchain.toolchain_id,
                entrypoint=toolchain.entrypoint,
                package_dir=toolchain.package_dir,
                request_id=request_id,
                request_payload=request_data,
                artifacts_dir=artifacts_dir,
                timeout_ms=binding["effective_timeout_ms"],
            )
            exec_result = {
                "started_at": resident_result.get("started_at"),
                "finished_at": resident_result.get("finished_at"),
                "duration_ms": resident_result.get("duration_ms"),
                "exit_code": None,
                "error_summary": resident_result.get("error_summary"),
                "stdout_excerpt": resident_result.get("stdout_excerpt"),
                "stderr_excerpt": resident_result.get("stderr_excerpt"),
            }
            validation = None
            module_status = resident_result["module_status"]
            if module_status == "success":
                validation = self._validate_response_payload(
                    resident_result["response_payload"],
                    request_id,
                )
                module_status = validation["status"]
                if module_status == "success":
                    try:
                        with open(response_json_path, "w", encoding="utf-8") as f:
                            json.dump(
                                validation["raw_response"],
                                f,
                                ensure_ascii=False,
                                indent=2,
                            )
                    except OSError as e:
                        if os.path.exists(response_json_path):
                            try:
                                os.remove(response_json_path)
                            except OSError:
                                pass
                        validation = None
                        module_status = "process_error"
                        exec_result["error_summary"] = (
                            f"Failed to persist response.json: {e}"
                        )
            return exec_result, validation, module_status

        if toolchain.execution_mode != "oneshot_process":
            started_at = _now_iso()
            exec_result = {
                "started_at": started_at,
                "finished_at": started_at,
                "duration_ms": 0,
                "exit_code": None,
                "error_summary": f"Unsupported execution_mode: {toolchain.execution_mode}",
                "stdout_excerpt": None,
                "stderr_excerpt": (
                    f"Unsupported execution_mode: {toolchain.execution_mode}"
                )[:2000],
                "process_error": True,
            }
            return exec_result, None, "process_error"

        exec_result = self.execute_toolchain(
            entrypoint=toolchain.entrypoint,
            package_dir=toolchain.package_dir,
            request_json_path=request_json_path,
            response_json_path=response_json_path,
            artifacts_dir=artifacts_dir,
            timeout_ms=binding["effective_timeout_ms"],
        )

        validation = None
        if exec_result.get("timeout"):
            module_status = "timeout"
        elif exec_result.get("process_error"):
            module_status = "process_error"
        elif exec_result["exit_code"] != 0:
            module_status = "process_error"
        else:
            validation = self.validate_response(response_json_path, request_id)
            module_status = validation["status"]
        return exec_result, validation, module_status

    def run_ai_analysis(
        self,
        config_id: int,
        test_run_id: str,
        segments: list[dict],
        context: dict,
        ai_runs_dir: str,
        window_info: dict | None = None,
    ) -> dict:
        """Full AI analysis orchestration. Returns the execution envelope dict."""
        request_id = _gen_request_id()

        # 0. Clear previous effective BEFORE anything (spec: any new attempt invalidates old result)
        self.mark_previous_ineffective(test_run_id)

        # 1. Preflight
        preflight = self.preflight_check(config_id, segments)
        if preflight["status"] != "ok":
            envelope = {
                "request_id": request_id,
                "test_run_id": test_run_id,
                "config_id": config_id,
                "toolchain_id": "",
                "module_status": preflight["status"],
                "started_time": _now_iso(),
                "finished_time": _now_iso(),
                "error_summary": preflight.get("error"),
                "is_current_effective": False,
            }
            self.save_execution(envelope)
            return envelope

        binding = preflight["binding"]
        tc = preflight["toolchain"]

        # 2. Prepare work directory
        work_dir = os.path.join(ai_runs_dir, request_id)
        artifacts_dir = os.path.join(work_dir, "artifacts")
        try:
            os.makedirs(artifacts_dir, exist_ok=True)
        except OSError as e:
            envelope = {
                "request_id": request_id,
                "test_run_id": test_run_id,
                "config_id": config_id,
                "toolchain_id": tc.toolchain_id,
                "module_status": "preflight_error",
                "started_time": _now_iso(),
                "finished_time": _now_iso(),
                "error_summary": f"Cannot create work directory: {e}",
                "is_current_effective": False,
            }
            self.save_execution(envelope)
            return envelope

        request_json_path = os.path.join(work_dir, "request.json")
        response_json_path = os.path.join(work_dir, "response.json")

        # 3. Build & write request
        params = json.loads(binding["params_json"])
        request_data = self.build_request(
            request_id=request_id,
            test_run_id=test_run_id,
            toolchain_id=tc.toolchain_id,
            segments=segments,
            context=context,
            params=params,
            window_info=window_info,
        )
        with open(request_json_path, "w", encoding="utf-8") as f:
            json.dump(request_data, f, ensure_ascii=False, indent=2)

        # 4. Execute
        exec_result, validation, module_status = self._execute_ai_transport(
            toolchain=tc,
            binding=binding,
            request_id=request_id,
            request_data=request_data,
            request_json_path=request_json_path,
            response_json_path=response_json_path,
            artifacts_dir=artifacts_dir,
        )

        # 7. Build envelope
        wi = window_info or {}
        envelope = {
            "request_id": request_id,
            "test_run_id": test_run_id,
            "config_id": config_id,
            "toolchain_id": tc.toolchain_id,
            "module_status": module_status,
            "started_time": exec_result.get("started_at"),
            "finished_time": exec_result.get("finished_at"),
            "duration_ms": exec_result.get("duration_ms"),
            "exit_code": exec_result.get("exit_code"),
            "error_summary": None,
            "stdout_excerpt": exec_result.get("stdout_excerpt"),
            "stderr_excerpt": exec_result.get("stderr_excerpt"),
            "toolchain_name": None,
            "toolchain_version": None,
            "protocol_version": 1,
            "window_overridden": wi.get("window_overridden", False),
            "default_window_offset_ms": wi.get("default_window_offset_ms", 0),
            "default_window_duration_ms": wi.get("default_window_duration_ms", 0),
            "final_window_offset_ms": wi.get("final_window_offset_ms", 0),
            "final_window_duration_ms": wi.get("final_window_duration_ms", 0),
            "is_current_effective": False,
            "ai_label": None,
            "score": None,
            "summary": None,
            "confidence": None,
            "extra_json": None,
            "artifacts_json": None,
            "raw_response_json": None,
            "request_json_path": request_json_path,
            "response_json_path": response_json_path,
            "artifacts_dir": artifacts_dir,
        }

        # Set error_summary based on failure type
        if module_status == "timeout":
            envelope["error_summary"] = "Process timed out"
        elif module_status == "process_error":
            envelope["error_summary"] = (
                exec_result.get("error_summary")
                or exec_result.get("stderr_excerpt")
                or f"Exit code {exec_result.get('exit_code')}"
            )
        elif module_status in ("protocol_error", "invalid_output"):
            envelope["error_summary"] = (
                validation.get("error")
                if validation
                else exec_result.get("error_summary")
            )

        if module_status == "success":
            ai_result = validation["ai_result"]
            decision_threshold, threshold_source = _resolve_effective_decision_threshold(
                ai_result,
                params,
            )
            envelope["toolchain_name"] = validation.get("toolchain_name")
            envelope["toolchain_version"] = validation.get("toolchain_version")
            envelope["ai_label"] = ai_result.get("ai_label")
            envelope["score"] = ai_result.get("score")
            envelope["summary"] = ai_result.get("summary")
            envelope["confidence"] = ai_result.get("confidence")
            envelope["decision_threshold"] = decision_threshold
            envelope["decision_threshold_source"] = threshold_source
            envelope["extra_json"] = json.dumps(
                ai_result.get("extra", {}), ensure_ascii=False
            )

            # Normalize artifact paths to absolute
            artifacts = ai_result.get("artifacts", [])
            normalized = []
            for art in artifacts:
                normalized.append({
                    "type": art.get("type"),
                    "relative_path": art.get("relative_path"),
                    "absolute_path": os.path.join(
                        artifacts_dir, art.get("relative_path", "")
                    ),
                })
            envelope["artifacts_json"] = json.dumps(normalized, ensure_ascii=False)
            envelope["raw_response_json"] = json.dumps(
                validation["raw_response"], ensure_ascii=False
            )

            # Mark as current effective
            envelope["is_current_effective"] = True

        # 8. Persist
        self.save_execution(envelope)
        return envelope

    def get_latest_execution(self, test_run_id: str) -> AiExecutionTable | None:
        """Get the most recent AI execution for a test run (by started_time desc, then request_id desc)."""
        try:
            return (
                AiExecutionTable.select()
                .where(AiExecutionTable.test_run_id == test_run_id)
                .order_by(
                    AiExecutionTable.started_time.desc(),
                    AiExecutionTable.request_id.desc(),
                )
                .get()
            )
        except DoesNotExist:
            return None

    # ── UI ViewModel ────────────────────────────────────────

    def get_ai_view_model(self, test_run_id: str) -> dict:
        """Get AI display data for the result UI.

        Prefers the current effective record. If none exists (e.g. after a failed
        rerun cleared the old effective), falls back to the latest execution
        so failure states are visible.
        """
        record = self.get_current_effective(test_run_id)
        if record is None:
            record = self.get_latest_execution(test_run_id)
        if record is None:
            return {"has_result": False, "display_status": "no_result"}

        if record.module_status == "success":
            return {
                "has_result": True,
                "display_status": "success",
                "ai_label": record.ai_label,
                "ai_score": record.score,
                "ai_summary": record.summary,
                "ai_status": record.module_status,
                "confidence": record.confidence,
                "toolchain_name": record.toolchain_name,
                "toolchain_version": record.toolchain_version,
                "duration_ms": record.duration_ms,
                "artifacts_json": record.artifacts_json,
            }
        else:
            return {
                "has_result": True,
                "display_status": record.module_status,
                "ai_label": None,
                "ai_score": None,
                "ai_summary": None,
                "ai_status": record.module_status,
                "error_summary": record.error_summary,
            }
