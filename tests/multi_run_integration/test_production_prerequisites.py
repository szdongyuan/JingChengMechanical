from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest
from scipy.io import wavfile

import app.service.analysis.golden_reference as golden_module
from app.service.analysis.golden_reference import freeze_golden_artifact
from app.service.factory_selection_settings import FactorySelectionSettings
from app.service.golden_sample_service import GoldenSampleService
from app.service.managed_input_contracts import serialize_calibration_json
from app.service.managed_input_registry import ManagedInputRegistry
from app.service.multi_run_integration.production_prerequisites import (
    ProductionPrerequisiteError,
    ProductionPrerequisites,
)
from model.channel_config_table import ChannelConfigTable
from model.input_hardware_table import InputHardwareTable
from model.managed_audio_input_table import ManagedAudioInputTable
from model.plc_table import PlcTable
from model.test_config_table import TestConfigTable


class _Settings:
    def __init__(self) -> None:
        self.values: dict[str, object] = {}
        self.writes = 0

    def value(self, key: str, default=None):
        return self.values.get(key, default)

    def setValue(self, key: str, value) -> None:
        self.writes += 1
        self.values[key] = value

    def remove(self, key: str) -> None:
        self.values.pop(key, None)

    def sync(self) -> None:
        pass


class _Clock:
    monotonic_domain = "python/system-monotonic"

    def monotonic(self) -> float:
        return 1.0

    def monotonic_ns(self) -> int:
        return 1_000_000_000


@pytest.fixture(autouse=True)
def _bind_managed_table(setup_test_db):
    setup_test_db.bind([ManagedAudioInputTable])
    setup_test_db.create_tables([ManagedAudioInputTable])
    yield
    setup_test_db.drop_tables([ManagedAudioInputTable])


def _managed(
    *,
    channels: int = 2,
    trusted: tuple[int, ...] = (0, 1),
    alias: str = "managed-one",
    hardware_revision: int = 1,
    stable_backend_uid: str | None = None,
    device_name: str = "Input",
    hostapi_name: str = "API",
    calibration_revision: int = 1,
):
    calibration = tuple(
        (index, index in trusted, 1 if index in trusted else 0, 0.0 if index in trusted else None)
        for index in range(channels)
    )
    return ManagedAudioInputTable.create(
        alias=alias,
        hardware_revision=hardware_revision,
        stable_backend_uid=stable_backend_uid,
        descriptor_device_name=device_name,
        descriptor_hostapi_name=hostapi_name,
        descriptor_sample_rate_hz=48_000.0,
        descriptor_input_channels=channels,
        calibration_revision=calibration_revision,
        calibration_json=serialize_calibration_json(
            calibration,
            expected_channels=channels,
        ),
    )


def _config(
    hardware_id: int | None,
    *,
    selected=(0, 1),
    name: str = "production",
):
    row = TestConfigTable.create(
        config_name=name,
        input_hardware_device_id=hardware_id,
        input_device_samplerate=48_000.0,
        selected_channels=json.dumps(list(selected)),
        analysis_duration=1.0,
    )
    for channel in selected:
        ChannelConfigTable.create(
            config=row,
            channel_index=channel,
            spl_weighting="Z",
        )
    return row


def _settings_for(config_ids: tuple[int, ...]):
    raw = _Settings()
    settings = FactorySelectionSettings(raw)
    assert settings.save_selected_config_ids(config_ids) is None
    return settings, raw


def _prepare(config_ids: tuple[int, ...]):
    settings, raw = _settings_for(config_ids)
    clock = _Clock()
    prepared = ProductionPrerequisites(settings).prepare(
        monotonic_authority=clock
    )
    return prepared, settings, raw, clock


