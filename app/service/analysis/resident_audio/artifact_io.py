"""Physical artifact file operations; callers must not hold the store lock."""

from __future__ import annotations

import os
import shutil
import stat
from pathlib import Path
from typing import Callable

import numpy as np

from .artifact_session import is_reparse_path
from .artifact_writer import ArtifactEntry, FileReceipt


class ArtifactFileIO:
    def __init__(
        self,
        root: Path,
        *,
        free_space_bytes: Callable[[Path], int] | None,
        remove_file: Callable[[Path], None] | None,
    ) -> None:
        self._root = root
        self._free_space_bytes = free_space_bytes or (
            lambda path: shutil.disk_usage(path).free
        )
        self._remove_file = remove_file or (lambda path: path.unlink())

    def current_free_bytes(self) -> int:
        return int(self._free_space_bytes(self._root))

    @staticmethod
    def create_mapping(entry: ArtifactEntry) -> np.memmap:
        with entry.partial_path.open("xb") as partial:
            partial.truncate(entry.artifact_bytes)
        return np.memmap(
            entry.partial_path,
            dtype=np.float32,
            mode="r+",
            shape=(entry.target_frames, entry.channels),
        )

    @staticmethod
    def close_mapping(mapping: np.memmap, *, flush: bool) -> Exception | None:
        failure: Exception | None = None
        if flush:
            try:
                mapping.flush()
            except (OSError, ValueError) as error:
                failure = error
        try:
            mapping._mmap.close()
        except (OSError, ValueError) as error:
            if failure is None:
                failure = error
        return failure

    @staticmethod
    def freeze(entry: ArtifactEntry) -> FileReceipt:
        details = os.stat(entry.partial_path, follow_symlinks=False)
        if (
            not stat.S_ISREG(details.st_mode)
            or is_reparse_path(entry.partial_path)
            or details.st_size != entry.artifact_bytes
        ):
            raise OSError("partial artifact is not the expected regular file")
        receipt = FileReceipt(
            device=details.st_dev,
            inode=details.st_ino,
            size=details.st_size,
        )
        os.replace(entry.partial_path, entry.ready_path)
        return receipt

    def credible_moved_destination(self, entry: ArtifactEntry, path: Path) -> bool:
        receipt = entry.transfer_receipt
        if receipt is None or is_reparse_path(path):
            return False
        try:
            details = os.lstat(path)
            resolved = path.resolve(strict=True)
            staging_root = self._root.resolve(strict=True)
        except OSError:
            return False
        if not stat.S_ISREG(details.st_mode) or details.st_size != entry.artifact_bytes:
            return False
        try:
            resolved.relative_to(staging_root)
        except ValueError:
            pass
        else:
            return False
        if os.path.lexists(entry.ready_path):
            return False
        return (
            receipt.inode != 0
            and details.st_dev == receipt.device
            and details.st_ino == receipt.inode
            and details.st_size == receipt.size
        )

    def remove(self, path: Path) -> bool:
        try:
            self._remove_file(path)
        except FileNotFoundError:
            return True
        except OSError:
            return False
        return True

    def remove_existing(self, path: Path) -> bool:
        try:
            self._remove_file(path)
        except OSError:
            return False
        return True


__all__ = ["ArtifactFileIO"]
