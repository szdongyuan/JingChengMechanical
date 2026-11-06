"""sounddevice expert C ingress with a bounded, backpressured Python pump.

The lifecycle control thread owns start/stop/close. A stopped generation is
never rearmed: restart opens a new stream and buffer after proven quiescence.
"""
from __future__ import annotations

import threading
from types import SimpleNamespace

from app.service.analysis.thread_priority import (
    THREAD_PRIORITY_ABOVE_NORMAL, set_current_thread_priority,
)
from app.service.analysis.resident_audio_mvp.contracts import BackendCallbackStatus
from app.service.native_capture_buffer import NativeCaptureBuffer, NativeCaptureError


class NativeCaptureStream:
    PUMP_JOIN_SECONDS = .2

    def __init__(self, sounddevice, spec, callback, on_fault, ready, set_latency):
        self._sounddevice = sounddevice
        self._spec = spec
        self._callback = callback
        self._on_fault = on_fault
        self._ready = ready
        self._set_latency = set_latency
        self._stream = None
        self._pump = None
        self._pump_stop = threading.Event()
        self._quiescent = True
        self._stop_unconfirmed = False
        self._started = False
        self._closed = False
        self._buffer = self._new_buffer()

    def _new_buffer(self):
        return NativeCaptureBuffer(sample_rate=self._spec.sample_rate_hz,
            channels=self._spec.input_channel_count,
            block_frames=self._spec.frames_per_buffer)

    @property
    def closed(self):
        # Native stream.closed alone is not sufficient evidence: the pump
        # must also have released every memory lease before base cleanup wins.
        return self._closed

    @property
    def active(self):
        return not self._quiescent or bool(self._pump and self._pump.is_alive())

    def producer_snapshot(self):
        try:
            return self._buffer.snapshot()
        except NativeCaptureError:
            return None

    def open_native(self):
        callback, userdata = self._buffer.cffi_callback(self._sounddevice._ffi)
        # Store the object before initialization so even a partial open failure
        # remains owned by the existing deadline/retry lifecycle controller.
        stream_type = self._sounddevice._StreamBase
        self._stream = stream_type.__new__(stream_type)
        stream_type.__init__(self._stream, kind='input',
            device=self._spec.device_index, samplerate=self._spec.sample_rate_hz,
            channels=self._spec.input_channel_count, dtype=self._spec.sample_format,
            blocksize=self._spec.frames_per_buffer, latency='high',
            callback=callback, userdata=userdata, wrap_callback=None)
        self._set_latency(float(self._stream.latency))

    def start(self):
        if self._closed:
            raise NativeCaptureError('Native stream is closed')
        if self._stop_unconfirmed:
            raise NativeCaptureError('Native producer stop unconfirmed; generation quarantined')
        if self._started:
            if self.active:
                raise NativeCaptureError('Previous native generation is still active')
            self._dispose_generation()
            self._buffer = self._new_buffer()
            self.open_native()
        elif self._stream is None:
            self.open_native()
        self._started = True
        self._buffer.arm()
        self._quiescent = False
        self._pump_stop.clear()
        self._pump = threading.Thread(target=self._run_pump,
            name=f'native-audio-pump-{self._spec.hardware_id}', daemon=True)
        self._pump.start()
        self._stream.start()

    def _run_pump(self):
        set_current_thread_priority(THREAD_PRIORITY_ABOVE_NORMAL)
        try:
            while not self._pump_stop.is_set():
                stats = self._buffer.snapshot()
                if stats.error:
                    raise NativeCaptureError(f'Native capture fault code={stats.error}')
                # Sole producer: once ready, only the consumer can change the
                # downstream queue, increasing space. Never remove a full block
                # from native protection before this readiness check.
                if not self._ready():
                    self._pump_stop.wait(.001)
                    continue
                block = self._buffer.peek()
                if block is None:
                    self._pump_stop.wait(.001)
                    continue
                try:
                    timing = SimpleNamespace(inputBufferAdcTime=block.adc_time,
                        currentTime=block.current_time, outputBufferDacTime=block.dac_time,
                        native_entry_ns=block.entry_ns,
                        clock_uncertainty_ns=self._buffer.clock_uncertainty_ns)
                    self._callback(block.samples, block.frames, timing,
                        self._sounddevice.CallbackFlags(block.status))
                finally:
                    self._buffer.release()
        except self._sounddevice.CallbackStop:
            self._buffer.request_stop()
        except Exception as exc:
            self._buffer.request_stop()
            self._on_fault(BackendCallbackStatus.BACKEND_FAULT,
                           f'native pump {type(exc).__name__}: {exc}')

    def stop(self):
        if self._closed:
            return
        if self._stop_unconfirmed:
            raise NativeCaptureError('Native producer stop unconfirmed; memory retained until process shutdown')
        self._buffer.request_stop()
        self._pump_stop.set()
        if not self._quiescent:
            try:
                self._stream.stop(ignore_errors=False)
            except Exception as exc:
                # ASIO can set stopped/inactive even when its driver stop fails
                # before callback completion. Later Pa_StopStream calls may then
                # return paStreamIsStopped without ever quiescing the producer.
                # Keep this failure sticky: the public API cannot prove cleanup.
                self._stop_unconfirmed = True
                if not (isinstance(exc, self._sounddevice.PortAudioError)
                        and len(exc.args) >= 2
                        and exc.args[1] == self._sounddevice._lib.paStreamIsStopped
                        and getattr(self._stream, 'stopped', False) is True
                        and getattr(self._stream, 'active', True) is False):
                    raise
                # A callback-requested abort with no earlier stop failure is
                # the sole benign error; still require stopped/inactive evidence.
                self._stop_unconfirmed = False
            self._buffer.producer_stopped()
            self._quiescent = True
        if self._pump is not None:
            self._pump.join(self.PUMP_JOIN_SECONDS)
            if self._pump.is_alive():
                raise TimeoutError('Native capture pump stop not confirmed; memory retained')

    def _dispose_generation(self):
        if self.active:
            raise NativeCaptureError('Cannot dispose a live native generation')
        if self._stream is not None:
            ffi = self._sounddevice._ffi
            pointer = getattr(self._stream, '_ptr', None)
            if pointer is not None and ffi.typeof(pointer) == ffi.typeof('PaStream **'):
                # Pa_OpenStream failed before sounddevice dereferenced its
                # output pointer. NULL proves that no stream was opened.
                pointer = pointer[0]
                self._stream._ptr = pointer
            if pointer is None or pointer != ffi.NULL:
                try:
                    self._stream.close(ignore_errors=False)
                except Exception:
                    # sounddevice clears _ptr even when Pa_CloseStream fails;
                    # keep ownership and permit an explicit lifecycle retry.
                    if pointer is not None:
                        self._stream._ptr = pointer
                    raise
            self._stream = None
        self._buffer.close()

    def close(self):
        if self._closed:
            return
        self.stop()
        self._dispose_generation()
        self._closed = True
