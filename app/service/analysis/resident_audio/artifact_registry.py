"""Indexed live artifacts plus bounded terminal and idempotency tombstones."""

from __future__ import annotations

from collections import OrderedDict

from .artifact_writer import ArtifactEntry
from .contracts import ArtifactState


DEFAULT_TOMBSTONE_LIMIT = 1_024


class ArtifactRegistry:
    """Keep the most recent N terminal states and N token outcomes."""

    def __init__(self, tombstone_limit: int = DEFAULT_TOMBSTONE_LIMIT) -> None:
        if (
            isinstance(tombstone_limit, bool)
            or not isinstance(tombstone_limit, int)
            or tombstone_limit < 1
        ):
            raise ValueError("tombstone_limit must be a positive integer")
        self.live: dict[str, ArtifactEntry] = {}
        self._claims: dict[str, ArtifactEntry] = {}
        self._tombstones: OrderedDict[str, ArtifactState] = OrderedDict()
        self._outcomes: OrderedDict[tuple[str, str], bool] = OrderedDict()
        self._limit = tombstone_limit

    @property
    def tombstone_count(self) -> int:
        return len(self._tombstones)

    @property
    def outcome_count(self) -> int:
        return len(self._outcomes)

    def add(self, entry: ArtifactEntry) -> None:
        self.live[entry.artifact_id] = entry

    def get(self, artifact_id: str) -> ArtifactEntry | None:
        return self.live.get(artifact_id)

    def state(self, artifact_id: str) -> ArtifactState | None:
        entry = self.live.get(artifact_id)
        if entry is not None:
            return entry.state
        state = self._tombstones.get(artifact_id)
        if state is not None:
            self._tombstones.move_to_end(artifact_id)
        return state

    def bind_claim(self, claim_token: str, entry: ArtifactEntry) -> None:
        self._claims[claim_token] = entry

    def claim(self, claim_token: str) -> ArtifactEntry | None:
        return self._claims.get(claim_token)

    def unbind_claim(self, claim_token: str | None) -> None:
        if claim_token is not None:
            self._claims.pop(claim_token, None)

    def remember_outcome(self, operation: str, token: str) -> None:
        key = (operation, token)
        self._outcomes[key] = True
        self._outcomes.move_to_end(key)
        while len(self._outcomes) > self._limit:
            self._outcomes.popitem(last=False)

    def has_outcome(self, operation: str, token: str) -> bool:
        key = (operation, token)
        if key not in self._outcomes:
            return False
        self._outcomes.move_to_end(key)
        return True

    def retire(self, entry: ArtifactEntry) -> None:
        if entry.state is ArtifactState.RELEASED and entry.claim_token is not None:
            self.remember_outcome("release", entry.claim_token)
        if entry.decline_recorded and entry.publication_token is not None:
            token = f"{entry.artifact_id}:{entry.publication_token}"
            self.remember_outcome("decline", token)
        if entry.abandoned_claim_token is not None:
            self.remember_outcome("abandon", entry.abandoned_claim_token)
        self.live.pop(entry.artifact_id, None)
        self.unbind_claim(entry.claim_token)
        self._tombstones[entry.artifact_id] = entry.state
        self._tombstones.move_to_end(entry.artifact_id)
        while len(self._tombstones) > self._limit:
            self._tombstones.popitem(last=False)


__all__ = ["ArtifactRegistry", "DEFAULT_TOMBSTONE_LIMIT"]