def test_gate_reads_only_test_config_and_managed_audio_input_tables(setup_test_db):
    managed = _managed()
    config = _config(managed.hardware_id)
    settings, raw = _settings_for((config.config_id,))
    writes_before = raw.writes

    prepared = ProductionPrerequisites(settings).prepare(
        monotonic_authority=_Clock()
    )

    assert prepared.selected_config_ids == (config.config_id,)
    assert prepared.configs[0].input_hardware_device_id == managed.hardware_id
    assert raw.writes == writes_before


def test_prepare_uses_hardware_added_by_hardware_management(setup_test_db):
    hardware = InputHardwareTable.create(
        alias="new-computer-input",
        device_name="USB Audio",
        hostapi="Windows WASAPI",
        samplerate=48_000.0,
        channel_count=2,
        mic_cal=json.dumps(
            {
                "In1": {
                    "deviation_value": "0",
                    "binding_index": 0,
                    "is_selected": 1,
                },
                "In2": {
                    "deviation_value": "0",
                    "binding_index": 1,
                    "is_selected": 1,
                },
            }
        ),
    )
    config = _config(hardware.hardware_id)
    settings, _ = _settings_for((config.config_id,))

    prepared = ProductionPrerequisites(settings).prepare(
        monotonic_authority=_Clock()
    )
    try:
        assert prepared.configs[0].managed_input.hardware_id == hardware.hardware_id
        assert prepared.configs[0].managed_input.descriptor_device_name == "USB Audio"
    finally:
        prepared.close()


def test_prepare_ignores_stale_unselected_hardware_rows(setup_test_db):
    selected_hardware = InputHardwareTable.create(
        alias="selected-input",
        device_name="Selected Audio",
        hostapi="Windows WASAPI",
        samplerate=48_000.0,
        channel_count=1,
        mic_cal=json.dumps(
            {
                "In1": {
                    "deviation_value": "0",
                    "binding_index": 0,
                    "is_selected": 1,
                }
            }
        ),
    )
    selected_config = _config(selected_hardware.hardware_id, selected=(0,))
    for index in range(7):
        InputHardwareTable.create(
            alias=f"stale-{index}",
            device_name=f"Stale Audio {index}",
            hostapi="Windows WASAPI",
            samplerate=44_100.0 + index,
            channel_count=0,
            mic_cal="not-json",
        )
    settings, _ = _settings_for((selected_config.config_id,))

    prepared = ProductionPrerequisites(settings).prepare(
        monotonic_authority=_Clock()
    )
    try:
        assert tuple(
            item.hardware_id for item in prepared.managed_inputs
        ) == (selected_hardware.hardware_id,)
        assert prepared.configs[0].managed_input.hardware_id == selected_hardware.hardware_id
    finally:
        prepared.close()


def test_gate_reconciles_missing_selection_and_rejects_invalid_inputs(
    setup_test_db,
):
    settings, raw = _settings_for((999,))
    writes_before = raw.writes
    with pytest.raises(ProductionPrerequisiteError):
        ProductionPrerequisites(settings).prepare(monotonic_authority=_Clock())
    assert raw.writes > writes_before
    assert settings.load_selected_config_ids() == ()

    managed = _managed(channels=1, trusted=(0,))
    stale = _config(managed.hardware_id + 99, selected=(0,))
    settings, raw = _settings_for((stale.config_id,))
    writes_before = raw.writes
    with pytest.raises(ProductionPrerequisiteError, match="managed input"):
        ProductionPrerequisites(settings).prepare(monotonic_authority=_Clock())
    assert raw.writes == writes_before

    TestConfigTable.delete().execute()
    ManagedAudioInputTable.delete().execute()
    untrusted_managed = _managed(channels=1, trusted=())
    untrusted = _config(untrusted_managed.hardware_id, selected=(0,))
    settings, raw = _settings_for((untrusted.config_id,))
    writes_before = raw.writes
    with pytest.raises(ProductionPrerequisiteError, match="untrusted"):
        ProductionPrerequisites(settings).prepare(monotonic_authority=_Clock())
    assert raw.writes == writes_before

    TestConfigTable.delete().execute()
    ManagedAudioInputTable.delete().execute()
    rows = [_managed(channels=1, trusted=(0,))]
    for index in range(1, 7):
        rows.append(
            ManagedAudioInputTable.create(
                alias=f"managed-{index}",
                hardware_revision=1,
                descriptor_device_name=f"Input {index}",
                descriptor_hostapi_name="API",
                descriptor_sample_rate_hz=48_000.0,
                descriptor_input_channels=1,
                calibration_revision=1,
                calibration_json=serialize_calibration_json(
                    ((0, True, 1, 0.0),), expected_channels=1
                ),
            )
        )
    config = _config(rows[0].hardware_id, selected=(0,))
    settings, raw = _settings_for((config.config_id,))
    writes_before = raw.writes
    prepared = ProductionPrerequisites(settings).prepare(
        monotonic_authority=_Clock()
    )
    try:
        assert tuple(
            item.hardware_id for item in prepared.managed_inputs
        ) == (rows[0].hardware_id,)
        assert raw.writes == writes_before
    finally:
        prepared.close()


