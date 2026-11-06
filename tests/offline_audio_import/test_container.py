from types import SimpleNamespace
from app.services_container import ServiceContainer, EventBus
from app.service.offline_audio_import import ImportRequest, ImportPhase
from app.service.run_persistence import AnalysisResultArtifactStore
from tests.test_service_container import _fresh_runtime
from tests.analysis_scheduler.test_imported_precision_analysis import write_tone


def test_offline_wiring_works_with_failed_production_and_raising_hardware(tmp_path):
    service = object.__new__(ServiceContainer)
    service.bus = EventBus()
    service.run_persistence_database = _fresh_runtime(tmp_path)
    from app.service.run_persistence import settle_startup
    assert settle_startup(service.run_persistence_database, '2026-09-09T00:00:00.000000Z').ready
    service.analysis_result_artifact_store = AnalysisResultArtifactStore(tmp_path / 'results')
    service._registered_independent_shutdown_resources = []
    service._shutdown_registration_closed = False
    service.final_composition = None
    service.multi_run_coordinator = None
    service.ingress_open = False
    service.production_unavailable_reason = 'no enabled configuration / missing recorder'
    def forbidden(*args, **kwargs):
        raise AssertionError('hardware, PLC, and configuration selection must not be used')
    service.hss_service = SimpleNamespace(get_input_hardware=forbidden)
    service.plc_services = SimpleNamespace(start=forbidden)
    service.factory_selection_settings = SimpleNamespace(load_committed_selection=forbidden)
    service.config_service = SimpleNamespace(load_config=lambda _: {
        'selected_channels': [7], 'channels': [{'channel_index': 7}], 'analysis_duration': 1.},
        golden_sample_service=SimpleNamespace(managed_root=lambda: str(tmp_path)))
    service.ai_service = SimpleNamespace(freeze_bound_toolchain=lambda _: None)
    service._initialize_offline_import()
    source = tmp_path / 'source.wav'; write_tone(source, 'float64')
    try:
        receipt = service.offline_audio_import_service.submit(ImportRequest(str(source), 87,
            ({'wav_channel_index': 0, 'mode': 'chart', 'channel_index': None},)))
        assert receipt.accepted, receipt.error
        service.offline_audio_import_service.wait_for_phase(ImportPhase.COMPLETED, 40)
        assert service.ingress_open is False
        assert not service.production_busy
        playback = service.historical_playback
        assert playback is service.historical_playback
        active = object()
        service.final_composition = SimpleNamespace(playback=active)
        assert service.historical_playback is active
    finally:
        for owner in service._registered_independent_shutdown_resources:
            owner.close()
        service.run_persistence_database.close_after_upstream_stopped()


def test_production_busy_reads_coordinator_activity_not_prepared_selection_lock():
    services = object.__new__(ServiceContainer)
    services._production_composition_state = 'active'
    state = SimpleNamespace(ingress_run_ids=(), accepted_active_run_ids=(), analysis_attempt_ids=(),
        result_operation_attempt_ids=(), pending_publication_run_ids=())
    services.multi_run_coordinator = SimpleNamespace(snapshot=lambda: state)
    assert services.production_selection_locked
    assert not services.production_busy
    state.accepted_active_run_ids = ('capturing-or-saving',)
    assert services.production_busy


def test_full_container_initializes_import_before_production_prepare_failure(tmp_path, monkeypatch):
    import app.services_container as module
    import app.service.factory_mode_controller as controller_module
    from app.services_container import FactoryControllerState
    class ConfigDatabase:
        def connect(self):
            pass
        def close(self):
            pass
    class BrokenProduction:
        def prepare(self):
            raise RuntimeError('recorder unavailable')
    class Controller:
        def __init__(self, bus, services, **kwargs):
            self.services = services
            self._state = FactoryControllerState()
        def initialize_selection(self):
            pass
    monkeypatch.setattr(module, 'DbConnectionService', ConfigDatabase)
    monkeypatch.setattr(module, 'FactorySelectionSettings', lambda: object())
    monkeypatch.setattr(controller_module, 'FactoryModeController', Controller)
    services = ServiceContainer(run_persistence_database=_fresh_runtime(tmp_path),
        production_composition_builder=BrokenProduction())
    try:
        assert services.production_unavailable_reason
        assert services.offline_audio_import_service is not None
        assert services.historical_playback is not None
        assert services.ingress_open is False
    finally:
        assert services.shutdown().clean
