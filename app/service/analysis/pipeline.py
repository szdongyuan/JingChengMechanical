"""Per-channel real-time analysis pipeline.

Receives raw audio chunks from AudioRecorder.  For each chunk, computes:
- STFT (when enough samples have accumulated)
- SPL (every chunk)
- 1/3-octave bands (every chunk)
- FFT spectrum (every chunk)
"""
from __future__ import annotations

import numpy as np

from app.service.analysis.frames import FftFrame, OctaveFrame, SplFrame, StftFrame
from app.service.analysis import dsp


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
        self._overlap: float = channel_config["stft_overlap"]

        # SPL config
        self._spl_weighting: str = channel_config["spl_weighting"]
        self._spl_tc: float = channel_config["spl_time_constant"]

        # Octave config
        self._oct_weighting: str = channel_config["oct_weighting"]

        # Internal STFT overlap buffer
        self._stft_buffer = np.array([], dtype=np.float64)

    def process_chunk(self, chunk_mono: np.ndarray,
                      elapsed_time: float) -> dict:
        """Process one audio chunk and return analysis results.

        Parameters
        ----------
        chunk_mono : 1-D float array (single channel)
        elapsed_time : seconds since recording start

        Returns
        -------
        dict with keys: "stft", "spl", "octave", "fft"
            Values are frame dataclasses or None if not enough data.
        """
        sig = np.asarray(chunk_mono, dtype=np.float64)

        # Apply mic calibration
        if self._mic_sensitivity != 0.0:
            sig = dsp.apply_mic_calibration(sig, self._mic_sensitivity)

        # SPL — always computed (even for short chunks)
        spl_db = dsp.compute_spl(sig, self.sample_rate,
                                 self._spl_weighting, self._spl_tc)
        spl_frame = SplFrame(time=elapsed_time, spl_db=spl_db,
                             weighting=self._spl_weighting)

        # FFT spectrum — use latest chunk directly
        if sig.size >= self._nfft:
            freqs, mag_db = dsp.compute_fft_spectrum(sig, self.sample_rate,
                                                     self._nfft)
            fft_frame = FftFrame(time=elapsed_time, frequencies=freqs,
                                 magnitudes=mag_db)
        else:
            fft_frame = None

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
        if self._stft_buffer.size >= self._nfft:
            # Compute STFT on accumulated buffer, then keep overlap
            times, freqs, mag_db = dsp.compute_stft(
                self._stft_buffer, self.sample_rate, self._nfft, self._overlap,
            )
            # Take the last time frame
            stft_frame = StftFrame(
                time=elapsed_time,
                frequencies=freqs,
                magnitudes=mag_db[:, -1] if mag_db.shape[1] > 0 else mag_db.ravel(),
            )
            # Keep only the overlap portion for next chunk
            keep = int(self._nfft * self._overlap)
            self._stft_buffer = self._stft_buffer[-keep:]
        else:
            stft_frame = None

        return {
            "stft": stft_frame,
            "spl": spl_frame,
            "octave": octave_frame,
            "fft": fft_frame,
        }

    def reset(self):
        """Clear internal state for reuse."""
        self._stft_buffer = np.array([], dtype=np.float64)