def test_gate_reuses_complete_canonical_managed_row_validation(
    setup_test_db,
    monkeypatch,
):
    managed = _managed(
        channels=2,
        trusted=(0, 1),
        alias="canonical-input",
        hardware_revision=7,
        stable_backend_uid="uid:production-input",
        device_name="Production Input",
        hostapi_name="ASIO",
        calibration_revision=9,
    )
    config = _config(managed.hardware_id)
    settings, _ = _settings_for((config.config_id,))
    calls = []
    established_load_rows = ManagedInputRegistry._load_rows

    def tracked_load_rows(registry):
        calls.append(registry._table)
        return established_load_rows(registry)

    monkeypatch.setattr(ManagedInputRegistry, "_load_rows", tracked_load_rows)

    prepared = ProductionPrerequisites(settings).prepare(
        monotonic_authority=_Clock()
    )

    assert len(calls) == 1
    frozen = prepared.managed_inputs[0]
    assert frozen.alias == "canonical-input"
    assert frozen.hardware_revision == 7
    assert frozen.stable_backend_uid == "uid:production-input"
    assert frozen.descriptor_device_name == "Production Input"
    assert frozen.descriptor_hostapi_name == "ASIO"
    assert frozen.descriptor_input_channels == 2
    assert frozen.calibration_revision == 9
    assert frozen.calibration == (
        (0, True, 1, 0.0),
        (1, True, 1, 0.0),
    )


def test_gate_rejects_row_that_only_partial_managed_validation_would_accept(
    setup_test_db,
):
    managed = _managed(channels=1, trusted=(0,))
    config = _config(managed.hardware_id, selected=(0,))
    settings, raw = _settings_for((config.config_id,))
    writes_before = raw.writes
    ManagedAudioInputTable.update(alias=" not-canonical ").where(
        ManagedAudioInputTable.hardware_id == managed.hardware_id
    ).execute()

    with pytest.raises(ProductionPrerequisiteError, match="managed input"):
        ProductionPrerequisites(settings).prepare(monotonic_authority=_Clock())

    assert raw.writes == writes_before


def test_prepare_freezes_authoritative_selection_generation_ids_and_config_rows(
    setup_test_db,
):
    managed = _managed()
    config = _config(managed.hardware_id)
    prepared, settings, _, _ = _prepare((config.config_id,))
    generation, ids = settings.load_committed_selection()
    assert prepared.selection_generation == generation
    assert prepared.selected_config_ids == ids
    assert prepared.configs[0].selected_channels == (0, 1)
    assert tuple(c.channel_index for c in prepared.configs[0].channels) == (0, 1)


def test_prepare_freezes_foreign_key_plc_as_canonical_plc_id(setup_test_db):
    managed = _managed()
    plc = PlcTable.create(plc_name="Line PLC")
    config = _config(managed.hardware_id)
    TestConfigTable.update(plc=plc.id).where(
        TestConfigTable.config_id == config.config_id
    ).execute()

    prepared, _, _, _ = _prepare((config.config_id,))
    try:
        values = prepared.configs[0].values
        assert values["plc_id"] == plc.id
        assert "plc" not in values
    finally:
        prepared.close()


