"""Pure builders and package-closure validation for v2 export manifests."""

from __future__ import annotations

from datetime import datetime, timezone
import json
import math
import re
from typing import Any
from uuid import UUID

from app.service.run_record_export.contracts import (
    PackagedRunRecord,
    RunDataExportOptions,
    RunDataExportSkippedRecord,
)
from app.service.run_record_export.naming import manifest_result


_ROOT_SCHEMA = "jingcheng-run-export-package/v2"
_RECORD_SCHEMA = "jingcheng-run-export-record/v2"
_ROOT_MANIFEST_PATH = "export_manifest.json"
_COMPONENT_ORDER = {"primary": 0, "audio": 1, "current": 2}
_UTC_TIMESTAMP = re.compile(r"\A\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{6}Z\Z")


def build_record_manifest(record: PackagedRunRecord) -> bytes:
    """Build one exact ``jingcheng-run-export-record/v2`` document."""

    if type(record) is not PackagedRunRecord:
        raise TypeError("record must be a PackagedRunRecord")
    accepted_at = _canonical_utc_timestamp(record.record.accepted_at)
    document = {
        "schema": _RECORD_SCHEMA,
        "sequence": record.sequence,
        "run": {
            "run_id": record.record.run_id,
            "accepted_at": accepted_at,
            "sn": record.record.sn,
            "model": record.record.model,
        },
        "outcome": {
            "result": manifest_result(
                state=record.record.state,
                judgment=record.record.judgment,
            ),
            "judgment": record.record.judgment,
            "judgment_source": record.record.judgment_source,
            "fault_type": record.record.fault_type,
        },
        "ai": {"score": record.ai.score, "label": record.ai.label},
        "audio": {
            "selected": record.audio.selected,
            "status": record.audio.status,
            "reason": record.audio.reason,
            "sample_rate_hz": record.audio.sample_rate_hz,
            "frame_count": record.audio.frame_count,
            "channels": [
                {
                    "position": channel.position,
                    "source_channel": channel.source_channel,
                    "path": channel.path,
                }
                for channel in record.audio.channels
            ],
        },
        "current": {
            "selected": record.current.selected,
            "status": record.current.status,
            "reason": record.current.reason,
            "path": record.current.path,
        },
    }
    return _serialize(document)


