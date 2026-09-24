"""Freeze saved software configuration without production preparation or leases."""
from copy import deepcopy
from dataclasses import dataclass, replace
import hashlib
import json
import math

from app.service.audio_wav import WavMetadata, probe_wav
from app.service.analysis.import_mapping import validate_import_mapping, import_calibration
from app.service.analysis.golden_reference import freeze_golden_artifact
from app.service.run_persistence.contracts import freeze_run_inputs, FrozenRunInputs


@dataclass(frozen=True)
class ImportSnapshot:
    metadata: WavMetadata
    inputs: FrozenRunInputs
    warnings: tuple[str, ...]
    golden_artifacts: tuple = ()
    source_identity: str = ''


class SnapshotPreparer:
    def __init__(
        self, *, config_service, calibration_loader, toolchain_loader, golden_root=None,
    ):
        self.config_service = config_service
        self.calibration_loader = calibration_loader
        self.toolchain_loader = toolchain_loader
        self.golden_root = golden_root

    def freeze(self, request):
        metadata = probe_wav(request.source_path)
        config = deepcopy(self.config_service.load_config(request.config_id))
        config['config_id'] = request.config_id
        config['monitor_gain_db'] = 20.0
        mapping = {
            'schema': 'audio-import-map/v2',
            'source_channel_count': metadata.channel_count,
            'assignments': deepcopy(list(request.assignments)),
        }
        validate_import_mapping(
            mapping, selected_channels=config['selected_channels'],
            channel_count=metadata.channel_count,
        )
        mapped = tuple(
            a['channel_index'] for a in mapping['assignments'] if a['mode'] == 'config'
        )
        calibration = (
            import_calibration(deepcopy(self.calibration_loader(config)), mapped)
            if request.use_mic_calibration and mapped else {}
        )
        toolchain = deepcopy(self.toolchain_loader(request.config_id)) if mapped else None
        start = float(
            request.start_seconds if request.start_seconds is not None
            else config.get('analysis_window_offset_ms', 0) / 1000
        )
        duration = float(
            request.duration_seconds if request.duration_seconds is not None
            else config.get('analysis_duration', metadata.duration_seconds)
        )
        if (
            not math.isfinite(start) or not math.isfinite(duration)
            or start < 0 or duration <= 0 or start >= metadata.duration_seconds
        ):
            raise ValueError('analysis window is empty or invalid')
        end = min(start + duration, metadata.duration_seconds)
        warnings = (
            ('分析窗口已截取到音频末尾',)
            if start + duration > metadata.duration_seconds else ()
        )
        envelope = {
            'schema': 'analysis-input/v1', 'config': config,
            'workorder': {}, 'channel_mapping': mapping,
            'analysis': {
                'window_start_seconds': start, 'window_end_seconds': end,
                'algorithm_revision': 'legacy-judgment/v1',
                'execution_timeout_seconds': max(30., duration * 2) + (
                    float(toolchain['timeout_seconds']) if toolchain else 0
                ),
                'queue_timeout_seconds': None, 'toolchain': toolchain,
            },
        }
        inputs = freeze_run_inputs(envelope, {}, calibration)
        return ImportSnapshot(metadata, inputs, warnings)

    def prepare(self, snapshot):
        envelope = json.loads(snapshot.inputs.config_snapshot_json)
        config = envelope['config']
        mapped = {
            a['channel_index'] for a in envelope['channel_mapping']['assignments']
            if a['mode'] == 'config'
        }
        goldens = []
        for channel in config.get('channels', []):
            enabled = any(
                str(channel.get(key) or 'off').lower() != 'off'
                for key in ('fft_golden_mode', 'oct_golden_mode')
            )
            if channel['channel_index'] in mapped and enabled:
                rate = (
                    channel.get('golden_sample_sample_rate')
                    or config.get('input_device_samplerate')
                    or config.get('sample_rate') or snapshot.metadata.sample_rate_hz
                )
                root = self.golden_root() if callable(self.golden_root) else self.golden_root
                goldens.append(freeze_golden_artifact(
                    config_id=config['config_id'], channel_index=channel['channel_index'],
                    channel_config=channel, runtime_sample_rate=float(rate),
                    analysis_duration=float(config.get('analysis_duration', snapshot.metadata.duration_seconds)),
                    managed_root=str(root),
                ))
        with open(snapshot.metadata.path, 'rb') as source:
            identity = 'sha256:' + hashlib.file_digest(source, 'sha256').hexdigest()
        return replace(snapshot, golden_artifacts=tuple(goldens), source_identity=identity)