def test_prepare_reconciles_deleted_selected_id_to_first_existing_config(
    setup_test_db,
):
    managed = _managed()
    first = _config(managed.hardware_id)
    second = _config(managed.hardware_id, name="second")
    settings, _ = _settings_for((999,))

    prepared = ProductionPrerequisites(settings).prepare(
        monotonic_authority=_Clock()
    )
    try:
        assert prepared.selected_config_ids == (first.config_id,)
        assert prepared.configs[0].config_id == first.config_id
        assert settings.load_selected_config_ids() == (first.config_id,)
        assert second.config_id != first.config_id
    finally:
        prepared.close()


def test_prepare_ignores_unselected_channel_parameter_rows(setup_test_db):
    managed = _managed(channels=2, trusted=(0, 1))
    config = _config(managed.hardware_id, selected=(0,))
    ChannelConfigTable.create(
        config=config,
        channel_index=1,
        spl_weighting="A",
    )

    prepared, _, _, _ = _prepare((config.config_id,))
    try:
        frozen = prepared.configs[0]
        assert frozen.selected_channels == (0,)
        assert tuple(item.channel_index for item in frozen.channels) == (0,)
    finally:
        prepared.close()


def test_prepare_accepts_a_weighted_selected_vibration_with_acoustic_channel(
    setup_test_db,
):
    managed = _managed(channels=2, trusted=(0, 1))
    config = _config(managed.hardware_id, selected=(0, 1))
    TestConfigTable.update(vibration_view_channel_index=1).where(
        TestConfigTable.config_id == config.config_id
    ).execute()
    ChannelConfigTable.update(spl_weighting="A").where(
        (ChannelConfigTable.config == config.config_id)
        & (ChannelConfigTable.channel_index == 1)
    ).execute()
    settings, _ = _settings_for((config.config_id,))

    prepared = ProductionPrerequisites(settings).prepare(
        monotonic_authority=_Clock()
    )
    try:
        frozen = prepared.configs[0]
        assert frozen.selected_channels == (0, 1)
        assert frozen.values["vibration_view_channel_index"] == 1
        assert tuple(item.channel_index for item in frozen.channels) == (0, 1)
        assert [
            item.values["spl_weighting"] for item in frozen.channels
        ] == ["Z", "A"]
    finally:
        prepared.close()


def test_prepare_accepts_a_weighted_selected_acoustic_channel(setup_test_db):
    managed = _managed(channels=1, trusted=(0,))
    config = _config(managed.hardware_id, selected=(0,))
    ChannelConfigTable.update(spl_weighting="A").where(
        ChannelConfigTable.config == config.config_id
    ).execute()
    settings, _ = _settings_for((config.config_id,))

    prepared = ProductionPrerequisites(settings).prepare(
        monotonic_authority=_Clock()
    )
    try:
        assert prepared.configs[0].channels[0].values["spl_weighting"] == "A"
    finally:
        prepared.close()


def test_prepare_accepts_a_weighted_vibration_only_selection(setup_test_db):
    managed = _managed(channels=1, trusted=(0,))
    config = _config(managed.hardware_id, selected=(0,))
    TestConfigTable.update(vibration_view_channel_index=0).where(
        TestConfigTable.config_id == config.config_id
    ).execute()
    ChannelConfigTable.update(spl_weighting="A").where(
        ChannelConfigTable.config == config.config_id
    ).execute()
    settings, _ = _settings_for((config.config_id,))

    prepared = ProductionPrerequisites(settings).prepare(
        monotonic_authority=_Clock()
    )
    try:
        frozen = prepared.configs[0]
        assert frozen.selected_channels == (0,)
        assert frozen.values["vibration_view_channel_index"] == 0
        assert tuple(item.channel_index for item in frozen.channels) == (0,)
        assert frozen.channels[0].values["spl_weighting"] == "A"
    finally:
        prepared.close()


