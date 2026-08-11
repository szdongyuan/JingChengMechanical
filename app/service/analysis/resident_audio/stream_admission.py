"""Control-path command admission synchronized with ingest finalization."""

from __future__ import annotations

import queue
import threading


class CommandAdmission:
    """One gate that prevents enqueue-after-final-drain races."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._open = False

    def start_worker(self, worker: threading.Thread) -> None:
        with self._lock:
            worker.start()
            self._open = True

    def submit(self, command, commands, wake_event) -> bool:
        with self._lock:
            if not self._open:
                return False
            commands.put_nowait(command)
            wake_event.set()
            return True

    def close_and_drain(self, commands) -> None:
        with self._lock:
            self._open = False
            while True:
                try:
                    command = commands.get_nowait()
                except queue.Empty:
                    return
                command.cancel_if_queued()


__all__ = ["CommandAdmission"]