def build_export_manifest(
    *,
    options: RunDataExportOptions,
    exported_at: datetime | str,
    selected_run_ids: tuple[str, ...],
    records: tuple[PackagedRunRecord, ...],
    skips: tuple[RunDataExportSkippedRecord, ...],
    excel_path: str | None,
    database_path: str | None,
) -> bytes:
    """Build the exact package manifest and derive its counts."""

    if type(options) is not RunDataExportOptions:
        raise TypeError("options must be RunDataExportOptions")
    selected = _validate_selected_run_ids(selected_run_ids)
    if type(records) is not tuple or any(
        type(record) is not PackagedRunRecord for record in records
    ):
        raise TypeError("records must be a tuple of PackagedRunRecord")
    if type(skips) is not tuple or any(
        type(skip) is not RunDataExportSkippedRecord for skip in skips
    ):
        raise TypeError("skips must be a tuple of RunDataExportSkippedRecord")
    excel = _artifact_path("excel", options.include_excel, excel_path)
    database = _artifact_path(
        "database", options.include_database, database_path
    )

    ordered_records = tuple(sorted(records, key=lambda item: item.sequence))
    sequences = tuple(record.sequence for record in ordered_records)
    if sequences != tuple(range(1, len(ordered_records) + 1)):
        raise ValueError("record sequences must be unique and contiguous")
    selection_indexes: set[int] = set()
    for record in ordered_records:
        index = record.record.selection_index
        if index >= len(selected) or selected[index] != record.record.run_id:
            raise ValueError("record does not agree with selected run IDs")
        if index in selection_indexes:
            raise ValueError("record selection indexes must be unique")
        selection_indexes.add(index)

    ordered_skips = tuple(
        sorted(
            skips,
            key=lambda skip: (
                skip.selection_index,
                _COMPONENT_ORDER[skip.component],
            ),
        )
    )
    skip_keys: set[tuple[int, str]] = set()
    for skip in ordered_skips:
        if (
            skip.selection_index >= len(selected)
            or selected[skip.selection_index] != skip.run_id
        ):
            raise ValueError("skip does not agree with selected run IDs")
        key = (skip.selection_index, skip.component)
        if key in skip_keys:
            raise ValueError("skip selection/component pairs must be unique")
        skip_keys.add(key)

    primary_indexes = {
        skip.selection_index
        for skip in ordered_skips
        if skip.component == "primary"
    }
    if selection_indexes & primary_indexes:
        raise ValueError("a run cannot be exported and primary-skipped")
    if selection_indexes | primary_indexes != set(range(len(selected))):
        raise ValueError("each selection must be exported or primary-skipped")
    if any(
        skip.selection_index in primary_indexes and skip.component != "primary"
        for skip in ordered_skips
    ):
        raise ValueError("primary-skipped runs cannot have attachment skips")

    expected_attachment_skips: dict[tuple[int, str], str] = {}
    for packaged in ordered_records:
        for component, selected_option, attachment in (
            ("audio", options.include_audio, packaged.audio),
            ("current", options.include_current_csv, packaged.current),
        ):
            if selected_option and attachment.status == "not_selected":
                raise ValueError(f"selected {component} cannot be not_selected")
            if not selected_option and attachment.status != "not_selected":
                raise ValueError(f"unselected {component} must be not_selected")
            if attachment.status == "skipped":
                expected_attachment_skips[
                    (packaged.record.selection_index, component)
                ] = attachment.reason
    actual_attachment_skips = {
        (skip.selection_index, skip.component): skip.reason
        for skip in ordered_skips
        if skip.component != "primary"
    }
    if actual_attachment_skips != expected_attachment_skips:
        raise ValueError("attachment skips do not match packaged statuses")

    audio_emitted = tuple(
        record for record in ordered_records if record.audio.status == "exported"
    )
    audio_skipped = tuple(
        record for record in ordered_records if record.audio.status == "skipped"
    )
    current_emitted = tuple(
        record
        for record in ordered_records
        if record.current.status == "exported"
    )
    current_skipped = tuple(
        record
        for record in ordered_records
        if record.current.status == "skipped"
    )

    document = {
        "schema": _ROOT_SCHEMA,
        "exported_at": _canonical_utc_timestamp(exported_at),
        "options": {
            "excel": options.include_excel,
            "audio": options.include_audio,
            "database": options.include_database,
            "current_csv": options.include_current_csv,
        },
        "counts": {
            "selected_runs": len(selected),
            "exported_runs": len(ordered_records),
            "skipped_runs": len(primary_indexes),
            "audio_runs_emitted": len(audio_emitted),
            "audio_runs_skipped": len(audio_skipped),
            "audio_files_emitted": sum(
                len(record.audio.channels) for record in audio_emitted
            ),
            "current_runs_emitted": len(current_emitted),
            "current_runs_skipped": len(current_skipped),
        },
        "artifacts": {"excel": excel, "database": database},
        "records": [
            {
                "sequence": record.sequence,
                "run_id": record.record.run_id,
                "directory": record.directory,
                "manifest": f"{record.directory}/manifest.json",
            }
            for record in ordered_records
        ],
        "skips": [
            {
                "run_id": skip.run_id,
                "component": skip.component,
                "reason": skip.reason,
            }
            for skip in ordered_skips
        ],
    }
    return _serialize(document)