def test_activate_validation_rejects_selection_generation_or_id_drift(
    setup_test_db,
):
    managed = _managed()
    config = _config(managed.hardware_id)
    prepared, settings, _, _ = _prepare((config.config_id,))
    prepared.validate_activation()
    prepared.close()
    assert settings.save_selected_config_ids((config.config_id,)) is None
    with pytest.raises(ProductionPrerequisiteError, match="selection"):
        prepared.validate_activation()


def test_prepared_snapshot_never_rereads_config_channel_calibration_or_hardware_rows(
    setup_test_db,
):
    managed = _managed()
    config = _config(managed.hardware_id)
    prepared, _, _, _ = _prepare((config.config_id,))

    TestConfigTable.update(selected_channels="[1]").where(
        TestConfigTable.config_id == config.config_id
    ).execute()
    ChannelConfigTable.update(spl_weighting="A").where(
        ChannelConfigTable.config == config.config_id
    ).execute()
    ManagedAudioInputTable.update(
        calibration_json=serialize_calibration_json(
            ((0, False, 1, None), (1, False, 1, None)),
            expected_channels=2,
        ),
        descriptor_device_name="Changed",
    ).where(ManagedAudioInputTable.hardware_id == managed.hardware_id).execute()

    prepared.validate_activation()
    assert prepared.configs[0].selected_channels == (0, 1)
    assert prepared.configs[0].channels[0].values["spl_weighting"] == "Z"
    assert prepared.managed_inputs[0].descriptor_device_name == "Input"
    assert prepared.managed_inputs[0].calibration[0][1] is True


def test_prerequisites_keep_application_consumers_in_python_system_monotonic_domain(
    setup_test_db,
):
    from app.service.managed_input_sounddevice_backend import (
        PYTHON_SYSTEM_MONOTONIC_DOMAIN,
    )

    managed = _managed(channels=1, trusted=(0,))
    config = _config(managed.hardware_id, selected=(0,))
    prepared, _, _, _ = _prepare((config.config_id,))
    resident = prepared.build_resident_adapter(
        backend=object(),
        level_meter=lambda *_: 0.0,
        max_capture_frames=512,
    )

    class SoundDevice:
        @staticmethod
        def InputStream(**_kwargs):
            raise AssertionError("native stream need not open in this test")

    sounddevice = prepared.build_sounddevice_audio_backend(SoundDevice())

    assert prepared.monotonic_domain == PYTHON_SYSTEM_MONOTONIC_DOMAIN
    assert resident._monotonic_ns() == 1_000_000_000
    assert sounddevice.monotonic_domain == PYTHON_SYSTEM_MONOTONIC_DOMAIN


def test_prerequisites_reject_different_monotonic_source_domain(
    setup_test_db,
):
    class DifferentClock:
        monotonic_domain = "different-steady-clock"

        @staticmethod
        def monotonic() -> float:
            return 1.0

        @staticmethod
        def monotonic_ns() -> int:
            return 1_000_000_000

    settings, _ = _settings_for(())
    with pytest.raises(ProductionPrerequisiteError, match="Python/system"):
        ProductionPrerequisites(settings).prepare(
            monotonic_authority=DifferentClock()
        )


def test_production_inputs_open_at_most_six_with_explicit_512_frames(
    setup_test_db,
):
    managed = _managed(channels=1, trusted=(0,))
    config = _config(managed.hardware_id, selected=(0,))
    prepared, _, _, _ = _prepare((config.config_id,))
    resident = prepared.build_resident_adapter(
        backend=object(),
        level_meter=lambda *_: 0.0,
        max_capture_frames=48_000,
    )
    assert resident._frames_per_buffer == 512


