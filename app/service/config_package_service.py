from __future__ import annotations

from contextlib import nullcontext
import json
import ntpath
import os
import re
import hashlib
import logging
import shutil
import stat
import tempfile
import uuid
import zipfile
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Literal

from consts.running_consts import base_dir
from consts.time_consts import now_str
from model.ai_toolchain_table import AiToolchainTable
from model.test_config_ai_binding_table import TestConfigAiBindingTable
from model.test_config_table import TestConfigTable

if TYPE_CHECKING:
    from app.service.ai_service import AiService
    from app.service.config_service import ConfigService


FORMAT = "jingcheng-config-package"
FORMAT_VERSION = 1
MAX_UNCOMPRESSED_BYTES = 4 * 1024 * 1024 * 1024
MAX_FILE_COUNT = 50_000
MAX_NORMALIZED_PATH_LENGTH_WINDOWS = 240

_CONFIG_ENTRY = "config/config.json"
_MANIFEST_ENTRY = "manifest.json"
_AI_TOOLCHAIN_ENTRY = "ai/toolchain.json"
_AI_PACKAGE_PREFIX = "ai/package"
_GOLDEN_PREFIX = "golden_samples"
_AI_MANAGED_ROOT_PARTS = ("data", "ai_toolchains")
_TOOLCHAIN_MARKER_NAME = ".jingcheng_toolchain_id"
_TOOLCHAIN_METADATA_FIELDS = (
    "toolchain_id",
    "display_name",
    "version",
    "entrypoint",
    "protocol_version",
    "execution_mode",
    "default_timeout_ms",
    "enabled",
    "description",
    "source_package_dir",
)
_OPTIONAL_TOOLCHAIN_METADATA_FIELDS = ("package_archive_path",)
logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ConfigPackageExportOptions:
    include_golden_samples: bool = False
    include_ai_toolchain: bool = False
    include_ai_package: bool = False


@dataclass(frozen=True)
class ConfigPackagePreview:
    config_name: str
    contents: dict
    config_conflict: bool
    ai_toolchain_id: str | None
    ai_conflict: bool
    config_action_options: tuple[str, ...]
    default_config_action: str
    ai_action_options: tuple[str, ...]
    default_ai_action: str | None
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True)
class ConfigPackageImportDecision:
    config_action: Literal["create", "overwrite", "use_existing_config", "cancel"] = "create"
    ai_action: Literal[
        "create",
        "overwrite",
        "use_existing_toolchain",
        "clear_binding",
        "cancel",
    ] = "create"


@dataclass(frozen=True)
class ConfigPackageImportResult:
    status: Literal["created", "overwritten", "kept_existing", "cancelled"]
    config_id: int | None
    config_name: str
    ai_binding_status: Literal[
        "created_toolchain",
        "overwrote_toolchain",
        "used_existing_toolchain",
        "cleared_binding",
        "no_ai_metadata",
        "cancelled",
        "unchanged",
    ]
    warnings: tuple[str, ...] = ()
    info_message: str | None = None


@dataclass(frozen=True)
class _ArchiveInspection:
    manifest: dict
    contents: dict[str, bool]
    config_payload: dict
    ai_toolchain_metadata: dict | None
    file_entries: dict[str, zipfile.ZipInfo]


@dataclass(frozen=True)
class _RestoredAiPackage:
    package_dir: str
    backup_dir: str | None = None


