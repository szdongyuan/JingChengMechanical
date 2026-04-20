from __future__ import annotations

import json
import os
import re
import stat
import tempfile
import zipfile
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING

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
class _ArchiveInspection:
    manifest: dict
    contents: dict[str, bool]
    config_payload: dict
    ai_toolchain_metadata: dict | None
    file_entries: dict[str, zipfile.ZipInfo]


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
        config_root = inspection.config_payload.get("config")
        if not isinstance(config_root, dict):
            raise ValueError("config/config.json must contain a config object")

        config_name = str(config_root.get("config_name") or "").strip()
        if not config_name:
            raise ValueError("config/config.json is missing config_name")

        config_conflict = any(
            row.get("config_name") == config_name
            for row in self._config_service.list_configs()
        )
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
        has_package_files = any(
            self._is_child_of(entry_name, normalized_prefix)
            for entry_name in file_entries
        )
        if not has_package_files:
            raise ValueError("Manifest declared ai_package but no files exist under package_archive_path")

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
