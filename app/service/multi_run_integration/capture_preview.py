"""Parent-only best-effort forwarding, independent of capture control and PCM."""
import threading
import logging

from app.service.multi_run_coordinator_mvp.contracts import CaptureReservation


class PreviewCapturePort:
    """Observe capture termination without changing the delegated contracts."""

    def __init__(self, delegate, live_display):
        self._delegate = delegate
        self._live_display = live_display

    def __getattr__(self, name):
        return getattr(self._delegate, name)

    def _finish(self, reservation):
        if type(reservation) is not CaptureReservation:
            return
        try:
            # finish only fences registration and schedules the existing drain.
            self._live_display.finish(reservation.run_id)
        except Exception:
            logging.getLogger(__name__).exception("capture preview cleanup failed")

    def register_completion(self, reservation, sink):
        def completed(outcome):
            self._finish(reservation)
            sink(outcome)

        try:
            if not callable(sink):
                return self._delegate.register_completion(reservation, sink)
            return self._delegate.register_completion(reservation, completed)
        except Exception:
            self._finish(reservation)
            raise

    def start(self, reservation, trigger_monotonic_ns, target_frame_count):
        try:
            return self._delegate.start(reservation, trigger_monotonic_ns, target_frame_count)
        except Exception:
            self._finish(reservation)
            raise

    def cancel(self, reservation):
        self._finish(reservation)
        return self._delegate.cancel(reservation)

    def close_and_join(self, timeout_seconds):
        try:
            return self._delegate.close_and_join(timeout_seconds)
        finally:
            try:
                self._live_display.finish_all()
            except Exception:
                logging.getLogger(__name__).exception("capture preview shutdown cleanup failed")


class CapturePreviewPump:
    def __init__(self, client, accept_preview, diagnose):
        self._client = client
        self._accept_preview = accept_preview
        self._diagnose = diagnose
        self._stop = threading.Event()
        self.thread = threading.Thread(
            target=self._run, name="production-capture-preview", daemon=True)
        self.thread.start()

    def _run(self):
        preview = self._client.preview
        hardware_ids = tuple(handle.config.hardware_id for handle in preview.bootstrap.streams)
        try:
            while not self._stop.is_set() and self._client.process.is_alive():
                for hardware_id in hardware_ids:
                    for block in preview.read(hardware_id):
                        if self._stop.is_set():
                            return
                        self._accept_preview(block)
                self._stop.wait(.01)
        except Exception as exc:
            self._diagnose(f"capture preview forwarding failed: {exc}")

    def close(self, timeout_seconds):
        self._stop.set()
        if self.thread is not threading.current_thread():
            self.thread.join(max(0.0, timeout_seconds))
        return not self.thread.is_alive()