def test_prepare_freezes_golden_bytes_digest_and_exact_wav_facts(
    setup_test_db, tmp_path: Path
):
    root = tmp_path / "golden"
    root.mkdir()
    path = root / "committed.wav"
    signal = np.arange(48_000, dtype=np.int16)
    wavfile.write(path, 48_000, signal)
    managed = _managed(channels=1, trusted=(0,))
    config = _config(managed.hardware_id, selected=(0,))
    ChannelConfigTable.update(
        golden_sample_path=str(path),
        golden_sample_source_channel=0,
        golden_sample_start_sample=0,
        golden_sample_num_samples=48_000,
        golden_sample_start_ms=0,
        golden_sample_duration_ms=1000,
        golden_sample_sample_rate=48_000.0,
        fft_golden_mode="absolute",
    ).where(ChannelConfigTable.config == config.config_id).execute()
    settings, _ = _settings_for((config.config_id,))
    prepared = ProductionPrerequisites(
        settings,
        golden_sample_service=GoldenSampleService(root_dir=str(root)),
    ).prepare(monotonic_authority=_Clock())
    artifact = prepared.golden_artifacts[0]
    assert artifact.config_id == config.config_id
    assert artifact.channel_index == 0
    assert artifact.byte_size == path.stat().st_size
    assert artifact.sample_rate_hz == 48_000
    assert artifact.sample_width_bytes == 2
    assert artifact.channel_count == 1
    assert artifact.frame_count == 48_000
    assert artifact.signal.flags.writeable is False
    assert artifact.rehash() == artifact.sha256
    prepared.close()


def test_prepare_accepts_nonzero_golden_source_channel_as_audit_binding(
    setup_test_db,
    tmp_path: Path,
):
    root = tmp_path / "golden"
    root.mkdir()
    path = root / "committed.wav"
    wavfile.write(path, 48_000, np.arange(48_000, dtype=np.int16))
    managed = _managed(channels=1, trusted=(0,))
    config = _config(managed.hardware_id, selected=(0,))
    ChannelConfigTable.update(
        golden_sample_path=str(path),
        golden_sample_source_channel=7,
        golden_sample_start_sample=0,
        golden_sample_num_samples=48_000,
        golden_sample_start_ms=0,
        golden_sample_duration_ms=1000,
        golden_sample_sample_rate=48_000.0,
        fft_golden_mode="absolute",
    ).where(ChannelConfigTable.config == config.config_id).execute()
    settings, _ = _settings_for((config.config_id,))

    prepared = ProductionPrerequisites(
        settings,
        golden_sample_service=GoldenSampleService(root_dir=str(root)),
    ).prepare(monotonic_authority=_Clock())

    assert prepared.configs[0].channels[0].values[
        "golden_sample_source_channel"
    ] == 7
    assert prepared.golden_artifacts[0].channel_count == 1
    prepared.close()


def test_prepare_rejects_golden_source_identity_or_digest_drift_during_single_read(
    setup_test_db, tmp_path: Path, monkeypatch
):
    root = tmp_path / "golden"
    root.mkdir()
    path = root / "committed.wav"
    wavfile.write(path, 48_000, np.zeros(48_000, dtype=np.int16))
    original = golden_module._file_identity
    calls = 0

    def drifting(handle):
        nonlocal calls
        calls += 1
        identity = original(handle)
        if calls == 2:
            return (*identity[:-1], identity[-1] + 1)
        return identity

    monkeypatch.setattr(golden_module, "_file_identity", drifting)
    with pytest.raises(Exception, match="changed"):
        freeze_golden_artifact(
            config_id=7,
            channel_index=0,
            channel_config={
                "golden_sample_path": str(path),
                "golden_sample_source_channel": 0,
                "golden_sample_start_sample": 0,
                "golden_sample_num_samples": 48_000,
                "golden_sample_start_ms": 0,
                "golden_sample_duration_ms": 1000,
                "golden_sample_sample_rate": 48_000.0,
            },
            runtime_sample_rate=48_000.0,
            analysis_duration=1.0,
            managed_root=str(root),
        )
