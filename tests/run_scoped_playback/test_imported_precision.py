import json
import numpy as np
import pytest
from app.service.run_persistence import ProductionRunAudioAdapter
from tests.analysis_scheduler.test_imported_precision_analysis import write_tone, import_mapping
from .conftest import make_audio, make_run, make_config_snapshot, FakeAudioRepository, FakeRunRepository, FakeBackend
from .test_service_resolution import make_service, request


@pytest.mark.parametrize('kind', ['pcm24', 'float32', 'float64'])
@pytest.mark.parametrize('column', [0, 1, 2])
def test_import_replay_selects_original_columns_including_ignored(tmp_path, kind, column):
    path = tmp_path / 'archive.wav'
    direct = write_tone(path, kind, columns=3)
    envelope = json.loads(make_config_snapshot(selected_channels=[7], monitor_gain_db=20.0))
    envelope['channel_mapping'] = import_mapping()
    run = make_run(ingress_kind='IMPORTED', trigger_source='IMPORT', hardware_id=None,
                   config_snapshot_json=json.dumps(envelope))
    audio = make_audio(source_kind='IMPORTED_SOURCE', final_path=str(path.resolve()),
                       container_format='WAV_PCM' if kind == 'pcm24' else 'WAV_IEEE_FLOAT',
                       sample_rate_hz=48000, channel_count=3, frame_count=48000, duration_ms=1000,
                       byte_size=path.stat().st_size)
    backend = FakeBackend()
    service = make_service(run_repository=FakeRunRepository(run), audio_repository=FakeAudioRepository(audio),
                           audio_loader=ProductionRunAudioAdapter(None), backend=backend)
    result = service.play(request(display_channel_index=column, window_end_seconds=1.0))
    assert backend.start_arguments, result
    np.testing.assert_allclose(backend.start_arguments[0][0], direct[:, column].astype(np.float32), rtol=1e-6)
    assert backend.start_arguments[0][4] == 20.0
