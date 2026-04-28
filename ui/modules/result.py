from __future__ import annotations

import math
import re

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QSizePolicy,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from app.service.analysis import dsp
from app.services_container import FactoryControllerState, FactoryReviewPayload


_DISPLAY_LABEL_MAP = {
    "SPL": "声压级",
    "平均SPL": "平均声压级",
    "Loudness": "响度",
    "平均Loudness": "平均响度",
    "FFT": "FFT频谱",
    "Octave": "倍频程",
}
_CHANNEL_LABEL_RE = re.compile(r"CH(\d+)", flags=re.IGNORECASE)


class ResultModule(QWidget):
    module_id = "result"
    _ROW_LABELS = ("声压级", "响度", "FFT频谱", "倍频程", "AI分析")
    _DISPLAY_PRESERVING_PHASES = frozenset(
        {"running", "analyzing", "reanalyzing", "stopped"}
    )
    _LIVE_METRIC_PHASES = frozenset({"running", "analyzing"})
    _AVERAGE_METRIC_PHASES = frozenset({"reviewing", "stopped"})

    def __init__(self, services):
        super().__init__()
        self.services = services
        self._factory_state = getattr(services.factory_mode, "state", FactoryControllerState())
        self._review_payload = getattr(services.factory_mode, "review_payload", FactoryReviewPayload())
        self.selected_display_channel_provider = None
        self._current_live_run_id: str | None = None
        self._live_spl_by_key: dict[tuple[int, str, int], float] = {}
        self._live_loudness_by_key: dict[tuple[int, str, int], float] = {}
        self._metric_channel_cache_key: tuple[int | None, int | None] | None = None
        self._metric_selected_channel_index: int | None = None
        self._metric_loudness_unit: str | None = None
        self._metric_channel_valid = False
        self._metric_config_load_failed = False

        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(8)

        top = QWidget()
        top.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        top_lay = QHBoxLayout(top)
        top_lay.setContentsMargins(0, 0, 0, 0)
        top_lay.setSpacing(8)
        top_lay.addWidget(QLabel("总结果："))

        self.lb_overall = QLabel("-")
        self.lb_overall.setObjectName("ResultBadge")
        self.lb_overall.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.lb_overall.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        self.lb_overall.setMinimumHeight(36)
        self.lb_overall.setMinimumWidth(180)
        self.lb_overall.setProperty("resultLevel", "unknown")
        top_lay.addStretch(1)
        top_lay.addWidget(self.lb_overall)
        lay.addWidget(top)

        metrics = QWidget()
        metrics_lay = QHBoxLayout(metrics)
        metrics_lay.setContentsMargins(0, 0, 0, 0)
        metrics_lay.setSpacing(8)

        spl_metric = self._create_metric_badge("声压级", "--")
        self.lb_spl_metric_label = spl_metric["label"]
        self.lb_spl_metric_value = spl_metric["value"]
        metrics_lay.addWidget(spl_metric["widget"], 1)

        loudness_metric = self._create_metric_badge("响度", "--")
        self.lb_loudness_metric_label = loudness_metric["label"]
        self.lb_loudness_metric_value = loudness_metric["value"]
        metrics_lay.addWidget(loudness_metric["widget"], 1)
        lay.addWidget(metrics)

        self.empty_state = QWidget()
        self.empty_state.setObjectName("EmptyState")
        self.empty_state.setVisible(False)
        error_lay = QVBoxLayout(self.empty_state)
        error_lay.setContentsMargins(0, 0, 0, 0)
        error_lay.setSpacing(4)

        self.lb_empty_icon = QLabel("!")
        self.lb_empty_icon.setObjectName("EmptyIcon")
        self.lb_empty_icon.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.lb_empty_icon.setVisible(False)
        error_lay.addWidget(self.lb_empty_icon)

        self.lb_empty_text = QLabel("")
        self.lb_empty_text.setObjectName("EmptyText")
        self.lb_empty_text.setWordWrap(True)
        self.lb_empty_text.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.lb_empty_text.setVisible(False)
        error_lay.addWidget(self.lb_empty_text)
        self.error_label = self.lb_empty_text
        lay.addWidget(self.empty_state)

        self.table = QTableWidget(len(self._ROW_LABELS), 4)
        self.table.setHorizontalHeaderLabels(["项目", "偏差值", "通道", "结果"])
        self.table.verticalHeader().setVisible(False)
        self.table.setShowGrid(False)
        self.table.setSelectionMode(QTableWidget.SelectionMode.NoSelection)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.table.setAlternatingRowColors(True)
        self.table.setColumnWidth(0, 110)
        self.table.setColumnWidth(1, 110)
        self.table.setColumnWidth(2, 80)
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.table.setFixedHeight(
            self.table.horizontalHeader().height() + self.table.rowHeight(0) * len(self._ROW_LABELS) + 18
        )
        lay.addWidget(self.table)
        lay.addStretch(1)

        self._connect_bus()
        self._refresh_display()

    def _connect_bus(self) -> None:
        if hasattr(self.services.bus, "factory_state_changed"):
            self.services.bus.factory_state_changed.connect(self._on_factory_state_changed)
        if hasattr(self.services.bus, "factory_review_changed"):
            self.services.bus.factory_review_changed.connect(self._on_factory_review_changed)
        if hasattr(self.services.bus, "factory_display_reset"):
            self.services.bus.factory_display_reset.connect(self._on_factory_display_reset)
        if hasattr(self.services.bus, "spl_updated"):
            self.services.bus.spl_updated.connect(self._on_spl_updated)
        if hasattr(self.services.bus, "loudness_updated"):
            self.services.bus.loudness_updated.connect(self._on_loudness_updated)
        if hasattr(self.services.bus, "factory_runtime_start_accepted"):
            self.services.bus.factory_runtime_start_accepted.connect(
                self._on_factory_runtime_start_accepted
            )
        if hasattr(self.services.bus, "factory_runtime_analyzing_entered"):
            self.services.bus.factory_runtime_analyzing_entered.connect(
                self._on_factory_runtime_analyzing_entered
            )

    @staticmethod
    def _create_metric_badge(label_text: str, value_text: str) -> dict[str, QWidget | QLabel]:
        badge = QWidget()
        badge.setObjectName("ResultMetricBadge")
        badge.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        badge.setMinimumHeight(58)

        lay = QVBoxLayout(badge)
        lay.setContentsMargins(12, 8, 12, 8)
        lay.setSpacing(2)

        label = QLabel(label_text)
        label.setObjectName("ResultMetricLabel")
        label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lay.addWidget(label)

        value = QLabel(value_text)
        value.setObjectName("ResultMetricValue")
        value.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lay.addWidget(value)

        return {"widget": badge, "label": label, "value": value}

    def _set_cell(self, row: int, column: int, text: str) -> None:
        item = QTableWidgetItem(text)
        item.setFlags(Qt.ItemFlag.ItemIsEnabled)
        if column == 0:
            item.setTextAlignment(Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft)
        else:
            item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
        self.table.setItem(row, column, item)

    def _set_row(self, row: int, label: str, deviation: str, channel: str, status: str) -> None:
        self._set_cell(row, 0, label)
        self._set_cell(row, 1, deviation)
        self._set_cell(row, 2, channel)
        self._set_cell(row, 3, status)

    def _set_badge(self, text: str, level: str) -> None:
        self.lb_overall.setText(text)
        self.lb_overall.setProperty("resultLevel", level)
        self.lb_overall.style().unpolish(self.lb_overall)
        self.lb_overall.style().polish(self.lb_overall)

    def _set_error_message(self, message: str | None) -> None:
        visible = bool(message)
        self.empty_state.setVisible(visible)
        self.lb_empty_icon.setVisible(visible)
        self.lb_empty_text.setVisible(visible)
        self.lb_empty_text.setText(message or "")

    def _set_placeholder_rows(self) -> None:
        for row, label in enumerate(self._ROW_LABELS):
            self._set_row(row, label, "-", "-", "-")

    def _show_empty(self) -> None:
        self._set_badge("-", "unknown")
        self._set_error_message(None)
        self._set_placeholder_rows()
        self._refresh_metric_display()

    def _show_in_progress(self) -> None:
        self._set_badge("分析中", "unknown")
        self._set_error_message(None)
        self._set_placeholder_rows()
        self._refresh_metric_display()

    def _render_summary(self, summary_payload: dict) -> None:
        overall_status = summary_payload.get("overall_status")
        overall_label = self._text_or_default(summary_payload.get("overall_label"), "-")
        badge_level = {
            "pass": "pass",
            "fail": "fail",
            "no_result": "fail",
        }.get(overall_status, "unknown")
        self._set_badge(overall_label, badge_level)
        error_message = None
        if overall_status == "no_result":
            error_message = self._text_or_default(summary_payload.get("error_message"), None)
        self._set_error_message(error_message)

        rows = summary_payload.get("rows")
        if not isinstance(rows, list):
            rows = []
        for row_index, default_label in enumerate(self._ROW_LABELS):
            row_payload = rows[row_index] if row_index < len(rows) and isinstance(rows[row_index], dict) else {}
            self._set_row(
                row_index,
                self._display_label(row_payload.get("label"), default_label),
                self._text_or_default(row_payload.get("deviation_display"), "-"),
                self._display_channel(row_payload.get("channel_label"), "-"),
                self._text_or_default(row_payload.get("status"), "-"),
            )
        self._refresh_metric_display()

    def _refresh_display(self) -> None:
        summary_payload = getattr(self._review_payload, "summary_payload", None)
        if isinstance(summary_payload, dict):
            self._render_summary(summary_payload)
            return
        if self._is_in_progress():
            self._show_in_progress()
            return
        self._show_empty()

    def _is_in_progress(self) -> bool:
        return (
            self._factory_state.displayed_config_id is not None
            and self._factory_state.run_phase in self._DISPLAY_PRESERVING_PHASES
            and getattr(self._review_payload, "summary_payload", None) is None
        )

    def refresh_selected_display_channel(self) -> None:
        previous_channel = self._metric_selected_channel_index
        previous_cache_known = self._metric_channel_cache_key is not None
        self._refresh_metric_channel_cache(force=True)
        if (
            previous_cache_known
            and getattr(self._factory_state, "run_phase", None) in self._LIVE_METRIC_PHASES
            and self._metric_selected_channel_index != previous_channel
        ):
            self._clear_current_run_live_metric_values()
        self._refresh_metric_display()

    def _on_factory_state_changed(self, state) -> None:
        previous_config_id = self._displayed_config_id()
        if isinstance(state, FactoryControllerState):
            self._factory_state = state
        else:
            self._factory_state = FactoryControllerState()
        if self._displayed_config_id() != previous_config_id:
            self._clear_live_run()
            self._clear_metric_channel_cache()
        self._refresh_display()

    def _on_factory_review_changed(self, review) -> None:
        if isinstance(review, FactoryReviewPayload):
            self._review_payload = review
        else:
            self._review_payload = FactoryReviewPayload()
        self._refresh_display()

    def _on_factory_display_reset(self, _reason: str) -> None:
        self._review_payload = FactoryReviewPayload()
        self._clear_live_run()
        self._clear_metric_channel_cache()
        self._show_empty()

    def _on_factory_runtime_start_accepted(self, payload) -> None:
        if not self._payload_config_matches_display(payload):
            return
        run_id = self._payload_run_id(payload)
        if run_id is None:
            return
        self._current_live_run_id = run_id
        self._clear_live_metric_values()
        self._refresh_metric_display()

    def _on_factory_runtime_analyzing_entered(self, payload) -> None:
        if not self._payload_config_matches_display(payload):
            return
        run_id = self._payload_run_id(payload)
        if run_id is None:
            return
        if self._current_live_run_id is None:
            self._current_live_run_id = run_id
        elif self._current_live_run_id != run_id:
            self._current_live_run_id = run_id
            self._clear_live_metric_values()
        self._refresh_metric_display()

    def _on_spl_updated(self, config_id, run_id, channel_idx, frame) -> None:
        if not self._live_frame_matches(config_id, run_id, channel_idx):
            return
        value = getattr(frame, "spl_db", None)
        if not self._is_finite_number(value):
            return
        key = self._current_metric_key()
        if key is None:
            return
        self._live_spl_by_key[key] = float(value)
        self._refresh_metric_display()

    def _on_loudness_updated(self, config_id, run_id, channel_idx, frame) -> None:
        if not self._live_frame_matches(config_id, run_id, channel_idx):
            return
        value = getattr(frame, "loudness_sone", None)
        if not self._is_finite_number(value):
            return
        key = self._current_metric_key()
        if key is None:
            return
        self._live_loudness_by_key[key] = float(value)
        self._refresh_metric_display()

    def _refresh_metric_display(self) -> None:
        phase = getattr(self._factory_state, "run_phase", None)
        if phase in self._AVERAGE_METRIC_PHASES and isinstance(
            getattr(self._review_payload, "judgment_payload", None),
            dict,
        ):
            self._render_average_metrics()
            return
        if phase in self._LIVE_METRIC_PHASES:
            self._render_live_metrics()
            return
        self._set_metric_values("声压级", "--", "响度", "--")

    def _render_live_metrics(self) -> None:
        key = self._current_metric_key()
        unit = self._metric_loudness_unit
        spl_text = "--"
        loudness_text = "--"
        if key is not None and unit is not None:
            spl_text = self._format_spl_db(self._live_spl_by_key.get(key))
            loudness_text = self._format_loudness(
                self._live_loudness_by_key.get(key),
                unit,
            )
        self._set_metric_values("声压级", spl_text, "响度", loudness_text)

    def _render_average_metrics(self) -> None:
        channel_result = self._selected_channel_result()
        unit = self._metric_loudness_unit
        spl_text = "--"
        loudness_text = "--"
        if channel_result is not None and unit is not None:
            spl_text = self._format_spl_db(channel_result.get("spl_average_db"))
            loudness_text = self._format_loudness(
                channel_result.get("loudness_average_sone"),
                unit,
            )
        self._set_metric_values("平均声压级", spl_text, "平均响度", loudness_text)

    def _set_metric_values(
        self,
        spl_label: str,
        spl_value: str,
        loudness_label: str,
        loudness_value: str,
    ) -> None:
        self.lb_spl_metric_label.setText(spl_label)
        self.lb_spl_metric_value.setText(spl_value)
        self.lb_loudness_metric_label.setText(loudness_label)
        self.lb_loudness_metric_value.setText(loudness_value)

    def _selected_channel_result(self) -> dict | None:
        if self._review_payload.displayed_config_id != self._displayed_config_id():
            return None
        if not self._ensure_metric_channel_cache():
            return None
        selected_channel = self._metric_selected_channel_index
        if selected_channel is None:
            return None
        judgment_payload = getattr(self._review_payload, "judgment_payload", None)
        if not isinstance(judgment_payload, dict):
            return None
        channel_results = judgment_payload.get("channel_results")
        if not isinstance(channel_results, list):
            return None
        for channel_result in channel_results:
            if not isinstance(channel_result, dict):
                continue
            if self._as_int(channel_result.get("channel_index")) == selected_channel:
                return channel_result
        return None

    def _current_metric_key(self) -> tuple[int, str, int] | None:
        config_id = self._displayed_config_id()
        run_id = self._current_live_run_id
        if not self._ensure_metric_channel_cache():
            return None
        channel_index = self._metric_selected_channel_index
        if config_id is None or run_id is None or channel_index is None:
            return None
        return (config_id, run_id, channel_index)

    def _live_frame_matches(self, config_id, run_id, channel_idx) -> bool:
        if getattr(self._factory_state, "run_phase", None) not in self._LIVE_METRIC_PHASES:
            return False
        if self._current_live_run_id is None or run_id != self._current_live_run_id:
            return False
        if self._as_int(config_id) != self._displayed_config_id():
            return False
        selected_channel = self._selected_channel_index()
        if selected_channel is None or self._as_int(channel_idx) != selected_channel:
            return False
        return self._ensure_metric_channel_cache()

    def _payload_config_matches_display(self, payload) -> bool:
        payload_config_id = self._as_int(getattr(payload, "config_id", None))
        if payload_config_id is None:
            return False
        return payload_config_id in {
            self._displayed_config_id(),
            self._as_int(getattr(self._factory_state, "active_config_id", None)),
        }

    def _displayed_config_id(self) -> int | None:
        return self._as_int(getattr(self._factory_state, "displayed_config_id", None))

    def _selected_channel_index(self) -> int | None:
        provider = self.selected_display_channel_provider
        if not callable(provider):
            return None
        return self._as_int(provider())

    def _ensure_metric_channel_cache(self) -> bool:
        key = (self._displayed_config_id(), self._selected_channel_index())
        if key != self._metric_channel_cache_key:
            self._refresh_metric_channel_cache(force=True)
        return self._metric_channel_valid

    def _refresh_metric_channel_cache(self, *, force: bool = False) -> None:
        config_id = self._displayed_config_id()
        selected_channel = self._selected_channel_index()
        key = (config_id, selected_channel)
        if not force and key == self._metric_channel_cache_key:
            return

        self._metric_channel_cache_key = key
        self._metric_selected_channel_index = selected_channel
        self._metric_loudness_unit = None
        self._metric_channel_valid = False
        self._metric_config_load_failed = False

        if config_id is None or selected_channel is None:
            return

        config = self._load_config(config_id)
        if config is None:
            self._metric_config_load_failed = True
            return
        selected_channels = config.get("selected_channels") if isinstance(config, dict) else None
        if not isinstance(selected_channels, list):
            return
        selected_channel_indexes = {
            channel_index
            for channel_index in (self._as_int(channel) for channel in selected_channels)
            if channel_index is not None
        }
        if selected_channel not in selected_channel_indexes:
            return
        channels = config.get("channels") if isinstance(config, dict) else None
        if isinstance(channels, list):
            for channel in channels:
                if not isinstance(channel, dict):
                    continue
                if self._as_int(channel.get("channel_index")) == selected_channel:
                    self._metric_loudness_unit = self._normalize_loudness_unit(
                        channel.get("loudness_unit")
                    )
                    self._metric_channel_valid = True
                    return

    @staticmethod
    def _normalize_loudness_unit(value) -> str:
        unit = str(value or "sone").strip().lower()
        return unit if unit in {"sone", "phon"} else "sone"

    def _load_config(self, config_id: int | None) -> dict | None:
        if config_id is None:
            return None
        config_service = getattr(self.services, "config_service", None)
        load_config = getattr(config_service, "load_config", None)
        if not callable(load_config):
            return None
        try:
            config = load_config(config_id)
        except Exception:
            return None
        return config if isinstance(config, dict) else None

    def _clear_live_run(self) -> None:
        self._current_live_run_id = None
        self._clear_live_metric_values()

    def _clear_live_metric_values(self) -> None:
        self._live_spl_by_key.clear()
        self._live_loudness_by_key.clear()

    def _clear_current_run_live_metric_values(self) -> None:
        config_id = self._displayed_config_id()
        run_id = self._current_live_run_id
        if config_id is None or run_id is None:
            return
        for cache in (self._live_spl_by_key, self._live_loudness_by_key):
            for key in list(cache):
                if key[0] == config_id and key[1] == run_id:
                    del cache[key]

    def _clear_metric_channel_cache(self) -> None:
        self._metric_channel_cache_key = None
        self._metric_selected_channel_index = None
        self._metric_loudness_unit = None
        self._metric_channel_valid = False
        self._metric_config_load_failed = False

    @staticmethod
    def _payload_run_id(payload) -> str | None:
        run_id = getattr(payload, "run_id", None)
        if run_id is None:
            return None
        text = str(run_id)
        return text if text else None

    @staticmethod
    def _as_int(value) -> int | None:
        if isinstance(value, bool):
            return None
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _is_finite_number(value) -> bool:
        try:
            return math.isfinite(float(value))
        except (TypeError, ValueError):
            return False

    @staticmethod
    def _format_spl_db(value) -> str:
        if not ResultModule._is_finite_number(value):
            return "--"
        return f"{float(value):.1f} dB"

    @staticmethod
    def _format_loudness(value_sone, unit: str) -> str:
        if not ResultModule._is_finite_number(value_sone):
            return "--"
        value = float(value_sone)
        normalized_unit = str(unit or "sone").strip().lower()
        if normalized_unit == "phon":
            try:
                phon_value = float(dsp.sone_to_phon(value))
            except (TypeError, ValueError, OverflowError):
                return "--"
            if not math.isfinite(phon_value):
                return "--"
            return f"{phon_value:.1f} phon"
        if value < 10.0:
            return f"{value:.2f} sone"
        return f"{value:.1f} sone"

    @staticmethod
    def _text_or_default(value, default: str | None) -> str | None:
        if value is None:
            return default
        text = str(value)
        if text == "":
            return default
        return text

    @staticmethod
    def _display_label(value, default: str | None) -> str | None:
        text = ResultModule._text_or_default(value, default)
        if text is None:
            return None
        return _DISPLAY_LABEL_MAP.get(text, text)

    @staticmethod
    def _display_channel(value, default: str | None) -> str | None:
        text = ResultModule._text_or_default(value, default)
        if text is None:
            return None
        match = _CHANNEL_LABEL_RE.fullmatch(text.strip())
        if match:
            return f"通道{match.group(1)}"
        return text