def validate_declared_members(
    *,
    root_manifest: bytes,
    record_manifests: dict[str, bytes],
    members: set[str],
) -> None:
    """Validate that v2 manifests and actual package members close exactly."""

    root = _load_document(root_manifest, "root manifest")
    if type(record_manifests) is not dict or any(
        type(path) is not str or type(payload) is not bytes
        for path, payload in record_manifests.items()
    ):
        raise TypeError("record_manifests must map paths to bytes")
    if type(members) is not set or any(
        type(member) is not str for member in members
    ):
        raise TypeError("members must be a set of strings")
    for member in members:
        _member_path(member)
    for path in record_manifests:
        _member_path(path)

    root_records, options, counts, artifacts, skips = _validate_root(root)
    indexed_paths = [entry["manifest"] for entry in root_records]
    if len(set(indexed_paths)) != len(indexed_paths):
        raise ValueError("duplicate record manifest path")
    if set(indexed_paths) != set(record_manifests):
        raise ValueError("record manifest mapping does not match root index")

    documents: list[tuple[dict[str, Any], dict[str, Any]]] = []
    declared = {_ROOT_MANIFEST_PATH}
    for artifact in artifacts.values():
        if artifact is not None:
            _add_declaration(declared, artifact)
    for entry in root_records:
        manifest_path = entry["manifest"]
        _add_declaration(declared, manifest_path)
        record_document = _load_document(
            record_manifests[manifest_path], manifest_path
        )
        _validate_record(record_document, entry)
        documents.append((entry, record_document))
        for channel in record_document["audio"]["channels"]:
            path = channel["path"]
            _add_declaration(declared, path)
        current_path = record_document["current"]["path"]
        if current_path is not None:
            _add_declaration(declared, current_path)

    missing = declared - members
    if missing:
        raise ValueError(f"declared member is missing: {sorted(missing)[0]}")
    undeclared = members - declared
    if undeclared:
        raise ValueError(f"package contains undeclared member: {sorted(undeclared)[0]}")

    _validate_cross_manifest_consistency(
        documents=documents,
        options=options,
        counts=counts,
        skips=skips,
    )


def _validate_root(document: dict[str, Any]):
    _exact_keys(
        document,
        {
            "schema",
            "exported_at",
            "options",
            "counts",
            "artifacts",
            "records",
            "skips",
        },
        "root manifest",
    )
    if document["schema"] != _ROOT_SCHEMA:
        raise ValueError("root manifest schema is invalid")
    _canonical_utc_timestamp(document["exported_at"])
    options = _object(document["options"], "options")
    _exact_keys(
        options,
        {"excel", "audio", "database", "current_csv"},
        "options",
    )
    if any(type(value) is not bool for value in options.values()):
        raise ValueError("manifest options must be booleans")
    if not any(options.values()):
        raise ValueError("manifest options cannot all be false")
    counts = _object(document["counts"], "counts")
    count_keys = {
        "selected_runs",
        "exported_runs",
        "skipped_runs",
        "audio_runs_emitted",
        "audio_runs_skipped",
        "audio_files_emitted",
        "current_runs_emitted",
        "current_runs_skipped",
    }
    _exact_keys(counts, count_keys, "counts")
    if any(type(value) is not int or value < 0 for value in counts.values()):
        raise ValueError("manifest counts must be nonnegative integers")
    if counts["selected_runs"] == 0:
        raise ValueError("selected run count must be positive")
    artifacts = _object(document["artifacts"], "artifacts")
    _exact_keys(artifacts, {"excel", "database"}, "artifacts")
    for name in ("excel", "database"):
        path = artifacts[name]
        if path is not None:
            _member_path(path)
        if (path is not None) != options[name]:
            raise ValueError(f"{name} artifact contradicts options")
    records = _array(document["records"], "records")
    expected_sequence = 1
    run_ids: set[str] = set()
    directories: set[str] = set()
    for entry_value in records:
        entry = _object(entry_value, "record index entry")
        _exact_keys(
            entry,
            {"sequence", "run_id", "directory", "manifest"},
            "record index entry",
        )
        if (
            type(entry["sequence"]) is not int
            or entry["sequence"] != expected_sequence
        ):
            raise ValueError("record index must be ordered by contiguous sequence")
        expected_sequence += 1
        _uuid4(entry["run_id"])
        directory = _member_path(entry["directory"])
        manifest = _member_path(entry["manifest"])
        if manifest != f"{directory}/manifest.json":
            raise ValueError("record manifest path does not match directory")
        if entry["run_id"] in run_ids or directory in directories:
            raise ValueError("duplicate record index identity")
        run_ids.add(entry["run_id"])
        directories.add(directory)
    skips = _array(document["skips"], "skips")
    seen_skips: set[tuple[str, str]] = set()
    last_component_by_run: dict[str, int] = {}
    for skip_value in skips:
        skip = _object(skip_value, "skip")
        _exact_keys(skip, {"run_id", "component", "reason"}, "skip")
        run_id = _uuid4(skip["run_id"])
        component = skip["component"]
        if component not in _COMPONENT_ORDER:
            raise ValueError("skip component is invalid")
        _nonempty_string(skip["reason"], "skip reason")
        key = (run_id, component)
        if key in seen_skips:
            raise ValueError("duplicate skip entry")
        seen_skips.add(key)
        order = _COMPONENT_ORDER[component]
        if order < last_component_by_run.get(run_id, -1):
            raise ValueError("skip components are out of order")
        last_component_by_run[run_id] = order
    return records, options, counts, artifacts, skips


