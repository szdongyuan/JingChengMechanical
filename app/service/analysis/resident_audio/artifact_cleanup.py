"""Bounded delete retry, shutdown cleanup, and orphan-session cleanup."""

from __future__ import annotations

from dataclasses import dataclass

from .artifact_session import (
    SESSION_LOCK,
    SESSION_MARKER,
    SESSION_PREFIX,
    SessionOwnership,
    is_reparse_path,
    safe_session_path,
)
from .contracts import ArtifactState


@dataclass(frozen=True, slots=True)
class ArtifactCleanupReport:
    deleted_paths: tuple[str, ...] = ()
    failed_paths: tuple[str, ...] = ()
    retained_paths: tuple[str, ...] = ()
    attempted_paths: tuple[str, ...] = ()


class ArtifactCleanupMixin:
    def retry_delete_pending(self, *, limit: int) -> ArtifactCleanupReport:
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 0:
            raise ValueError("limit must be a non-negative integer")
        deleted: list[str] = []
        failed: list[str] = []
        attempted: list[str] = []
        with self._lock:
            pending = [
                entry
                for entry in self._entries.values()
                if entry.state is ArtifactState.DELETE_PENDING
                and entry.io_operation is None
            ][:limit]
        for entry in pending:
            report = self._retry_delete_pending_entry(entry, blocking=True)
            deleted.extend(report.deleted_paths)
            failed.extend(report.failed_paths)
            attempted.extend(report.attempted_paths)
        return ArtifactCleanupReport(
            deleted_paths=tuple(deleted),
            failed_paths=tuple(failed),
            attempted_paths=tuple(attempted),
        )

    def retry_delete_pending_artifact(
        self, artifact_id: str
    ) -> ArtifactCleanupReport:
        if type(artifact_id) is not str:
            raise TypeError("artifact_id must be a built-in string")
        if not artifact_id or len(artifact_id) > 256:
            raise ValueError("artifact_id must contain 1 to 256 characters")
        with self._lock:
            entry = self._registry.get(artifact_id)
            if (
                entry is None
                or entry.state is not ArtifactState.DELETE_PENDING
                or entry.io_operation is not None
            ):
                return ArtifactCleanupReport()
        return self._retry_delete_pending_entry(entry, blocking=False)

    def _retry_delete_pending_entry(
        self, entry, *, blocking: bool
    ) -> ArtifactCleanupReport:
        if not entry.io_lock.acquire(blocking=blocking):
            return ArtifactCleanupReport()
        try:
            with self._lock:
                if (
                    entry.state is not ArtifactState.DELETE_PENDING
                    or entry.io_operation is not None
                ):
                    return ArtifactCleanupReport()
                path = entry.delete_path or entry.path
                entry.io_operation = "DELETING"
            if entry.require_physical_unlink:
                removed = self._file_io.remove_existing(path)
            else:
                removed = self._file_io.remove(path)
            self._settle_delete_locked(entry, removed)
            path_text = str(path)
            return ArtifactCleanupReport(
                deleted_paths=(path_text,) if removed else (),
                failed_paths=() if removed else (path_text,),
                attempted_paths=(path_text,),
            )
        finally:
            entry.io_lock.release()

    def begin_closing(self) -> ArtifactCleanupReport:
        deleted: list[str] = []
        failed: list[str] = []
        retained: list[str] = []
        with self._lock:
            self._closing = True
            pending_count = sum(
                entry.state is ArtifactState.DELETE_PENDING
                for entry in self._entries.values()
            )
            partials = [
                (entry.artifact_id, str(entry.partial_path))
                for entry in self._entries.values()
                if entry.state is ArtifactState.PARTIAL
            ]
        pending_report = self.retry_delete_pending(limit=pending_count)
        deleted.extend(pending_report.deleted_paths)
        failed.extend(pending_report.failed_paths)
        for artifact_id, partial_path in partials:
            self.discard_partial(artifact_id)
            with self._lock:
                state = self._registry.state(artifact_id)
            if state is ArtifactState.DELETED:
                deleted.append(partial_path)
            elif state is ArtifactState.DELETE_PENDING:
                failed.append(partial_path)
        with self._lock:
            ready_to_delete = [
                entry
                for entry in self._entries.values()
                if entry.state is ArtifactState.READY_UNCLAIMED
                and not entry.publication_pending
            ]
        for entry in ready_to_delete:
            path = str(entry.ready_path)
            accepted = self._delete_entry(
                entry,
                allowed_states=(ArtifactState.READY_UNCLAIMED,),
                final_state=ArtifactState.DELETED,
                close_mapping=False,
            )
            if not accepted:
                continue
            with self._lock:
                state = entry.state
            if state is ArtifactState.DELETE_PENDING:
                failed.append(path)
            elif state is ArtifactState.DELETED:
                deleted.append(path)
        with self._lock:
            retained.extend(
                str(entry.ready_path)
                for entry in self._entries.values()
                if entry.state
                in (
                    ArtifactState.READY_UNCLAIMED,
                    ArtifactState.CLAIMED,
                    ArtifactState.TRANSFERRED,
                )
            )
            unresolved = [
                str(entry.delete_path or entry.path)
                for entry in self._entries.values()
                if entry.state is ArtifactState.DELETE_PENDING
            ]
        failed.extend(path for path in unresolved if path not in failed)
        return ArtifactCleanupReport(
            deleted_paths=tuple(deleted),
            failed_paths=tuple(failed),
            retained_paths=tuple(retained),
        )

    def cleanup_orphan_sessions(self) -> ArtifactCleanupReport:
        deleted: list[str] = []
        failed: list[str] = []
        retained: list[str] = []
        for session in self._root.iterdir():
            if session == self._session or not session.name.startswith(SESSION_PREFIX):
                continue
            resolved_session = safe_session_path(self._root, session)
            if resolved_session is None:
                retained.append(str(session))
                continue
            marker = resolved_session / SESSION_MARKER
            ownership = SessionOwnership.probe_inactive(resolved_session)
            if ownership is None:
                retained.append(str(session))
                continue
            session_failed = False
            lock_path = resolved_session / SESSION_LOCK
            try:
                for child in resolved_session.iterdir():
                    if child in (marker, lock_path):
                        continue
                    if safe_session_path(self._root, session) != resolved_session:
                        retained.append(str(session))
                        session_failed = True
                        break
                    try:
                        resolved_child = child.resolve(strict=True)
                    except OSError:
                        resolved_child = None
                    if (
                        is_reparse_path(child)
                        or resolved_child is None
                        or resolved_child.parent != resolved_session
                        or not resolved_child.is_file()
                    ):
                        failed.append(str(child))
                        session_failed = True
                        continue
                    if self._file_io.remove(child):
                        deleted.append(str(child))
                    else:
                        failed.append(str(child))
                        session_failed = True
            finally:
                ownership.release()
            if session_failed:
                continue
            if safe_session_path(self._root, session) != resolved_session:
                retained.append(str(session))
                continue
            try:
                lock_path.unlink()
                marker.unlink()
                resolved_session.rmdir()
            except OSError:
                failed.append(str(session))
        return ArtifactCleanupReport(
            deleted_paths=tuple(deleted),
            failed_paths=tuple(failed),
            retained_paths=tuple(retained),
        )

    def release_session_ownership(self) -> None:
        """Release liveness only after shutdown has ended all in-process use."""
        with self._lock:
            if not self._closing:
                raise RuntimeError("session ownership requires a closing artifact store")
        self._session_ownership.release()


__all__ = ["ArtifactCleanupReport"]