class ConfigPackageService:
    def __init__(self, *, config_service: ConfigService, ai_service: AiService):
        self._config_service = config_service
        self._ai_service = ai_service
        self._golden_samples = config_service.golden_sample_service

    def export_package(
        self,
        *,
        config_id: int,
        target_path: str,
        options: ConfigPackageExportOptions,
    ) -> None:
        if not isinstance(options, ConfigPackageExportOptions):
            raise ValueError("options must be ConfigPackageExportOptions")
        if options.include_ai_package and not options.include_ai_toolchain:
            raise ValueError("include_ai_package requires include_ai_toolchain")

        config_payload = self._config_service.load_config(config_id)
        ai_binding = self._serialize_ai_binding(self._ai_service.resolve_binding(config_id))
        package_payload = {
            "config": config_payload,
            "ai_binding": ai_binding,
            "golden_sample_assets": {},
        }
        manifest_contents = {
            "config": True,
            "golden_samples": False,
            "ai_toolchain": False,
            "ai_package": False,
        }

        registry = self._new_archive_registry()
        file_count = 0
        total_bytes = 0
        staged_entries: list[tuple[str, bytes]] = []
        staged_files: list[tuple[str, str]] = []

        golden_assets = self._collect_golden_assets(config_payload, include_assets=options.include_golden_samples)
        if golden_assets:
            manifest_contents["golden_samples"] = True
            package_payload["golden_sample_assets"] = {
                str(channel_index): archive_name
                for channel_index, archive_name, _path in golden_assets
            }
            for _channel_index, archive_name, source_path in golden_assets:
                normalized_name, file_count, total_bytes = self._track_archive_entry(
                    archive_name,
                    size=os.path.getsize(source_path),
                    registry=registry,
                    file_count=file_count,
                    total_bytes=total_bytes,
                )
                staged_files.append((normalized_name, source_path))

        toolchain = None
        if options.include_ai_toolchain:
            toolchain = self._resolve_bound_toolchain(ai_binding)
            toolchain_metadata = self._serialize_toolchain_metadata(
                toolchain=toolchain,
                include_package=options.include_ai_package,
            )
            manifest_contents["ai_toolchain"] = True
            toolchain_bytes = self._json_bytes(toolchain_metadata)
            normalized_name, file_count, total_bytes = self._track_archive_entry(
                _AI_TOOLCHAIN_ENTRY,
                size=len(toolchain_bytes),
                registry=registry,
                file_count=file_count,
                total_bytes=total_bytes,
            )
            staged_entries.append((normalized_name, toolchain_bytes))

        if options.include_ai_package:
            assert toolchain is not None
            ai_package_files = self._iter_ai_package_files(toolchain.package_dir)
            if not ai_package_files:
                raise ValueError("AI package directory is empty or contains no regular files")
            manifest_contents["ai_package"] = True
            for source_path, archive_name, file_size in ai_package_files:
                normalized_name, file_count, total_bytes = self._track_archive_entry(
                    archive_name,
                    size=file_size,
                    registry=registry,
                    file_count=file_count,
                    total_bytes=total_bytes,
                )
                staged_files.append((normalized_name, source_path))

        config_bytes = self._json_bytes(package_payload)
        manifest_bytes = self._json_bytes(
            {
                "format": FORMAT,
                "format_version": FORMAT_VERSION,
                "app": "JingChengMechanical",
                "exported_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                "contents": manifest_contents,
            }
        )
        manifest_name, file_count, total_bytes = self._track_archive_entry(
            _MANIFEST_ENTRY,
            size=len(manifest_bytes),
            registry=registry,
            file_count=file_count,
            total_bytes=total_bytes,
        )
        config_name, file_count, total_bytes = self._track_archive_entry(
            _CONFIG_ENTRY,
            size=len(config_bytes),
            registry=registry,
            file_count=file_count,
            total_bytes=total_bytes,
        )
        staged_entries.insert(0, (config_name, config_bytes))
        staged_entries.insert(0, (manifest_name, manifest_bytes))

        target = os.path.abspath(str(target_path))
        target_dir = os.path.dirname(target) or os.getcwd()
        os.makedirs(target_dir, exist_ok=True)
        fd, temp_path = tempfile.mkstemp(prefix=".config-package-", suffix=".tmp", dir=target_dir)
        os.close(fd)
        try:
            with zipfile.ZipFile(temp_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
                for archive_name, payload in staged_entries:
                    zf.writestr(archive_name, payload)
                for archive_name, source_path in staged_files:
                    zf.write(source_path, arcname=archive_name)
            os.replace(temp_path, target)
        except Exception:
            if os.path.exists(temp_path):
                os.remove(temp_path)
            raise

    def inspect_package(self, path: str) -> ConfigPackagePreview:
        inspection = self._inspect_archive(path)
        return self._build_preview(inspection)

    def import_package(
        self,
        path: str,
        decision: ConfigPackageImportDecision,
    ) -> ConfigPackageImportResult:
        if not isinstance(decision, ConfigPackageImportDecision):
            raise ValueError("decision must be ConfigPackageImportDecision")

        inspection = self._inspect_archive(path)
        preview = self._build_preview(inspection)
        existing_config = self._get_config_row_by_name(preview.config_name)
        config_action = self._resolve_requested_action(
            decision.config_action,
            allowed=preview.config_action_options,
            default_action=preview.default_config_action,
            label="config",
        )
        if config_action == "cancel":
            return ConfigPackageImportResult(
                status="cancelled",
                config_id=None,
                config_name=preview.config_name,
                ai_binding_status="cancelled",
                info_message="Import cancelled.",
            )
        if config_action == "use_existing_config":
            if existing_config is None:
                raise ValueError("配置不存在或已被删除")
            return ConfigPackageImportResult(
                status="kept_existing",
                config_id=int(existing_config.config_id),
                config_name=preview.config_name,
                ai_binding_status="unchanged",
                info_message="Existing local config was kept.",
            )

        target_config_id = (
            int(existing_config.config_id)
            if config_action == "overwrite"
            else self._config_service.reserve_import_config_id()
        )
        ai_action = self._resolve_ai_action(decision.ai_action, preview)
        if ai_action == "cancel":
            return ConfigPackageImportResult(
                status="cancelled",
                config_id=None,
                config_name=preview.config_name,
                ai_binding_status="cancelled",
                info_message="Import cancelled.",
            )

        restored_ai: _RestoredAiPackage | None = None
        restored_golden_paths: list[str] = []
        warnings = list(preview.warnings)
        old_golden_paths: list[str] = []
        ai_binding_status = "unchanged"
        toolchain_row_written = False
        binding_target_toolchain_id: str | None = None
        mutation_lease = self._toolchain_import_mutation_lease(
            ai_action=ai_action,
            preview=preview,
        )

        with tempfile.TemporaryDirectory(prefix="config-package-import-") as extract_root:
            self._extract_archive(path, inspection, extract_root)
            with mutation_lease:
                try:
                    restored_golden = self._restore_golden_samples(
                        inspection=inspection,
                        extract_root=extract_root,
                        target_config_id=target_config_id,
                    )
                    restored_golden_paths = [
                        binding["golden_sample_path"]
                        for binding in restored_golden.values()
                        if isinstance(binding, dict)
                        and isinstance(binding.get("golden_sample_path"), str)
                    ]
                    prepared_config = self._config_service.prepare_import_payload(
                        inspection.config_payload["config"],
                        golden_asset_paths_by_channel=restored_golden,
                    )
                    prepared_config["config_id"] = target_config_id
                    config_errors = self._config_service.validate(prepared_config)
                    if config_errors:
                        raise ValueError("\n".join(config_errors))
                    if config_action == "create" and TestConfigTable.select().where(
                        TestConfigTable.config_id == target_config_id
                    ).exists():
                        raise ValueError("导入配置 ID 已被占用")

                    old_golden_paths = (
                        self._config_service._collect_replaced_paths(
                            target_config_id,
                            prepared_config["channels"],
                        )
                        if config_action == "overwrite"
                        else []
                    )

                    normalized_toolchain = None
                    if inspection.ai_toolchain_metadata is None:
                        ai_binding_status = "no_ai_metadata"
                    elif inspection.contents["ai_package"]:
                        if ai_action == "use_existing_toolchain":
                            binding_target_toolchain_id = preview.ai_toolchain_id
                            ai_binding_status = "used_existing_toolchain"
                        else:
                            restored_ai = self._restore_ai_package(
                                inspection=inspection,
                                extract_root=extract_root,
                                toolchain_id=preview.ai_toolchain_id,
                            )
                            normalized_toolchain = self._normalize_imported_toolchain_payload(
                                inspection.ai_toolchain_metadata,
                                package_dir=restored_ai.package_dir,
                            )
                            toolchain_row_written = True
                            binding_target_toolchain_id = normalized_toolchain["toolchain_id"]
                            ai_binding_status = (
                                "overwrote_toolchain"
                                if preview.ai_conflict
                                else "created_toolchain"
                            )
                    else:
                        if ai_action == "use_existing_toolchain":
                            binding_target_toolchain_id = preview.ai_toolchain_id
                            ai_binding_status = "used_existing_toolchain"
                        else:
                            ai_binding_status = "cleared_binding"
                            if not warnings:
                                warnings.append("AI binding was cleared during import.")

                    binding_payload = self._build_import_binding_payload(
                        imported_binding=inspection.config_payload.get("ai_binding"),
                        toolchain_id=binding_target_toolchain_id,
                        clear_binding=ai_binding_status in {"cleared_binding", "no_ai_metadata"},
                    )

                    database = TestConfigTable._meta.database
                    with database.atomic():
                        if normalized_toolchain is not None:
                            self._upsert_imported_toolchain_row(normalized_toolchain)
                        self._persist_imported_config(
                            prepared_config,
                            config_action=config_action,
                        )
                        self._upsert_binding_row(
                            config_id=target_config_id,
                            binding=binding_payload,
                        )
                except Exception:
                    self._rollback_restored_golden_paths(restored_golden_paths)
                    self._rollback_restored_ai_package(restored_ai)
                    raise

                self._invalidate_imported_toolchain(
                    toolchain_row_written=toolchain_row_written,
                    toolchain_id=binding_target_toolchain_id,
                )

            self._config_service._post_commit_cleanup(
                old_paths=old_golden_paths,
                staged_paths=[],
            )
            self._cleanup_restored_ai_backup(restored_ai)
            self._emit_import_signals(toolchain_row_written)
            return ConfigPackageImportResult(
                status="created" if config_action == "create" else "overwritten",
                config_id=target_config_id,
                config_name=preview.config_name,
                ai_binding_status=ai_binding_status,
                warnings=tuple(warnings),
            )

    def _build_preview(self, inspection: _ArchiveInspection) -> ConfigPackagePreview:
        config_root = inspection.config_payload.get("config")
        if not isinstance(config_root, dict):
            raise ValueError("config/config.json must contain a config object")

        config_name = str(config_root.get("config_name") or "").strip()
        if not config_name:
            raise ValueError("config/config.json is missing config_name")

        config_conflict = self._get_config_row_by_name(config_name) is not None
        if config_conflict:
            config_action_options = ("overwrite", "use_existing_config", "cancel")
            default_config_action = "use_existing_config"
        else:
            config_action_options = ("create",)
            default_config_action = "create"

        ai_metadata = inspection.ai_toolchain_metadata
        ai_binding = inspection.config_payload.get("ai_binding")
        warnings: list[str] = []
        ai_toolchain_id = None
        ai_conflict = False
        ai_action_options: tuple[str, ...] = ()
        default_ai_action: str | None = None

        if isinstance(ai_metadata, dict):
            ai_toolchain_id = ai_metadata["toolchain_id"]
            local_toolchain = self._ai_service.get_toolchain(ai_toolchain_id)
            if inspection.contents["ai_package"]:
                if local_toolchain is None:
                    ai_action_options = ("create",)
                    default_ai_action = "create"
                else:
                    ai_conflict = True
                    ai_action_options = ("overwrite", "use_existing_toolchain", "cancel")
                    default_ai_action = "use_existing_toolchain"
            else:
                if local_toolchain is None:
                    ai_action_options = ("clear_binding",)
                    default_ai_action = "clear_binding"
                    warnings.append(
                        "Package did not include a runnable AI package; AI binding will be cleared."
                    )
                else:
                    ai_conflict = True
                    ai_action_options = ("use_existing_toolchain", "clear_binding", "cancel")
                    default_ai_action = "use_existing_toolchain"
        else:
            ai_toolchain_id = self._extract_bound_toolchain_id(ai_binding)
            if ai_toolchain_id:
                warnings.append(
                    "Package did not include AI toolchain metadata; AI binding will be cleared."
                )

        return ConfigPackagePreview(
            config_name=config_name,
            contents=dict(inspection.contents),
            config_conflict=config_conflict,
            ai_toolchain_id=ai_toolchain_id,
            ai_conflict=ai_conflict,
            config_action_options=config_action_options,
            default_config_action=default_config_action,
            ai_action_options=ai_action_options,
            default_ai_action=default_ai_action,
            warnings=tuple(warnings),
        )

    def _get_config_row_by_name(self, config_name: str) -> TestConfigTable | None:
        normalized = str(config_name or "").strip()
        if not normalized:
            return None
        return (
            TestConfigTable.select()
            .where(TestConfigTable.config_name == normalized)
            .first()
        )

    def _resolve_requested_action(
        self,
        requested_action: str | None,
        *,
        allowed: tuple[str, ...],
        default_action: str | None,
        label: str,
    ) -> str | None:
        if requested_action == "cancel":
            return "cancel"
        if not allowed:
            return None
        if requested_action in allowed:
            return requested_action
        if requested_action in (None, "create") and default_action in allowed:
            return default_action
        raise ValueError(f"Unsupported {label} import action: {requested_action}")

    def _resolve_ai_action(
        self,
        requested_action: str | None,
        preview: ConfigPackagePreview,
    ) -> str | None:
        if preview.ai_action_options:
            return self._resolve_requested_action(
                requested_action,
                allowed=preview.ai_action_options,
                default_action=preview.default_ai_action,
                label="AI",
            )
        if requested_action == "cancel":
            return "cancel"
        return None

    def _extract_archive(
        self,
        path: str,
        inspection: _ArchiveInspection,
        extract_root: str,
    ) -> None:
        with zipfile.ZipFile(os.path.abspath(str(path))) as zf:
            for normalized_name, info in inspection.file_entries.items():
                target_path = os.path.abspath(
                    os.path.join(extract_root, *normalized_name.split("/"))
                )
                if not self._is_within_root(target_path, extract_root):
                    raise ValueError("Archive extraction would escape target directory")
                os.makedirs(os.path.dirname(target_path), exist_ok=True)
                with zf.open(info) as src, open(target_path, "wb") as dst:
                    shutil.copyfileobj(src, dst)

    def _restore_golden_samples(
        self,
        *,
        inspection: _ArchiveInspection,
        extract_root: str,
        target_config_id: int,
    ) -> dict[int, dict]:
        golden_assets = inspection.config_payload.get("golden_sample_assets", {})
        if golden_assets is None:
            golden_assets = {}
        if not isinstance(golden_assets, dict):
            raise ValueError("config/config.json golden_sample_assets must be an object")

        restored: dict[int, dict] = {}
        config_root = inspection.config_payload["config"]
        channels = config_root.get("channels", [])
        if not isinstance(channels, list):
            return restored

        for channel in channels:
            if not isinstance(channel, dict):
                continue
            channel_index = channel.get("channel_index")
            if not isinstance(channel_index, int) or isinstance(channel_index, bool):
                continue
            raw_archive_name = golden_assets.get(str(channel_index))
            if raw_archive_name is None:
                continue
            normalized_name = self._normalize_archive_name(raw_archive_name)
            if normalized_name not in inspection.file_entries:
                raise ValueError("Package references a golden sample asset that is missing")
            source_path = os.path.abspath(
                os.path.join(extract_root, *normalized_name.split("/"))
            )
            restored[channel_index] = self._golden_samples.restore_imported_sample(
                config_id=target_config_id,
                channel_index=channel_index,
                source_path=source_path,
                metadata=channel,
            )
        return restored

    def _normalize_imported_toolchain_payload(
        self,
        metadata: dict,
        *,
        package_dir: str,
    ) -> dict:
        payload = {
            "toolchain_id": metadata["toolchain_id"],
            "display_name": metadata["display_name"],
            "version": metadata["version"],
            "entrypoint": metadata["entrypoint"],
            "protocol_version": metadata["protocol_version"],
            "execution_mode": metadata["execution_mode"],
            "default_timeout_ms": metadata["default_timeout_ms"],
            "enabled": metadata["enabled"],
            "description": metadata["description"],
            "package_dir": package_dir,
        }
        original_toolchain_id = (
            payload["toolchain_id"]
            if self._ai_service.get_toolchain(payload["toolchain_id"]) is not None
            else None
        )
        return self._ai_service._normalize_toolchain_payload(
            original_toolchain_id,
            payload,
        )

    def _build_import_binding_payload(
        self,
        *,
        imported_binding: object,
        toolchain_id: str | None,
        clear_binding: bool,
    ) -> dict:
        source = imported_binding if isinstance(imported_binding, dict) else {}
        params_json = source.get("params_json", "{}")
        if not isinstance(params_json, str):
            params_json = json.dumps(params_json, ensure_ascii=False)
        timeout_ms = source.get("timeout_ms")
        if isinstance(timeout_ms, bool) or not isinstance(timeout_ms, int):
            timeout_ms = None
        enabled = bool(source.get("enabled", False)) and toolchain_id is not None
        if clear_binding:
            enabled = False
            toolchain_id = None
        return {
            "enabled": enabled,
            "toolchain_id": toolchain_id,
            "timeout_ms": timeout_ms,
            "params_json": params_json,
        }

    def _upsert_imported_toolchain_row(self, normalized_toolchain: dict) -> None:
        existing = self._ai_service.get_toolchain(normalized_toolchain["toolchain_id"])
        timestamp = now_str()
        if existing is None:
            AiToolchainTable.create(
                **normalized_toolchain,
                created_time=timestamp,
                updated_time=timestamp,
            )
            return

        existing.display_name = normalized_toolchain["display_name"]
        existing.version = normalized_toolchain["version"]
        existing.entrypoint = normalized_toolchain["entrypoint"]
        existing.package_dir = normalized_toolchain["package_dir"]
        existing.default_timeout_ms = normalized_toolchain["default_timeout_ms"]
        existing.enabled = normalized_toolchain["enabled"]
        existing.description = normalized_toolchain["description"]
        existing.protocol_version = normalized_toolchain["protocol_version"]
        existing.execution_mode = normalized_toolchain["execution_mode"]
        existing.updated_time = timestamp
        existing.save()

    def _persist_imported_config(self, prepared_config: dict, *, config_action: str) -> None:
        config_id = prepared_config["config_id"]
        if config_action == "create":
            TestConfigTable.create(
                config_id=config_id,
                created_time=now_str(),
                updated_time=now_str(),
                **self._config_service._config_row_values(prepared_config),
            )
            self._config_service._replace_channel_rows(
                config_id=config_id,
                channels=prepared_config["channels"],
            )
            return

        if not TestConfigTable.select().where(TestConfigTable.config_id == config_id).exists():
            raise ValueError("配置不存在或已被删除")
        (
            TestConfigTable.update(
                **self._config_service._config_row_values(prepared_config),
                updated_time=now_str(),
            )
            .where(TestConfigTable.config_id == config_id)
            .execute()
        )
        self._config_service._replace_channel_rows(
            config_id=config_id,
            channels=prepared_config["channels"],
        )

    def _upsert_binding_row(self, *, config_id: int, binding: dict) -> None:
        timestamp = now_str()
        existing = (
            TestConfigAiBindingTable.select()
            .where(TestConfigAiBindingTable.config == config_id)
            .first()
        )
        if existing is None:
            TestConfigAiBindingTable.create(
                config=config_id,
                enabled=binding["enabled"],
                toolchain=binding["toolchain_id"],
                timeout_ms=binding["timeout_ms"],
                params_json=binding["params_json"],
                updated_time=timestamp,
            )
            return

        existing.enabled = binding["enabled"]
        existing.toolchain = binding["toolchain_id"]
        existing.timeout_ms = binding["timeout_ms"]
        existing.params_json = binding["params_json"]
        existing.updated_time = timestamp
        existing.save()

    def _managed_ai_root(self) -> str:
        return os.path.abspath(os.path.join(base_dir, *_AI_MANAGED_ROOT_PARTS))

    def _restore_ai_package(
        self,
        *,
        inspection: _ArchiveInspection,
        extract_root: str,
        toolchain_id: str | None,
    ) -> _RestoredAiPackage:
        if not isinstance(toolchain_id, str) or not toolchain_id.strip():
            raise ValueError("ai/toolchain.json toolchain_id must not be empty")
        source_prefix = inspection.ai_toolchain_metadata["package_archive_path"]
        normalized_prefix = self._normalize_archive_name(source_prefix)
        source_root = os.path.abspath(
            os.path.join(extract_root, *normalized_prefix.split("/"))
        )
        if not os.path.isdir(source_root):
            raise ValueError("Extracted AI package directory is missing")

        final_dir = self._select_managed_toolchain_dir(toolchain_id)
        backup_dir = None
        if os.path.isdir(final_dir):
            backup_dir = f"{final_dir}.backup.{uuid.uuid4().hex}"
            os.replace(final_dir, backup_dir)
        elif os.path.exists(final_dir):
            raise ValueError("Managed AI package path is occupied by a non-directory entry")

        try:
            shutil.copytree(source_root, final_dir)
            self._write_toolchain_marker(final_dir, toolchain_id)
        except Exception:
            self._remove_managed_dir(final_dir)
            if backup_dir is not None and os.path.isdir(backup_dir):
                os.replace(backup_dir, final_dir)
            raise
        return _RestoredAiPackage(package_dir=final_dir, backup_dir=backup_dir)

    def _select_managed_toolchain_dir(self, toolchain_id: str) -> str:
        root = self._managed_ai_root()
        os.makedirs(root, exist_ok=True)
        digest = hashlib.sha256(toolchain_id.encode("utf-8")).hexdigest()[:16]
        sanitized = re.sub(r"[^A-Za-z0-9._-]+", "_", toolchain_id).strip("._-")
        if not sanitized:
            sanitized = "toolchain"
        base_name = f"{sanitized}_{digest}"
        suffix = 1
        while True:
            candidate_name = base_name if suffix == 1 else f"{base_name}_{suffix}"
            candidate = os.path.abspath(os.path.join(root, candidate_name))
            if not os.path.exists(candidate):
                return candidate
            if self._read_toolchain_marker(candidate) == toolchain_id:
                return candidate
            suffix += 1

    def _read_toolchain_marker(self, package_dir: str) -> str | None:
        marker_path = os.path.join(package_dir, _TOOLCHAIN_MARKER_NAME)
        if not os.path.isfile(marker_path):
            return None
        try:
            with open(marker_path, "r", encoding="utf-8") as marker:
                return marker.read().strip() or None
        except OSError:
            return None

    def _write_toolchain_marker(self, package_dir: str, toolchain_id: str) -> None:
        marker_path = os.path.join(package_dir, _TOOLCHAIN_MARKER_NAME)
        with open(marker_path, "w", encoding="utf-8") as marker:
            marker.write(toolchain_id)

    def _toolchain_import_mutation_lease(
        self,
        *,
        ai_action: str | None,
        preview: ConfigPackagePreview,
    ):
        if ai_action != "overwrite" or not preview.ai_toolchain_id:
            return nullcontext()
        existing = self._ai_service.get_toolchain(preview.ai_toolchain_id)
        if not self._ai_service._is_resident_toolchain(existing):
            return nullcontext()
        return self._ai_service._resident_runtime.hold_mutation_lease(
            preview.ai_toolchain_id
        )

    def _rollback_restored_golden_paths(self, golden_paths: list[str]) -> None:
        if not golden_paths:
            return
        try:
            self._golden_samples.cleanup_paths(golden_paths)
        except Exception as exc:  # pragma: no cover - logging branch
            logger.warning("Failed to rollback imported golden samples: %s", exc)

    def _rollback_restored_ai_package(
        self,
        restored_ai: _RestoredAiPackage | None,
    ) -> None:
        if restored_ai is None:
            return
        self._remove_managed_dir(restored_ai.package_dir)
        if restored_ai.backup_dir and os.path.isdir(restored_ai.backup_dir):
            os.replace(restored_ai.backup_dir, restored_ai.package_dir)

    def _cleanup_restored_ai_backup(self, restored_ai: _RestoredAiPackage | None) -> None:
        if restored_ai is None or restored_ai.backup_dir is None:
            return
        try:
            shutil.rmtree(restored_ai.backup_dir)
        except OSError as exc:  # pragma: no cover - logging branch
            logger.warning("Failed to cleanup AI package backup: %s", exc)

    def _invalidate_imported_toolchain(
        self,
        *,
        toolchain_row_written: bool,
        toolchain_id: str | None,
    ) -> None:
        if toolchain_row_written and toolchain_id:
            self._ai_service._resident_runtime.invalidate(toolchain_id)

    def _emit_import_signals(self, toolchain_row_written: bool) -> None:
        self._config_service.bus.config_changed.emit()
        if toolchain_row_written:
            ai_signal = getattr(self._config_service.bus, "ai_toolchains_changed", None)
            if ai_signal is not None:
                ai_signal.emit()

    def _remove_managed_dir(self, package_dir: str) -> None:
        if (
            not package_dir
            or not os.path.isdir(package_dir)
            or not self._is_within_root(package_dir, self._managed_ai_root())
        ):
            return
        shutil.rmtree(package_dir)

    def _inspect_archive(self, path: str) -> _ArchiveInspection:
        archive_path = os.path.abspath(str(path))
        try:
            with zipfile.ZipFile(archive_path) as zf:
                file_entries, _dir_entries = self._scan_archive_entries(zf)
                manifest_info = file_entries.get(_MANIFEST_ENTRY)
                if manifest_info is None:
                    raise ValueError("manifest.json is missing")
                manifest = self._read_json_entry(zf, manifest_info, "manifest.json")
                contents = self._validate_manifest(manifest)

                config_info = file_entries.get(_CONFIG_ENTRY)
                if config_info is None:
                    raise ValueError("config/config.json is missing")
                config_payload = self._read_json_entry(zf, config_info, "config/config.json")
                self._validate_layout(
                    contents=contents,
                    file_entries=file_entries,
                )
                self._validate_import_config_payload(
                    config_payload=config_payload,
                    contents=contents,
                    file_entries=file_entries,
                )
                ai_toolchain_metadata = None
                ai_toolchain_info = file_entries.get(_AI_TOOLCHAIN_ENTRY)
                if ai_toolchain_info is not None:
                    ai_toolchain_metadata = self._read_json_entry(zf, ai_toolchain_info, "ai/toolchain.json")
                    self._validate_ai_toolchain_metadata(
                        ai_toolchain_metadata,
                        ai_package_included=contents["ai_package"],
                    )
                    self._validate_ai_package_entries(
                        file_entries=file_entries,
                        ai_toolchain_metadata=ai_toolchain_metadata,
                        ai_package_included=contents["ai_package"],
                    )
                    ai_toolchain_metadata = self._normalize_imported_toolchain_metadata(
                        ai_toolchain_metadata
                    )
                elif contents["ai_package"] or contents["ai_toolchain"]:
                    raise ValueError("ai/toolchain.json is missing")

                return _ArchiveInspection(
                    manifest=manifest,
                    contents=contents,
                    config_payload=config_payload,
                    ai_toolchain_metadata=ai_toolchain_metadata,
                    file_entries=file_entries,
                )
        except zipfile.BadZipFile as exc:
            raise ValueError("Package is not a readable ZIP archive") from exc
        except OSError as exc:
            raise ValueError("Package is not a readable ZIP archive") from exc

    def _scan_archive_entries(
        self,
        archive: zipfile.ZipFile,
    ) -> tuple[dict[str, zipfile.ZipInfo], set[str]]:
        file_entries: dict[str, zipfile.ZipInfo] = {}
        dir_entries: set[str] = set()
        registry = self._new_archive_registry()
        file_count = 0
        total_bytes = 0

        for info in archive.infolist():
            kind = self._classify_zip_entry(info)
            normalized_name = self._normalize_archive_name(
                info.filename,
                allow_directory=kind == "dir",
            )
            if not normalized_name:
                continue
            self._check_path_collision(normalized_name, registry)
            if kind == "dir":
                dir_entries.add(normalized_name)
                continue

            file_count += 1
            total_bytes += int(info.file_size)
            self._enforce_entry_limits(
                normalized_name,
                file_count=file_count,
                total_bytes=total_bytes,
            )
            file_entries[normalized_name] = info

        return file_entries, dir_entries

    def _validate_layout(
        self,
        *,
        contents: dict[str, bool],
        file_entries: dict[str, zipfile.ZipInfo],
    ) -> None:
        for entry_name in file_entries:
            if entry_name.startswith("config/") and entry_name != _CONFIG_ENTRY:
                raise ValueError("Version 1 is a single-config package format")

        has_golden_files = any(self._is_under(entry_name, _GOLDEN_PREFIX) for entry_name in file_entries)
        if contents["golden_samples"] and not has_golden_files:
            raise ValueError("Manifest declared golden samples but none were packaged")
        if not contents["golden_samples"] and has_golden_files:
            raise ValueError("Manifest contradicts packaged golden samples")

        has_toolchain_metadata = _AI_TOOLCHAIN_ENTRY in file_entries
        if contents["ai_toolchain"] and not has_toolchain_metadata:
            raise ValueError("ai/toolchain.json is missing")
        if not contents["ai_toolchain"] and has_toolchain_metadata:
            raise ValueError("Manifest contents.ai_toolchain is false but ai/toolchain.json is present")

        has_ai_package_payload = any(
            entry_name == _AI_PACKAGE_PREFIX or self._is_child_of(entry_name, _AI_PACKAGE_PREFIX)
            for entry_name in file_entries
        )
        if not contents["ai_package"] and has_ai_package_payload:
            raise ValueError("Manifest contents.ai_package is false but AI package payload files are present")

        if contents["ai_package"] and not contents["ai_toolchain"]:
            raise ValueError("Manifest contents.ai_package requires contents.ai_toolchain")

    def _validate_import_config_payload(
        self,
        *,
        config_payload: dict,
        contents: dict[str, bool],
        file_entries: dict[str, zipfile.ZipInfo],
    ) -> None:
        config_root = config_payload.get("config")
        if not isinstance(config_root, dict):
            raise ValueError("config/config.json must contain a config object")

        golden_assets = config_payload.get("golden_sample_assets", {})
        if golden_assets is None:
            golden_assets = {}
        if not isinstance(golden_assets, dict):
            raise ValueError("config/config.json golden_sample_assets must be an object")

        if contents["golden_samples"]:
            for archive_name in golden_assets.values():
                if not isinstance(archive_name, str) or not archive_name.strip():
                    raise ValueError("config/config.json golden_sample_assets is invalid")
                normalized_name = self._normalize_archive_name(archive_name)
                if normalized_name not in file_entries:
                    raise ValueError("Manifest declared golden samples but an asset is missing")
        elif golden_assets:
            raise ValueError("Manifest contradicts packaged golden samples")

    def _validate_manifest(self, manifest: dict) -> dict[str, bool]:
        if not isinstance(manifest, dict):
            raise ValueError("manifest.json must contain an object")
        if manifest.get("format") != FORMAT:
            raise ValueError("Unsupported package format")
        if manifest.get("format_version") != FORMAT_VERSION:
            raise ValueError("Unsupported package format_version")
        if not isinstance(manifest.get("app"), str) or not manifest["app"].strip():
            raise ValueError("manifest.json is missing app")
        if not isinstance(manifest.get("exported_at"), str) or not manifest["exported_at"].strip():
            raise ValueError("manifest.json is missing exported_at")
        contents = manifest.get("contents")
        if not isinstance(contents, dict):
            raise ValueError("manifest.json contents is invalid")

        if contents.get("config") is not True:
            raise ValueError("manifest contents.config must be true")

        normalized = {"config": True}
        for key in ("golden_samples", "ai_toolchain", "ai_package"):
            value = contents.get(key, False)
            if not isinstance(value, bool):
                raise ValueError(f"manifest contents.{key} must be boolean")
            normalized[key] = value
        return normalized

    def _validate_ai_toolchain_metadata(
        self,
        metadata: dict,
        *,
        ai_package_included: bool,
    ) -> None:
        if not isinstance(metadata, dict):
            raise ValueError("ai/toolchain.json must contain an object")

        allowed = set(_TOOLCHAIN_METADATA_FIELDS)
        if ai_package_included:
            allowed.update(_OPTIONAL_TOOLCHAIN_METADATA_FIELDS)
        actual = set(metadata)
        if "package_dir" in actual:
            raise ValueError("ai/toolchain.json must not persist package_dir")
        extras = actual - allowed
        missing = set(_TOOLCHAIN_METADATA_FIELDS) - actual
        if extras:
            raise ValueError("ai/toolchain.json contains unexpected fields")
        if missing:
            raise ValueError("ai/toolchain.json is missing required fields")

        for key in (
            "toolchain_id",
            "display_name",
            "version",
            "entrypoint",
            "execution_mode",
            "description",
            "source_package_dir",
        ):
            if not isinstance(metadata.get(key), str):
                raise ValueError(f"ai/toolchain.json field {key} must be a string")
        if not str(metadata["toolchain_id"]).strip():
            raise ValueError("ai/toolchain.json toolchain_id must not be empty")
        if not str(metadata["display_name"]).strip():
            raise ValueError("ai/toolchain.json display_name must not be empty")
        if not str(metadata["entrypoint"]).strip():
            raise ValueError("ai/toolchain.json entrypoint must not be empty")
        if isinstance(metadata.get("enabled"), bool) is False:
            raise ValueError("ai/toolchain.json enabled must be boolean")
        if (
            isinstance(metadata.get("protocol_version"), int) is False
            or isinstance(metadata.get("protocol_version"), bool)
        ):
            raise ValueError("ai/toolchain.json protocol_version must be integer")
        if (
            isinstance(metadata.get("default_timeout_ms"), int) is False
            or isinstance(metadata.get("default_timeout_ms"), bool)
            or metadata["default_timeout_ms"] <= 0
        ):
            raise ValueError("ai/toolchain.json default_timeout_ms must be positive integer")
        package_archive_path = metadata.get("package_archive_path")
        if ai_package_included:
            if not isinstance(package_archive_path, str) or not package_archive_path.strip():
                raise ValueError("ai/toolchain.json must include package_archive_path")
            self._normalize_archive_name(package_archive_path)
        elif package_archive_path is not None:
            raise ValueError("package_archive_path is only valid when ai_package is included")

    def _validate_ai_package_entries(
        self,
        *,
        file_entries: dict[str, zipfile.ZipInfo],
        ai_toolchain_metadata: dict,
        ai_package_included: bool,
    ) -> None:
        if not ai_package_included:
            return
        package_archive_path = ai_toolchain_metadata.get("package_archive_path")
        if not isinstance(package_archive_path, str) or not package_archive_path.strip():
            raise ValueError("ai/toolchain.json must include package_archive_path")
        normalized_prefix = self._normalize_archive_name(package_archive_path)
        if normalized_prefix in file_entries:
            raise ValueError("Manifest declared ai_package but package_archive_path resolves to a file entry")
        has_package_files = any(
            self._is_child_of(entry_name, normalized_prefix)
            for entry_name in file_entries
        )
        if not has_package_files:
            raise ValueError("Manifest declared ai_package but no files exist under package_archive_path")

    def _normalize_imported_toolchain_metadata(self, metadata: dict) -> dict:
        normalized = dict(metadata)
        normalized["toolchain_id"] = self._ai_service._normalize_toolchain_text(
            metadata.get("toolchain_id"),
            "toolchain_id",
            allow_empty=False,
        )
        package_archive_path = normalized.get("package_archive_path")
        if isinstance(package_archive_path, str) and package_archive_path.strip():
            normalized["package_archive_path"] = self._normalize_archive_name(package_archive_path)
        return normalized

    def _serialize_ai_binding(self, binding: dict | None) -> dict:
        if not isinstance(binding, dict):
            return {
                "enabled": False,
                "toolchain_id": None,
                "timeout_ms": None,
                "params_json": "{}",
            }
        return {
            "enabled": bool(binding.get("enabled", False)),
            "toolchain_id": binding.get("toolchain_id"),
            "timeout_ms": binding.get("timeout_ms"),
            "params_json": binding.get("params_json", "{}"),
        }

    def _collect_golden_assets(
        self,
        config_payload: dict,
        *,
        include_assets: bool,
    ) -> list[tuple[int, str, str]]:
        if not include_assets:
            return []
        assets: list[tuple[int, str, str]] = []
        channels = config_payload.get("channels", [])
        if not isinstance(channels, list):
            return assets
        for channel in channels:
            if not isinstance(channel, dict):
                continue
            channel_index = channel.get("channel_index")
            if (
                not isinstance(channel_index, int)
                or isinstance(channel_index, bool)
                or channel_index < 0
            ):
                continue
            source_path = str(channel.get("golden_sample_path") or "").strip()
            if not source_path:
                continue
            normalized_path = os.path.abspath(source_path)
            if not os.path.isfile(normalized_path):
                raise ValueError("Selected golden sample file is missing")
            if not self._golden_samples.is_managed_path(normalized_path):
                raise ValueError("Selected golden sample must be under managed golden root")
            if self._golden_samples.is_managed_staged_path(normalized_path):
                raise ValueError("Selected golden sample must be committed before export")
            assets.append(
                (
                    channel_index,
                    f"{_GOLDEN_PREFIX}/channel_{channel_index}.wav",
                    normalized_path,
                )
            )
        return assets

    def _resolve_bound_toolchain(self, ai_binding: dict) -> object:
        if not ai_binding.get("enabled"):
            raise ValueError("A bound AI toolchain is required for export")
        toolchain_id = ai_binding.get("toolchain_id")
        if not isinstance(toolchain_id, str) or not toolchain_id.strip():
            raise ValueError("A bound AI toolchain is required for export")
        toolchain = self._ai_service.get_toolchain(toolchain_id)
        if toolchain is None:
            raise ValueError("Bound AI toolchain could not be found")
        return toolchain

    def _serialize_toolchain_metadata(
        self,
        *,
        toolchain: object,
        include_package: bool,
    ) -> dict:
        if include_package:
            package_dir = str(getattr(toolchain, "package_dir", "") or "").strip()
            resolved_package_dir = os.path.realpath(package_dir)
            if not package_dir or not os.path.isdir(resolved_package_dir):
                raise ValueError("toolchain package_dir is missing or invalid")
            source_package_dir = resolved_package_dir
        else:
            source_package_dir = str(getattr(toolchain, "package_dir", "") or "").strip()

        payload = {
            "toolchain_id": getattr(toolchain, "toolchain_id"),
            "display_name": getattr(toolchain, "display_name"),
            "version": getattr(toolchain, "version"),
            "entrypoint": getattr(toolchain, "entrypoint"),
            "protocol_version": getattr(toolchain, "protocol_version"),
            "execution_mode": getattr(toolchain, "execution_mode"),
            "default_timeout_ms": getattr(toolchain, "default_timeout_ms"),
            "enabled": bool(getattr(toolchain, "enabled", True)),
            "description": getattr(toolchain, "description"),
            "source_package_dir": source_package_dir,
        }
        if include_package:
            payload["package_archive_path"] = _AI_PACKAGE_PREFIX
        return payload

    def _iter_ai_package_files(self, package_dir: str) -> list[tuple[str, str, int]]:
        root = os.path.realpath(os.path.abspath(str(package_dir)))
        if not os.path.isdir(root):
            raise ValueError("toolchain package_dir is missing or invalid")

        package_files: list[tuple[str, str, int]] = []

        def walk_dir(current_dir: str) -> None:
            with os.scandir(current_dir) as entries:
                for entry in entries:
                    entry_path = os.path.abspath(entry.path)
                    if entry.is_symlink() or self._is_reparse_point(entry_path):
                        raise ValueError("AI package contains a symlink or reparse point")
                    real_path = os.path.realpath(entry_path)
                    if not self._is_within_root(real_path, root):
                        raise ValueError("AI package contains a path outside package_dir")
                    if entry.is_dir(follow_symlinks=False):
                        walk_dir(entry_path)
                        continue
                    if not entry.is_file(follow_symlinks=False):
                        raise ValueError("AI package contains a special file")

                    relative_path = os.path.relpath(real_path, root).replace("\\", "/")
                    archive_name = f"{_AI_PACKAGE_PREFIX}/{relative_path}"
                    package_files.append((real_path, archive_name, os.path.getsize(real_path)))

        walk_dir(root)
        return sorted(package_files, key=lambda item: item[1])

    def _track_archive_entry(
        self,
        archive_name: str,
        *,
        size: int,
        registry: dict[str, set[str]],
        file_count: int,
        total_bytes: int,
    ) -> tuple[str, int, int]:
        normalized_name = self._normalize_archive_name(archive_name)
        self._check_path_collision(normalized_name, registry)
        file_count += 1
        total_bytes += int(size)
        self._enforce_entry_limits(
            normalized_name,
            file_count=file_count,
            total_bytes=total_bytes,
        )
        return normalized_name, file_count, total_bytes

    def _classify_zip_entry(self, info: zipfile.ZipInfo) -> str:
        mode = (info.external_attr >> 16) & 0xFFFF
        kind = stat.S_IFMT(mode) if mode else 0
        if kind == stat.S_IFLNK:
            raise ValueError("ZIP entry is symlink-like")
        if kind and kind not in (stat.S_IFREG, stat.S_IFDIR):
            raise ValueError("ZIP entry is a special file")
        if info.is_dir() or info.filename.endswith("/") or bool(info.external_attr & 0x10):
            return "dir"
        return "file"

    def _read_json_entry(
        self,
        archive: zipfile.ZipFile,
        info: zipfile.ZipInfo,
        label: str,
    ) -> dict:
        try:
            payload = json.loads(archive.read(info))
        except json.JSONDecodeError as exc:
            raise ValueError(f"{label} is not valid JSON") from exc
        if not isinstance(payload, dict):
            raise ValueError(f"{label} must contain a JSON object")
        return payload

    def _new_archive_registry(self) -> dict[str, set[str]]:
        return {
            "normalized": set(),
            "casefold": set(),
        }

    def _check_path_collision(self, normalized_name: str, registry: dict[str, set[str]]) -> None:
        if normalized_name in registry["normalized"]:
            raise ValueError("Archive contains duplicate normalized entry names")
        registry["normalized"].add(normalized_name)

        if self._enforce_case_insensitive_collisions():
            key = normalized_name.casefold()
            if key in registry["casefold"]:
                raise ValueError("Archive contains case-insensitive path collisions")
            registry["casefold"].add(key)

    def _enforce_entry_limits(
        self,
        normalized_name: str,
        *,
        file_count: int,
        total_bytes: int,
    ) -> None:
        if self._enforce_windows_path_limit() and len(normalized_name) > MAX_NORMALIZED_PATH_LENGTH_WINDOWS:
            raise ValueError("Archive path length exceeds Windows safety limit")
        if file_count > MAX_FILE_COUNT:
            raise ValueError("Archive file count exceeds safety limit")
        if total_bytes > MAX_UNCOMPRESSED_BYTES:
            raise ValueError("Archive uncompressed payload exceeds safety limit")

    def _normalize_archive_name(self, name: str, *, allow_directory: bool = False) -> str:
        if not isinstance(name, str):
            raise ValueError("Archive entry name must be a string")
        normalized = name.replace("\\", "/").strip()
        if allow_directory:
            normalized = normalized.rstrip("/")
            if not normalized:
                return ""
        if normalized.startswith("/"):
            raise ValueError("Archive contains an absolute path")
        if re.match(r"^[A-Za-z]:($|/)", normalized):
            raise ValueError("Archive contains a drive-prefixed path")
        parts = normalized.split("/")
        if any(ntpath.splitdrive(part)[0] for part in parts):
            raise ValueError("Archive contains a drive-prefixed path")
        if any(part == "" for part in parts):
            raise ValueError("Archive contains an empty path segment")
        if any(part == ".." for part in parts):
            raise ValueError("Archive contains path traversal")
        if any(part == "." for part in parts):
            raise ValueError("Archive contains an invalid relative path")
        return "/".join(parts)

    def _is_under(self, name: str, prefix: str) -> bool:
        return name == prefix or name.startswith(f"{prefix}/")

    def _is_child_of(self, name: str, prefix: str) -> bool:
        return name.startswith(f"{prefix}/")

    def _is_within_root(self, candidate: str, root: str) -> bool:
        try:
            return os.path.commonpath([root, candidate]) == root
        except ValueError:
            return False

    @staticmethod
    def _is_reparse_point(path: str) -> bool:
        try:
            st = os.lstat(path)
        except OSError:
            return False
        attribute_mask = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
        return bool(getattr(st, "st_file_attributes", 0) & attribute_mask)

    @staticmethod
    def _json_bytes(payload: dict) -> bytes:
        return json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")

    @staticmethod
    def _extract_bound_toolchain_id(ai_binding: object) -> str | None:
        if not isinstance(ai_binding, dict):
            return None
        if not ai_binding.get("enabled"):
            return None
        toolchain_id = ai_binding.get("toolchain_id")
        if isinstance(toolchain_id, str) and toolchain_id.strip():
            return toolchain_id
        return None

    @staticmethod
    def _enforce_case_insensitive_collisions() -> bool:
        return os.name == "nt"

    @staticmethod
    def _enforce_windows_path_limit() -> bool:
        return os.name == "nt"
