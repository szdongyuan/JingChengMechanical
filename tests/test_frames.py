"""Tests for analysis frame dataclasses."""
from __future__ import annotations

from dataclasses import asdict

import numpy as np

from app.service.analysis.frames import (
    BandResult,
    ChannelJudgment,
    FftFrame,
    JudgmentResult,
    OctaveFrame,
    SplFrame,
    StftFrame,
)


class TestStftFrame:
    def test_create(self):
        f = StftFrame(time=0.5, frequencies=np.array([100.0, 200.0]),
                      magnitudes=np.array([60.0, 55.0]))
        assert f.time == 0.5
        assert f.frequencies.shape == (2,)
        assert f.magnitudes.shape == (2,)


class TestSplFrame:
    def test_create(self):
        f = SplFrame(time=1.0, spl_db=72.5, weighting="A")
        assert f.spl_db == 72.5
        assert f.weighting == "A"


class TestOctaveFrame:
    def test_create(self):
        f = OctaveFrame(time=0.1, center_freqs=np.array([1000.0]),
                        levels=np.array([65.0]), weighting="C")
        assert f.weighting == "C"


class TestFftFrame:
    def test_create(self):
        f = FftFrame(time=0.2, frequencies=np.array([50.0]),
                     magnitudes=np.array([70.0]))
        assert f.time == 0.2


class TestBandResult:
    def test_pass(self):
        b = BandResult(freq=1000.0, level=60.0, lower=50.0, upper=70.0, passed=True)
        assert b.passed is True

    def test_fail(self):
        b = BandResult(freq=1000.0, level=80.0, lower=50.0, upper=70.0, passed=False)
        assert b.passed is False


class TestChannelJudgment:
    def test_serializes_loudness_fields(self):
        judgment = ChannelJudgment(
            channel_index=0,
            spl_db=65.0,
            spl_pass=True,
            loudness_max_sone=8.0,
            loudness_pass=True,
            loudness_available=True,
            loudness_error_message=None,
        )

        payload = asdict(judgment)

        assert payload["loudness_max_sone"] == 8.0
        assert payload["loudness_pass"] is True
        assert payload["loudness_available"] is True
        assert payload["loudness_error_message"] is None


class TestJudgmentResult:
    def test_pass_verdict(self):
        ch = ChannelJudgment(channel_index=0, spl_db=65.0, spl_pass=True)
        r = JudgmentResult(overall="合格", limit_pass=True, ai_pass=True,
                           ai_score=0.95, channel_results=[ch],
                           analysis_window=(2.0, 5.0))
        assert r.overall == "合格"
        assert r.limit_pass is True
        assert r.ai_pass is True

    def test_no_ai(self):
        r = JudgmentResult(overall="合格", limit_pass=True, ai_pass=None,
                           ai_score=None)
        assert r.ai_pass is None

    def test_defaults(self):
        r = JudgmentResult(overall="不合格", limit_pass=False, ai_pass=None,
                           ai_score=None)
        assert r.channel_results == []
        assert r.analysis_window == (0.0, 0.0)
