from __future__ import annotations

import os
from pathlib import Path
import threading
import wave

import pytest

from tests.test_audio_wav import write_numeric_wav

from app.service.run_persistence import audio_publication as publication_module
from app.service.run_persistence.audio_publication import (
    AudioPublication,
    AudioPublicationCode,
    verify_wav_pcm,
)


def wav_bytes(path: Path, *, rate=8_000, channels=1, width=2, frames=8) -> bytes:
    with wave.open(str(path), "wb") as writer:
        writer.setframerate(rate)
        writer.setnchannels(channels)
        writer.setsampwidth(width)
        writer.writeframes(b"\x01\x00" * frames * channels)
    return path.read_bytes()


@pytest.mark.parametrize('bits,floating', [(24, False), (32, True), (64, True)])
def test_publication_preserves_original_precision_bytes(tmp_path, bits, floating):
    source = tmp_path / 'precision.wav'
    write_numeric_wav(source, bits=bits, floating=floating)
    final = tmp_path / 'archive.wav'
    result = AudioPublication().publish(source, final, 8000, 2, bits // 8, 3)
    assert result.ok, result.reason
    assert final.read_bytes() == source.read_bytes()
    assert result.metadata.container_format == ('WAV_IEEE_FLOAT' if floating else 'WAV_PCM')
    assert result.metadata.frame_count == 3
    assert result.metadata.channel_count == 2


def test_pcm_only_verifier_keeps_rejecting_ieee_float(tmp_path):
    path = tmp_path / 'float.wav'
    write_numeric_wav(path, bits=32, floating=True)
    with pytest.raises(ValueError, match='PCM'):
        verify_wav_pcm(path, 8000, 2, 4, 3)


def test_verify_real_supported_pcm_and_rejects_invalid_or_incomplete(tmp_path):
    path = tmp_path / "source.wav"
    expected = wav_bytes(path)

    metadata = verify_wav_pcm(path, 8_000, 1, 2)

    assert metadata.sample_rate_hz == 8_000
    assert metadata.channel_count == 1
    assert metadata.frame_count == 8
    assert metadata.duration_ms == 1
    assert metadata.byte_size == len(expected)

    invalid = tmp_path / "invalid.wav"
    invalid.write_bytes(b"not wav")
    with pytest.raises(ValueError):
        verify_wav_pcm(invalid, 8_000, 1, 2)

    incomplete = tmp_path / "incomplete.wav"
    incomplete.write_bytes(expected[:20])
    with pytest.raises(ValueError):
        verify_wav_pcm(incomplete, 8_000, 1, 2)


def test_verify_rejects_writer_that_is_still_open_and_incomplete(tmp_path):
    path = tmp_path / "open-writer.wav"
    writer = wave.open(str(path), "wb")
    writer.setframerate(8_000)
    writer.setnchannels(1)
    writer.setsampwidth(2)
    try:
        with pytest.raises(ValueError):
            verify_wav_pcm(path, 8_000, 1, 2)
    finally:
        writer.close()


def test_positive_sub_millisecond_pcm_duration_rounds_up(tmp_path):
    path = tmp_path / "one-frame.wav"
    wav_bytes(path, rate=48_000, frames=1)

    metadata = verify_wav_pcm(path, 48_000, 1, 2)

    assert metadata.frame_count == 1
    assert metadata.duration_ms == 1


@pytest.mark.parametrize(
    ("rate", "channels", "width"),
    [(16_000, 1, 2), (8_000, 2, 2), (8_000, 1, 1)],
)
def test_verify_rejects_input_format_mismatch(tmp_path, rate, channels, width):
    path = tmp_path / "mismatch.wav"
    wav_bytes(path, rate=rate, channels=channels, width=width)
    with pytest.raises(ValueError):
        verify_wav_pcm(path, 8_000, 1, 2)


def test_verify_rejects_zero_frames_and_truncated_pcm_payload(tmp_path):
    zero = tmp_path / "zero.wav"
    with wave.open(str(zero), "wb") as writer:
        writer.setframerate(8_000)
        writer.setnchannels(1)
        writer.setsampwidth(2)
        writer.writeframes(b"")
    with pytest.raises(ValueError):
        verify_wav_pcm(zero, 8_000, 1, 2)

    truncated = tmp_path / "truncated.wav"
    content = bytearray(wav_bytes(truncated))
    truncated.write_bytes(content[:-1])
    with pytest.raises(ValueError):
        verify_wav_pcm(truncated, 8_000, 1, 2)


def test_verify_wav_pcm_rejects_frame_mismatch_before_publication(
    tmp_path,
) -> None:
    source = tmp_path / "source.wav"
    wav_bytes(source, frames=8)
    final = tmp_path / "final.wav"

    with pytest.raises(ValueError, match="frame count"):
        verify_wav_pcm(
            source,
            8_000,
            1,
            2,
            expected_frame_count=7,
        )

    assert not final.exists()


def test_publication_normalizes_path_uses_exclusive_temp_and_no_replace(
    tmp_path, monkeypatch
):
    source = tmp_path / "source.wav"
    source_bytes = wav_bytes(source)
    (tmp_path / "nested").mkdir()
    final = tmp_path / "nested" / ".." / "final.wav"
    publication = AudioPublication()
    observed_flags = []
    real_open = os.open

    def recording_open(path, flags, mode=0o777):
        observed_flags.append(flags)
        return real_open(path, flags, mode)

    monkeypatch.setattr(os, "open", recording_open)
    result = publication.publish(source, final, 8_000, 1, 2)

    assert result.ok
    assert result.metadata is not None
    assert result.metadata.final_path == str(final.resolve(strict=False))
    assert Path(result.metadata.final_path).read_bytes() == source_bytes
    assert any(flags & os.O_EXCL for flags in observed_flags)

    before_mtime = Path(result.metadata.final_path).stat().st_mtime_ns
    source.write_bytes(source_bytes + b"other")
    collision = publication.publish(source, final, 8_000, 1, 2)
    assert collision.code is AudioPublicationCode.AUDIO_PATH_COLLISION
    assert Path(result.metadata.final_path).read_bytes() == source_bytes
    assert Path(result.metadata.final_path).stat().st_mtime_ns == before_mtime


def test_two_writer_race_preserves_winner_bytes(tmp_path):
    first = tmp_path / "first.wav"
    second = tmp_path / "second.wav"
    first_bytes = wav_bytes(first, frames=8)
    second_bytes = wav_bytes(second, frames=12)
    final = tmp_path / "winner.wav"
    barrier = threading.Barrier(2)
    publication = AudioPublication(before_finalize=barrier.wait)
    results = []

    threads = [
        threading.Thread(
            target=lambda source=source: results.append(
                publication.publish(source, final, 8_000, 1, 2)
            )
        )
        for source in (first, second)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert sum(result.ok for result in results) == 1
    assert [result.code for result in results if not result.ok] == [
        AudioPublicationCode.AUDIO_PATH_COLLISION
    ]
    assert final.read_bytes() in (first_bytes, second_bytes)


@pytest.mark.parametrize(
    ("fault", "code"),
    [
        ("write", AudioPublicationCode.AUDIO_WRITE_FAILED),
        ("close", AudioPublicationCode.AUDIO_CLOSE_FAILED),
        ("verify", AudioPublicationCode.AUDIO_VERIFY_FAILED),
        ("finalize", AudioPublicationCode.AUDIO_FINALIZE_FAILED),
    ],
)
def test_publication_maps_faults_and_attempts_cleanup_once(
    tmp_path, monkeypatch, fault, code
):
    source = tmp_path / "source.wav"
    wav_bytes(source)
    publication = AudioPublication()
    cleanup_calls = []

    def fail(*args, **kwargs):
        raise OSError(f"{fault} failed")

    monkeypatch.setattr(publication, f"_{fault}", fail)
    real_cleanup = publication._cleanup

    def count_cleanup(path):
        cleanup_calls.append(path)
        return real_cleanup(path)

    monkeypatch.setattr(publication, "_cleanup", count_cleanup)
    result = publication.publish(source, tmp_path / "final.wav", 8_000, 1, 2)

    assert result.code is code
    assert len(cleanup_calls) == 1


def test_cleanup_failure_is_reported_once_with_exact_path(tmp_path, monkeypatch):
    source = tmp_path / "source.wav"
    wav_bytes(source)
    publication = AudioPublication()
    cleanup_calls = []
    monkeypatch.setattr(
        publication, "_verify", lambda *args: (_ for _ in ()).throw(ValueError("bad"))
    )

    def cleanup_fails(path):
        cleanup_calls.append(path)
        raise OSError("locked")

    monkeypatch.setattr(publication, "_cleanup", cleanup_fails)
    result = publication.publish(source, tmp_path / "final.wav", 8_000, 1, 2)

    assert result.code is AudioPublicationCode.AUDIO_VERIFY_FAILED
    assert result.cleanup_code is AudioPublicationCode.AUDIO_TEMP_CLEANUP_FAILED
    assert result.cleanup_path == str(cleanup_calls[0])
    assert len(cleanup_calls) == 1


def test_post_verify_final_size_mismatch_leaves_orphan(tmp_path, monkeypatch):
    source = tmp_path / "source.wav"
    source_bytes = wav_bytes(source)
    final = tmp_path / "final.wav"
    publication = AudioPublication()

    def grow_after_finalize(path):
        with path.open("ab") as output:
            output.write(b"x")
        return path.stat().st_size

    monkeypatch.setattr(publication, "_final_size", grow_after_finalize)
    result = publication.publish(source, final, 8_000, 1, 2)

    assert result.code is AudioPublicationCode.AUDIO_FINAL_SIZE_MISMATCH
    assert final.exists()
    assert final.read_bytes() == source_bytes + b"x"


@pytest.mark.parametrize(
    ("error", "final_exists"),
    [
        (FileNotFoundError("final disappeared"), False),
        (OSError("final is inaccessible"), True),
    ],
)
def test_post_final_stat_failures_are_typed_and_never_delete_or_retry(
    tmp_path, monkeypatch, error, final_exists
):
    source = tmp_path / "source.wav"
    source_bytes = wav_bytes(source)
    final = tmp_path / "final.wav"
    publication = AudioPublication()
    calls = 0

    def fail_final_recheck(path):
        nonlocal calls
        calls += 1
        if isinstance(error, FileNotFoundError):
            path.unlink()
        raise error

    monkeypatch.setattr(publication, "_final_size", fail_final_recheck)
    result = publication.publish(source, final, 8_000, 1, 2)

    assert result.code is AudioPublicationCode.AUDIO_FINAL_SIZE_MISMATCH
    assert calls == 1
    assert final.exists() is final_exists
    if final_exists:
        assert final.read_bytes() == source_bytes


def test_preexisting_deterministic_temp_collision_is_never_owned_or_cleaned(
    tmp_path, monkeypatch
):
    source = tmp_path / "source.wav"
    wav_bytes(source)
    final = tmp_path / "final.wav"
    monkeypatch.setattr(publication_module, "uuid4", lambda: "fixed")
    temporary = tmp_path / ".final.wav.fixed.part"
    temporary.write_bytes(b"someone-elses-temp")
    before_mtime = temporary.stat().st_mtime_ns
    publication = AudioPublication()
    cleanup_calls = []
    monkeypatch.setattr(
        publication, "_cleanup", lambda path: cleanup_calls.append(path)
    )

    result = publication.publish(source, final, 8_000, 1, 2)

    assert result.code is AudioPublicationCode.AUDIO_WRITE_FAILED
    assert cleanup_calls == []
    assert temporary.read_bytes() == b"someone-elses-temp"
    assert temporary.stat().st_mtime_ns == before_mtime
    assert not final.exists()


def test_fdopen_failure_closes_raw_descriptor_once_and_cleans_owned_temp(
    tmp_path, monkeypatch
):
    source = tmp_path / "source.wav"
    wav_bytes(source)
    final = tmp_path / "final.wav"
    publication = AudioPublication()
    opened_descriptors = []
    close_calls = []
    real_open = os.open
    real_close = os.close

    def recording_open(*args, **kwargs):
        descriptor = real_open(*args, **kwargs)
        opened_descriptors.append(descriptor)
        return descriptor

    def recording_close(descriptor):
        close_calls.append(descriptor)
        return real_close(descriptor)

    monkeypatch.setattr(os, "open", recording_open)
    monkeypatch.setattr(os, "close", recording_close)
    monkeypatch.setattr(
        os, "fdopen", lambda *args, **kwargs: (_ for _ in ()).throw(OSError("fdopen"))
    )

    result = publication.publish(source, final, 8_000, 1, 2)
    if opened_descriptors and not close_calls:
        real_close(opened_descriptors[0])

    assert result.code is AudioPublicationCode.AUDIO_WRITE_FAILED
    assert close_calls == opened_descriptors
    assert list(tmp_path.glob("*.part")) == []
    assert not final.exists()