def _validate_record(document: dict[str, Any], entry: dict[str, Any]) -> None:
    _exact_keys(
        document,
        {"schema", "sequence", "run", "outcome", "ai", "audio", "current"},
        "record manifest",
    )
    if document["schema"] != _RECORD_SCHEMA:
        raise ValueError("record manifest schema is invalid")
    if (
        type(document["sequence"]) is not int
        or document["sequence"] != entry["sequence"]
    ):
        raise ValueError("record manifest sequence does not match root")
    run = _object(document["run"], "run")
    _exact_keys(run, {"run_id", "accepted_at", "sn", "model"}, "run")
    if _uuid4(run["run_id"]) != entry["run_id"]:
        raise ValueError("record run ID does not match root")
    _canonical_utc_timestamp(run["accepted_at"])
    if type(run["sn"]) is not str or type(run["model"]) is not str:
        raise ValueError("run SN and model must be strings")

    outcome = _object(document["outcome"], "outcome")
    _exact_keys(
        outcome,
        {"result", "judgment", "judgment_source", "fault_type"},
        "outcome",
    )
    if outcome["result"] not in {"OK", "NG", "NO_RESULT", "ERROR"}:
        raise ValueError("outcome result is invalid")
    if outcome["judgment"] not in {None, "PASS", "FAIL"}:
        raise ValueError("outcome judgment is invalid")
    if outcome["judgment_source"] not in {"AUTOMATIC", "MANUAL", "NONE"}:
        raise ValueError("outcome judgment source is invalid")
    if (
        outcome["fault_type"] is not None
        and type(outcome["fault_type"]) is not str
    ):
        raise ValueError("outcome fault type is invalid")

    ai = _object(document["ai"], "ai")
    _exact_keys(ai, {"score", "label"}, "ai")
    score = ai["score"]
    if score is not None and (
        isinstance(score, bool)
        or not isinstance(score, (int, float))
        or not math.isfinite(float(score))
    ):
        raise ValueError("AI score must be finite or null")
    if ai["label"] is not None:
        _nonempty_string(ai["label"], "AI label")

    audio = _object(document["audio"], "audio")
    _exact_keys(
        audio,
        {
            "selected",
            "status",
            "reason",
            "sample_rate_hz",
            "frame_count",
            "channels",
        },
        "audio",
    )
    channels = _array(audio["channels"], "audio channels")
    _validate_attachment_status(audio, has_path=bool(channels), component="audio")
    seen_paths: set[str] = set()
    seen_sources: set[int] = set()
    for position, channel_value in enumerate(channels, start=1):
        channel = _object(channel_value, "audio channel")
        _exact_keys(channel, {"position", "source_channel", "path"}, "audio channel")
        if type(channel["position"]) is not int or channel["position"] != position:
            raise ValueError("audio channels must be ordered by position")
        source = channel["source_channel"]
        if type(source) is not int or source < 0:
            raise ValueError("audio source channel is invalid")
        path = _member_path(channel["path"])
        if path in seen_paths or source in seen_sources:
            raise ValueError("duplicate audio channel member path or source")
        expected = f"{entry['directory']}/audio/CH{source + 1}.wav"
        if path != expected:
            raise ValueError("audio channel path does not match source channel")
        seen_paths.add(path)
        seen_sources.add(source)
    if audio["status"] == "exported":
        for field in ("sample_rate_hz", "frame_count"):
            if type(audio[field]) is not int or audio[field] <= 0:
                raise ValueError(f"audio {field} must be positive")
    elif audio["sample_rate_hz"] is not None or audio["frame_count"] is not None:
        raise ValueError("audio sampling facts contradict status")

    current = _object(document["current"], "current")
    _exact_keys(current, {"selected", "status", "reason", "path"}, "current")
    path = current["path"]
    if path is not None:
        path = _member_path(path)
        if path != f"{entry['directory']}/current.csv":
            raise ValueError("current path does not match record directory")
    _validate_attachment_status(current, has_path=path is not None, component="current")


