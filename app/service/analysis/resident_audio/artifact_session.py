"""Cross-process liveness ownership for resident artifact staging sessions."""

from __future__ import annotations

import os
import stat
import uuid
from dataclasses import dataclass
from pathlib import Path

import portalocker


SESSION_MARKER = ".resident-audio-session"
SESSION_LOCK = ".resident-audio-session.lock"
SESSION_PREFIX = "session-"
_LOCK_FLAGS = portalocker.LockFlags.EXCLUSIVE | portalocker.LockFlags.NON_BLOCKING


def is_reparse_path(path: Path) -> bool:
    try:
        details = os.lstat(path)
    except OSError:
        return True
    attributes = getattr(details, "st_file_attributes", 0)
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    return stat.S_ISLNK(details.st_mode) or bool(attributes & reparse_flag)


def safe_session_path(root: Path, candidate: Path) -> Path | None:
    """Resolve a non-reparse immediate child session, or conservatively reject it."""
    if is_reparse_path(candidate):
        return None
    try:
        resolved_root = root.resolve(strict=True)
        resolved = candidate.resolve(strict=True)
    except OSError:
        return None
    if resolved.parent != resolved_root or not resolved.is_dir():
        return None
    return resolved


@dataclass(slots=True)
class SessionOwnership:
    directory: Path
    _lock: portalocker.Lock
    _owned: bool = True

    @classmethod
    def create(cls, root: Path) -> SessionOwnership:
        for _ in range(10):
            directory = root / f"{SESSION_PREFIX}{uuid.uuid4().hex}"
            try:
                directory.mkdir()
            except FileExistsError:
                continue
            marker = directory / SESSION_MARKER
            lock_path = directory / SESSION_LOCK
            try:
                marker.write_text(str(os.getpid()), encoding="ascii")
                lock = portalocker.Lock(
                    lock_path,
                    mode="a+",
                    timeout=0,
                    flags=_LOCK_FLAGS,
                )
                lock.acquire()
            except (OSError, portalocker.exceptions.LockException):
                for path in (lock_path, marker):
                    try:
                        path.unlink()
                    except OSError:
                        pass
                try:
                    directory.rmdir()
                except OSError:
                    pass
                raise
            return cls(directory=directory, _lock=lock)
        raise OSError("could not create unique artifact staging session")

    @classmethod
    def probe_inactive(cls, directory: Path) -> SessionOwnership | None:
        if safe_session_path(directory.parent, directory) is None:
            return None
        marker = directory / SESSION_MARKER
        lock_path = directory / SESSION_LOCK
        if (
            marker.is_symlink()
            or lock_path.is_symlink()
            or not marker.is_file()
            or not lock_path.is_file()
        ):
            return None
        try:
            lock = portalocker.Lock(
                lock_path,
                mode="a+",
                timeout=0,
                flags=_LOCK_FLAGS,
            )
            lock.acquire()
        except Exception:
            # Any unexpected probe failure is indeterminate, never proof of death.
            return None
        return cls(directory=directory, _lock=lock)

    def release(self) -> None:
        if not self._owned:
            return
        self._lock.release()
        self._owned = False


__all__ = [
    "SESSION_LOCK",
    "SESSION_MARKER",
    "SESSION_PREFIX",
    "SessionOwnership",
    "is_reparse_path",
    "safe_session_path",
]
