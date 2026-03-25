from __future__ import annotations

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QLabel, QStackedLayout, QVBoxLayout, QWidget

from app.services_container import FactoryControllerState
from ui.widgets.fake_plots import FakeLinePlot


class SpectrumModule(QWidget):
    module_id = "spectrum"
    _DISPLAY_PHASES = frozenset({"running", "analyzing", "reviewing", "reanalyzing"})

    def __init__(self, services):
        super().__init__()
        self.services = services
        self._factory_state = getattr(services.factory_mode, "state", FactoryControllerState())
        self._has_plot_data = False
        self._display_run_id = getattr(
            getattr(services.factory_mode, "review_payload", None),
            "run_id",
            None,
        )

        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)

        self.stack = QStackedLayout()
        self.stack.setContentsMargins(0, 0, 0, 0)
        lay.addLayout(self.stack, 1)

        self.empty_state = QLabel("No data")
        self.empty_state.setObjectName("ChartEmptyState")
        self.empty_state.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.stack.addWidget(self.empty_state)

        self.plot = FakeLinePlot()
        self.stack.addWidget(self.plot)

        if hasattr(self.services.bus, "factory_state_changed"):
            self.services.bus.factory_state_changed.connect(self._on_factory_state_changed)
        if hasattr(self.services.bus, "factory_display_reset"):
            self.services.bus.factory_display_reset.connect(self._on_factory_display_reset)
        if hasattr(self.services.bus, "factory_runtime_start_accepted"):
            self.services.bus.factory_runtime_start_accepted.connect(self._on_factory_runtime_context)
        if hasattr(self.services.bus, "factory_runtime_analyzing_entered"):
            self.services.bus.factory_runtime_analyzing_entered.connect(self._on_factory_runtime_context)
        if hasattr(self.services.bus, "factory_review_changed"):
            self.services.bus.factory_review_changed.connect(self._on_factory_review_changed)
        if hasattr(self.services.bus, "fft_updated"):
            self.services.bus.fft_updated.connect(self._on_fft_updated)

        self._update_visible_state()

    def _on_factory_state_changed(self, state):
        previous_config_id = self._factory_state.displayed_config_id
        if isinstance(state, FactoryControllerState):
            self._factory_state = state
        else:
            self._factory_state = FactoryControllerState()

        if (
            self._factory_state.displayed_config_id != previous_config_id
            or not self._has_display_context()
        ):
            self._has_plot_data = False
        if not self._has_display_context():
            self._display_run_id = None
        self._update_visible_state()

    def _on_factory_display_reset(self, _reason: str):
        self._display_run_id = None
        self._has_plot_data = False
        self._update_visible_state()

    def _on_factory_runtime_context(self, payload) -> None:
        run_id = getattr(payload, "run_id", None)
        if isinstance(run_id, str) and run_id:
            self._display_run_id = run_id

    def _on_factory_review_changed(self, review) -> None:
        run_id = getattr(review, "run_id", None)
        if isinstance(run_id, str) and run_id:
            self._display_run_id = run_id

    def _on_fft_updated(self, config_id: int, run_id: str, _channel_idx: int, _frame) -> None:
        if (
            config_id != self._factory_state.displayed_config_id
            or run_id != self._display_run_id
            or not self._has_display_context()
        ):
            return
        self._has_plot_data = True
        self._update_visible_state()

    def _has_display_context(self) -> bool:
        return (
            self._factory_state.displayed_config_id is not None
            and self._factory_state.run_phase in self._DISPLAY_PHASES
        )

    def _update_visible_state(self) -> None:
        self.stack.setCurrentWidget(self.plot if self._has_plot_data else self.empty_state)