def _validate_attachment_status(
    value: dict[str, Any], *, has_path: bool, component: str
) -> None:
    if type(value["selected"]) is not bool:
        raise ValueError(f"{component} selected must be a boolean")
    status = value["status"]
    if status not in {"not_selected", "exported", "skipped"}:
        raise ValueError(f"{component} status is invalid")
    reason = value["reason"]
    if status == "not_selected":
        valid = not value["selected"] and reason is None and not has_path
    elif status == "exported":
        valid = value["selected"] and reason is None and has_path
    else:
        valid = (
            value["selected"]
            and isinstance(reason, str)
            and bool(reason.strip())
            and not has_path
        )
    if not valid:
        raise ValueError(f"{component} status/path fields are inconsistent")


def _validate_cross_manifest_consistency(*, documents, options, counts, skips) -> None:
    primary_skips = [skip for skip in skips if skip["component"] == "primary"]
    attachment_skips = {
        (skip["run_id"], skip["component"]): skip["reason"]
        for skip in skips
        if skip["component"] != "primary"
    }
    record_ids = {entry["run_id"] for entry, _ in documents}
    if any(skip["run_id"] in record_ids for skip in primary_skips):
        raise ValueError("primary skip also appears in record index")
    expected_skips: dict[tuple[str, str], str] = {}
    for entry, document in documents:
        for component, option_key in (
            ("audio", "audio"),
            ("current", "current_csv"),
        ):
            attachment = document[component]
            if options[option_key] and attachment["status"] == "not_selected":
                raise ValueError(f"selected {component} is not_selected")
            if not options[option_key] and attachment["status"] != "not_selected":
                raise ValueError(f"unselected {component} has emitted state")
            if attachment["status"] == "skipped":
                expected_skips[(entry["run_id"], component)] = attachment["reason"]
    if attachment_skips != expected_skips:
        raise ValueError("attachment skip entries do not match record manifests")
    expected_counts = {
        "selected_runs": len(documents) + len(primary_skips),
        "exported_runs": len(documents),
        "skipped_runs": len(primary_skips),
        "audio_runs_emitted": sum(
            doc["audio"]["status"] == "exported" for _, doc in documents
        ),
        "audio_runs_skipped": sum(
            doc["audio"]["status"] == "skipped" for _, doc in documents
        ),
        "audio_files_emitted": sum(
            len(doc["audio"]["channels"]) for _, doc in documents
        ),
        "current_runs_emitted": sum(
            doc["current"]["status"] == "exported" for _, doc in documents
        ),
        "current_runs_skipped": sum(
            doc["current"]["status"] == "skipped" for _, doc in documents
        ),
    }
    if counts != expected_counts:
        raise ValueError("manifest count mismatch")


