"""Coordinator-facing audio saver backed by the production run-audio authority."""

from __future__ import annotations

from app.service.multi_run_coordinator_mvp.contracts import (
    AudioSaveOutcome,
    CapturedAudioSaveRequest,
    ImportedAudioSaveRequest,
    SaveCancelToken,
)
from app.service.run_persistence.production_run_audio_adapter import (
    ProductionRunAudioAdapter,
)


class ProductionAudioSaver:
    """Narrowly expose the production persistence boundary as both saver ports."""

    __slots__ = ("_authority",)

    def __init__(self, authority: ProductionRunAudioAdapter) -> None:
        if type(authority) is not ProductionRunAudioAdapter:
            raise TypeError("authority must be an exact ProductionRunAudioAdapter")
        self._authority = authority

    def save_captured(
        self,
        request: CapturedAudioSaveRequest,
        cancel_token: SaveCancelToken,
    ) -> AudioSaveOutcome:
        return self._authority.save_captured(request, cancel_token)

    def save_imported(
        self,
        request: ImportedAudioSaveRequest,
        cancel_token: SaveCancelToken,
    ) -> AudioSaveOutcome:
        return self._authority.save_imported(request, cancel_token)
