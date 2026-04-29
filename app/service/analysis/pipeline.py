"""Per-channel real-time analysis pipeline.

Receives raw audio chunks from AudioRecorder.  For each chunk, computes:
- STFT (when enough samples have accumulated)
- SPL (every chunk)
- 1/3-octave bands (every chunk)
- FFT spectrum (once the rolling FFT buffer is primed)
"""
from __future__ import annotations

import numpy as np

from app.service.analysis.frames import FftFrame, LoudnessFrame, OctaveFrame, SplFrame, StftFrame
from app.service.analysis import dsp


_LIVE_LOUDNESS_THROTTLE_SEC = 0.25
_LIVE_LOUDNESS_WINDOW_SEC = 0.5


class AnalysisPipeline:
    """Streaming analysis for one channel."""

    def __init__(
        self,
        channel_index: int,
        channel_config: dict,
        sample_rate: float,
        mic_sensitivity: float = 0.0,
    ):
        self.channel_index = channel_index
        self.sample_rate = sample_rate
        self._cfg = channel_config
        self._mic_sensitivity = mic_sensitivity

        # STFT config
        self._nfft: int = channel_config["stft_nfft"]
        self._fft_nfft: int = channel_config["fft_nfft"]
        self._overlap: float = channel_config["stft_overlap"]

        # SPL config
        self._spl_weighting: str = channel_config["spl_weighting"]
        self._spl_tc: float = channel_config["spl_time_constant"]

        # Octave config
        self._oct_weighting: str = channel_config["oct_weighting"]

        # Internal STFT overlap buffer
        self._stft_buffer = np.array([], dtype=np.float64)
        self._latest_stft_frame_sample_index: int | None = None
        self._fft_buffer = np.array([], dtype=np.float64)
        self._loudness_buffer = np.array([], dtype=np.float64)
        self._last_live_loudness_emit_time = 0.0

    def process_chunk(
        self,
        chunk_mono: np.ndarray,
        elapsed_time: float,
        compute_loudness: bool = True,
    ) -> dict:
        """Process one audio chunk and return analysis results.

        Parameters
        ----------
        chunk_mono : 1-D float array (single channel)
        elapsed_time : seconds since recording start

        Returns
        -------
        dict with keys: "stft_frames", "stft", "spl", "octave", "fft"
            Values are frame dataclasses or None if not enough data.
        """
        sig = np.asarray(chunk_mono, dtype=np.float64)

        # Apply mic calibration
        if self._mic_sensitivity != 0.0:
            sig = dsp.apply_mic_calibration(sig, self._mic_sensitivity)

        if sig.size > 0:
            self._loudness_buffer = np.concatenate([self._loudness_buffer, sig])
            loudness_window_samples = int(round(self.sample_rate * _LIVE_LOUDNESS_WINDOW_SEC))
            if loudness_window_samples > 0 and self._loudness_buffer.size > loudness_window_samples:
                self._loudness_buffer = self._loudness_buffer[-loudness_window_samples:]
        loudness_frame = (
            self._maybe_compute_live_loudness(elapsed_time)
            if compute_loudness
            else None
        )

        # SPL — always computed (even for short chunks)
        spl_db = dsp.compute_spl(sig, self.sample_rate,
                                 self._spl_weighting, self._spl_tc)
        spl_frame = SplFrame(time=elapsed_time, spl_db=spl_db,
                             weighting=self._spl_weighting)

        # FFT spectrum — use a dedicated rolling window sized by fft_nfft
        fft_frame = None
        if sig.size > 0:
            self._fft_buffer = np.concatenate([self._fft_buffer, sig])
            if self._fft_buffer.size > self._fft_nfft:
                self._fft_buffer = self._fft_buffer[-self._fft_nfft:]
            if self._fft_buffer.size >= self._fft_nfft:
                freqs, mag_db = dsp.compute_fft_spectrum(
                    self._fft_buffer,
                    self.sample_rate,
                    self._fft_nfft,
                )
                fft_frame = FftFrame(
                    time=elapsed_time,
                    frequencies=freqs,
                    magnitudes=mag_db,
                )

        # Octave bands — need enough samples for low-freq resolution
        if sig.size >= 256:
            centers, levels = dsp.compute_octave_bands(sig, self.sample_rate,
                                                       self._oct_weighting)
            octave_frame = OctaveFrame(time=elapsed_time, center_freqs=centers,
                                       levels=levels, weighting=self._oct_weighting)
        else:
            octave_frame = None

        # STFT — accumulate in overlap buffer
        self._stft_buffer = np.concatenate([self._stft_buffer, sig])
        stft_frames: list[StftFrame] = []
        if self._stft_buffer.size >= self._nfft:
            stft_signal = self._stft_buffer
            buffer_duration_sec = stft_signal.size / self.sample_rate
            buffer_start_sec = elapsed_time - buffer_duration_sec
            chunk_end_sample_index = int(round(elapsed_time * self.sample_rate))
            times, freqs, mag_db = dsp.compute_stft(
                stft_signal, self.sample_rate, self._nfft, self._overlap,
            )

            for frame_index, t_rel in enumerate(times):
                t_abs = buffer_start_sec + float(t_rel)
                frame_sample_index = int(round(t_abs * self.sample_rate))
                if frame_sample_index > chunk_end_sample_index:
                    continue
                if (
                    self._latest_stft_frame_sample_index is not None
                    and frame_sample_index <= self._latest_stft_frame_sample_index
                ):
                    continue

                stft_frames.append(
                    StftFrame(
                        time=t_abs,
                        frequencies=freqs,
                        magnitudes=mag_db[:, frame_index],
                    )
                )
                self._latest_stft_frame_sample_index = frame_sample_index

            # Keep only the overlap portion for next chunk
            keep = int(self._nfft * self._overlap)
            if keep > 0:
                self._stft_buffer = self._stft_buffer[-keep:]
            else:
                self._stft_buffer = np.array([], dtype=np.float64)
            stft_frame = stft_frames[-1] if stft_frames else None
        else:
            stft_frame = None

        return {
            "stft_frames": stft_frames,
            "stft": stft_frame,
            "spl": spl_frame,
            "loudness": loudness_frame,
            "octave": octave_frame,
            "fft": fft_frame,
        }

    def _maybe_compute_live_loudness(self, elapsed_time: float) -> LoudnessFrame | None:
        if elapsed_time - self._last_live_loudness_emit_time < _LIVE_LOUDNESS_THROTTLE_SEC:
            return None
        if self._loudness_buffer.size == 0 or self.sample_rate <= 0:
            return None

        self._last_live_loudness_emit_time = elapsed_time
        try:
            _times_sec, values_sone, _max_sone = dsp.compute_loudness_trace(
                self._loudness_buffer,
                self.sample_rate,
            )
            values = np.asarray(values_sone, dtype=np.float64).reshape(-1)
            finite_values = values[np.isfinite(values)]
            if finite_values.size == 0:
                return None
            return LoudnessFrame(
                time=elapsed_time,
                loudness_sone=float(finite_values[-1]),
            )
        except Exception:
            return None

    def reset(self):
        """Clear internal state for reuse."""
        self._stft_buffer = np.array([], dtype=np.float64)
        self._latest_stft_frame_sample_index = None
        self._fft_buffer = np.array([], dtype=np.float64)
        self._loudness_buffer = np.array([], dtype=np.float64)
        self._last_live_loudness_emit_time = 0.0