def _serialize(document: dict[str, Any]) -> bytes:
    return json.dumps(
        document,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
    ).encode("utf-8")


def _load_document(payload: bytes, name: str) -> dict[str, Any]:
    if type(payload) is not bytes:
        raise TypeError(f"{name} must be bytes")
    try:
        value = json.loads(
            (
                payload.decode("utf-8-sig")
                if payload.startswith(b"\xef\xbb\xbf")
                else payload.decode("utf-8")
            ),
            parse_constant=lambda constant: (_raise_invalid_number(constant)),
        )
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise ValueError(f"{name} is not valid UTF-8 JSON") from None
    if payload.startswith(b"\xef\xbb\xbf"):
        raise ValueError(f"{name} must not contain a UTF-8 BOM")
    return _object(value, name)


def _raise_invalid_number(constant: str):
    raise ValueError(f"non-finite JSON number {constant}")


def _canonical_utc_timestamp(value: datetime | str) -> str:
    if isinstance(value, datetime):
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("timestamp must be timezone-aware")
        value = value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    if type(value) is not str or not _UTC_TIMESTAMP.fullmatch(value):
        raise ValueError("timestamp must be canonical UTC text")
    try:
        datetime.strptime(value, "%Y-%m-%dT%H:%M:%S.%fZ")
    except ValueError:
        raise ValueError("timestamp must be canonical UTC text") from None
    return value


def _artifact_path(name: str, selected: bool, path: str | None) -> str | None:
    if selected:
        if path is None:
            raise ValueError(f"selected {name} requires a path")
        return _member_path(path)
    if path is not None:
        raise ValueError(f"unselected {name} requires a null path")
    return None


def _validate_selected_run_ids(value: object) -> tuple[str, ...]:
    if type(value) is not tuple or not value:
        raise ValueError("selected run IDs must be a nonempty tuple")
    result = tuple(_uuid4(run_id) for run_id in value)
    if len(set(result)) != len(result):
        raise ValueError("selected run IDs must be unique")
    return result


def _uuid4(value: object) -> str:
    if type(value) is not str:
        raise ValueError("run ID must be canonical UUIDv4 text")
    try:
        parsed = UUID(value)
    except ValueError:
        raise ValueError("run ID must be canonical UUIDv4 text") from None
    if parsed.version != 4 or str(parsed) != value:
        raise ValueError("run ID must be canonical UUIDv4 text")
    return value


def _member_path(value: object) -> str:
    if (
        type(value) is not str
        or not value
        or "\\" in value
        or value.startswith("/")
    ):
        raise ValueError("path must be package-root-relative POSIX text")
    if any(part in {"", ".", ".."} for part in value.split("/")):
        raise ValueError("path must not contain empty or traversal segments")
    return value


def _add_declaration(declared: set[str], path: str) -> None:
    if path in declared:
        raise ValueError("duplicate declared member path")
    declared.add(path)


def _object(value: object, name: str) -> dict[str, Any]:
    if type(value) is not dict:
        raise ValueError(f"{name} must be a JSON object")
    return value


def _array(value: object, name: str) -> list[Any]:
    if type(value) is not list:
        raise ValueError(f"{name} must be a JSON array")
    return value


def _exact_keys(value: dict[str, Any], expected: set[str], name: str) -> None:
    if set(value) != expected:
        raise ValueError(f"{name} has incorrect fields")


def _nonempty_string(value: object, name: str) -> str:
    if type(value) is not str or not value.strip():
        raise ValueError(f"{name} must be a nonempty string")
    return value
