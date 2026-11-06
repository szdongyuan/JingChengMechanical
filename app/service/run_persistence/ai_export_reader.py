"""AI export values from the same persisted attempt used by historical review."""

from __future__ import annotations

import json
import math

from app.service.run_persistence.analysis_result_artifact import (
    AnalysisResultArtifactError,
    AnalysisResultArtifactStore,
)
from app.service.run_persistence.attempt_repository import AttemptRepository
from app.service.run_persistence.database import RunPersistenceDatabase
from app.service.run_record_export.contracts import AiExportValue


def _export_value(score: object, label: object) -> AiExportValue:
    if type(score) not in (int, float):
        score = None
    else:
        try:
            score = float(score)
        except OverflowError:
            score = None
        else:
            if not math.isfinite(score):
                score = None
    if type(label) is not str or not label.strip():
        label = None
    return AiExportValue(score, label)


class RunAiExportReader:
    def __init__(
        self,
        runtime: RunPersistenceDatabase,
        *,
        result_artifact_store: AnalysisResultArtifactStore,
    ) -> None:
        self._runtime = runtime
        self._result_artifact_store = result_artifact_store
        self._attempts = AttemptRepository(
            runtime, result_artifact_store=result_artifact_store,
        )

    def get_current_effective_export_values(
        self, run_ids: tuple[str, ...],
    ) -> dict[str, AiExportValue]:
        values = {}
        for run_id in run_ids:
            values[run_id] = self._read_value(run_id)
        return values

    def _read_value(self, run_id: str) -> AiExportValue:
        unavailable = AiExportValue(None, None)
        try:
            attempt = self._attempts.latest_successful_for_run(run_id)
        except AnalysisResultArtifactError:
            return unavailable
        if attempt is None:
            return unavailable
        try:
            root = json.loads(attempt.result_payload_json)
        except (ValueError, TypeError, RecursionError):
            return unavailable
        if type(root) is not dict or type(root.get("result")) is not dict:
            return unavailable
        result = root["result"]
        review = result.get("review")
        if type(review) is dict and "ai_result" in review:
            # Presence is authoritative, even when fields are unavailable.
            snapshot = review["ai_result"]
            if type(snapshot) is not dict or snapshot.get("status") != "success":
                return unavailable
            return _export_value(snapshot.get("score"), snapshot.get("ai_label"))
        return _export_value(
            result.get("ai_score"), self._legacy_label(attempt.attempt_id),
        )

    def _legacy_label(self, attempt_id: str) -> object:
        path = (
            self._result_artifact_store.root.parent
            / "ai_runs" / attempt_id / "response.json"
        )
        try:
            response = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, ValueError, RecursionError):
            return None
        if type(response) is not dict:
            return None
        ai_result = response.get("ai_result")
        return ai_result.get("ai_label") if type(ai_result) is dict else None
