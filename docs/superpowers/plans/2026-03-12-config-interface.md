# Configuration Interface Implementation Plan

> **For agentic workers:** REQUIRED: Use superpowers:subagent-driven-development (if subagents available) or superpowers:executing-plans to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a multi-profile test configuration CRUD interface in the "配置" tab, with database persistence and per-channel analysis parameter editing.

**Architecture:** Left-right split page (config list + detail form). Data stored in two new SQLite tables via Peewee ORM. ConfigService handles all CRUD/validation logic. UI module communicates through existing EventBus pattern.

**Tech Stack:** PyQt6, Peewee ORM, SQLite, Python 3

**Spec:** `docs/superpowers/specs/2026-03-12-config-interface-design.md`

**Notes:**
- DB-level constraints (e.g. `record_duration > 0`) are enforced in `ConfigService.validate()`, not in Peewee model definitions. This matches the existing project pattern where `device_table.py` uses no DB-level checks beyond PK constraints.
- Band-limit tables use plain editable `QTableWidgetItem` cells (not `QDoubleSpinBox` delegates) to match the existing `QTableWidget` pattern in `hardware.py`. Invalid text is caught during `_read_band_table()` parsing.
- The plan assumes hardware may or may not be saved. If no hardware exists, channel tabs show a warning but the app still functions.

---

## Chunk 1: Data Model + DB Bootstrap

### Task 1: Create TestConfigTable model

**Files:**
- Create: `model/test_config_table.py`

- [ ] **Step 1: Create TestConfigTable model**

Create `model/test_config_table.py`:

```python
from peewee import AutoField, BooleanField, FloatField, TextField

from consts.time_consts import now_str
from model.base_mode import BaseModel


class TestConfigTable(BaseModel):
    config_id = AutoField()
    config_name = TextField(unique=True)
    record_duration = FloatField(default=5.0)
    analysis_duration = FloatField(default=3.0)
    scan_triggers_test = BooleanField(default=False)
    created_time = TextField(default=now_str)
    updated_time = TextField(default=now_str)

    class Meta:
        table_name = "test_config_table"
```

- [ ] **Step 2: Verify model imports cleanly**

Run: `cd D:/Work/jingchengpython/JingChengMechanical && python -c "from model.test_config_table import TestConfigTable; print('OK:', TestConfigTable._meta.table_name)"`

Expected: `OK: test_config_table`

- [ ] **Step 3: Commit**

```bash
git add model/test_config_table.py
git commit -m "feat: add TestConfigTable model"
```

---

### Task 2: Create ChannelConfigTable model

**Files:**
- Create: `model/channel_config_table.py`

- [ ] **Step 1: Create ChannelConfigTable model**

Create `model/channel_config_table.py`:

```python
from peewee import AutoField, FloatField, ForeignKeyField, IntegerField, TextField

from model.base_mode import BaseModel
from model.test_config_table import TestConfigTable


class ChannelConfigTable(BaseModel):
    channel_config_id = AutoField()
    config = ForeignKeyField(
        TestConfigTable,
        backref="channels",
        on_delete="CASCADE",
    )
    channel_index = IntegerField()
    stft_nfft = IntegerField(default=1024)
    stft_overlap = FloatField(default=0.5)
    spl_weighting = TextField(default="A")
    spl_time_constant = FloatField(default=0.125)
    spl_upper_limit = FloatField(default=85.0)
    spl_lower_limit = FloatField(default=40.0)
    fft_band_limits = TextField(default="[]")
    oct_weighting = TextField(default="A")
    oct_band_limits = TextField(default="[]")
    ai_config = TextField(default="{}")

    class Meta:
        table_name = "channel_config_table"
        indexes = ((("config", "channel_index"), True),)
```

- [ ] **Step 2: Verify model imports cleanly**

Run: `cd D:/Work/jingchengpython/JingChengMechanical && python -c "from model.channel_config_table import ChannelConfigTable; print('OK:', ChannelConfigTable._meta.table_name)"`

Expected: `OK: channel_config_table`

- [ ] **Step 3: Commit**

```bash
git add model/channel_config_table.py
git commit -m "feat: add ChannelConfigTable model"
```

---

### Task 3: Add table creation to DB bootstrap

**Files:**
- Modify: `app/service/db_connection_service.py`

- [ ] **Step 1: Add create_tables call after connect**

In `app/service/db_connection_service.py`, modify the `connect` method:

```python
from __future__ import annotations

from model.base_mode import db
from model.test_config_table import TestConfigTable
from model.channel_config_table import ChannelConfigTable


class DbConnectionService:
    """
    数据库连接服务（长连接）。

    目标：
    - 程序启动时 connect 一次（或首次使用时自动 connect）
    - 程序退出时 close 一次
    - 业务代码/CRUD 不需要每次手动 connect/close
    """

    def connect(self):
        db.connect(reuse_if_open=True)
        db.create_tables([TestConfigTable, ChannelConfigTable], safe=True)

    def close(self):
        if not db.is_closed():
            db.close()

    def is_connected(self) -> bool:
        return not db.is_closed()
```

- [ ] **Step 2: Verify app starts and tables are created**

Run: `cd D:/Work/jingchengpython/JingChengMechanical && python -c "from app.service.db_connection_service import DbConnectionService; s = DbConnectionService(); s.connect(); print('Tables created OK'); s.close()"`

Expected: `Tables created OK`

Then verify tables exist:

Run: `cd D:/Work/jingchengpython/JingChengMechanical && python -c "from model.base_mode import db; db.connect(); cursor = db.execute_sql('SELECT name FROM sqlite_master WHERE type=\"table\" AND name LIKE \"%config%\"'); print([r[0] for r in cursor.fetchall()]); db.close()"`

Expected: `['test_config_table', 'channel_config_table']`

- [ ] **Step 3: Commit**

```bash
git add app/service/db_connection_service.py
git commit -m "feat: auto-create config tables on DB connect"
```

---

## Chunk 2: Service Layer

### Task 4: Implement ConfigService

**Files:**
- Create: `app/service/config_service.py`

- [ ] **Step 1: Create ConfigService with all methods**

Create `app/service/config_service.py`:

```python
from __future__ import annotations

import json

from peewee import IntegrityError

from model.base_mode import db
from model.test_config_table import TestConfigTable
from model.channel_config_table import ChannelConfigTable
from model.device_table import DeviceTable
from consts.time_consts import now_str

# Default values for new channel configs
_CHANNEL_DEFAULTS = {
    "stft_nfft": 1024,
    "stft_overlap": 0.5,
    "spl_weighting": "A",
    "spl_time_constant": 0.125,
    "spl_lower_limit": 40.0,
    "spl_upper_limit": 85.0,
    "fft_band_limits": "[]",
    "oct_weighting": "A",
    "oct_band_limits": "[]",
    "ai_config": "{}",
}

_VALID_NFFT = [256, 512, 1024, 2048, 4096, 8192]
_VALID_OVERLAP = [0.25, 0.5, 0.75]
_VALID_WEIGHTING = ["A", "C", "Z"]


class ConfigService:
    def __init__(self, bus):
        self.bus = bus

    def list_configs(self) -> list[dict]:
        rows = (
            TestConfigTable.select(
                TestConfigTable.config_id, TestConfigTable.config_name
            )
            .order_by(TestConfigTable.config_id)
            .dicts()
        )
        return list(rows)

    def load_config(self, config_id: int) -> dict:
        cfg = TestConfigTable.get_by_id(config_id)
        result = {
            "config_id": cfg.config_id,
            "config_name": cfg.config_name,
            "record_duration": cfg.record_duration,
            "analysis_duration": cfg.analysis_duration,
            "scan_triggers_test": bool(cfg.scan_triggers_test),
            "channels": [],
        }
        ch_rows = (
            ChannelConfigTable.select()
            .where(ChannelConfigTable.config == config_id)
            .order_by(ChannelConfigTable.channel_index)
        )
        for ch in ch_rows:
            result["channels"].append(
                {
                    "channel_index": ch.channel_index,
                    "stft_nfft": ch.stft_nfft,
                    "stft_overlap": ch.stft_overlap,
                    "spl_weighting": ch.spl_weighting,
                    "spl_time_constant": ch.spl_time_constant,
                    "spl_upper_limit": ch.spl_upper_limit,
                    "spl_lower_limit": ch.spl_lower_limit,
                    "fft_band_limits": self._parse_json(ch.fft_band_limits, []),
                    "oct_weighting": ch.oct_weighting,
                    "oct_band_limits": self._parse_json(ch.oct_band_limits, []),
                    "ai_config": self._parse_json(ch.ai_config, {}),
                }
            )
        return result

    def create_config(self, config_name: str) -> int:
        name = config_name.strip()
        if not name:
            raise ValueError("配置名称不能为空")
        if TestConfigTable.select().where(TestConfigTable.config_name == name).exists():
            raise ValueError(f"配置名称「{name}」已存在")
        try:
            with db.atomic():
                cfg = TestConfigTable.create(
                    config_name=name,
                    created_time=now_str(),
                    updated_time=now_str(),
                )
                ch_count = self.get_channel_count()
                for i in range(ch_count):
                    ChannelConfigTable.create(
                        config=cfg, channel_index=i, **_CHANNEL_DEFAULTS
                    )
        except IntegrityError:
            raise ValueError(f"配置名称「{name}」已存在")
        self.bus.config_changed.emit()
        return cfg.config_id

    def save_config(self, data: dict) -> int:
        errors = self.validate(data)
        if errors:
            raise ValueError("\n".join(errors))

        config_id = data["config_id"]
        if not TestConfigTable.select().where(TestConfigTable.config_id == config_id).exists():
            raise ValueError("配置不存在或已被删除")
        channels = data.get("channels", [])

        try:
            with db.atomic():
                TestConfigTable.update(
                    config_name=data["config_name"].strip(),
                    record_duration=data["record_duration"],
                    analysis_duration=data["analysis_duration"],
                    scan_triggers_test=data["scan_triggers_test"],
                    updated_time=now_str(),
                ).where(TestConfigTable.config_id == config_id).execute()

                if channels:
                    ChannelConfigTable.delete().where(
                        ChannelConfigTable.config == config_id
                    ).execute()
                    for ch in channels:
                        ChannelConfigTable.create(
                            config=config_id,
                            channel_index=ch["channel_index"],
                            stft_nfft=ch["stft_nfft"],
                            stft_overlap=ch["stft_overlap"],
                            spl_weighting=ch["spl_weighting"],
                            spl_time_constant=ch["spl_time_constant"],
                            spl_upper_limit=ch["spl_upper_limit"],
                            spl_lower_limit=ch["spl_lower_limit"],
                            fft_band_limits=json.dumps(ch["fft_band_limits"]),
                            oct_weighting=ch["oct_weighting"],
                            oct_band_limits=json.dumps(ch["oct_band_limits"]),
                            ai_config=json.dumps(ch.get("ai_config", {})),
                        )
        except IntegrityError:
            raise ValueError(f"配置名称「{data['config_name']}」已存在")

        self.bus.config_changed.emit()
        return config_id

    def delete_config(self, config_id: int) -> bool:
        with db.atomic():
            count = (
                TestConfigTable.delete()
                .where(TestConfigTable.config_id == config_id)
                .execute()
            )
        if count > 0:
            self.bus.config_changed.emit()
            return True
        return False

    def get_channel_count(self) -> int:
        try:
            row = DeviceTable.get_by_id(1)
            return int(row.input_channel_count)
        except Exception:
            return 0

    def validate(self, data: dict) -> list[str]:
        errors = []
        name = data.get("config_name", "").strip()
        if not name:
            errors.append("配置名称不能为空")
        else:
            q = TestConfigTable.select().where(TestConfigTable.config_name == name)
            config_id = data.get("config_id")
            if config_id is not None:
                q = q.where(TestConfigTable.config_id != config_id)
            if q.exists():
                errors.append(f"配置名称「{name}」已存在")

        rd = data.get("record_duration", 0)
        ad = data.get("analysis_duration", 0)
        if not isinstance(rd, (int, float)) or rd <= 0:
            errors.append("录音时长必须大于 0")
        if not isinstance(ad, (int, float)) or ad <= 0:
            errors.append("分析时长必须大于 0")
        elif isinstance(rd, (int, float)) and rd > 0 and ad > rd:
            errors.append("分析时长不能超过录音时长")

        if not isinstance(data.get("scan_triggers_test"), bool):
            errors.append("扫码触发必须为布尔值")

        channels = data.get("channels", [])
        if not isinstance(channels, list):
            errors.append("通道数据格式无效")
            return errors

        seen_idx = set()
        ch_count = self.get_channel_count()
        for i, ch in enumerate(channels):
            if not isinstance(ch, dict):
                errors.append(f"通道 {i + 1}: 数据格式无效")
                continue
            idx = ch.get("channel_index")
            if not isinstance(idx, int):
                errors.append(f"通道 {i + 1}: 通道序号无效")
                continue
            prefix = f"通道 {idx + 1}"
            if idx < 0 or (ch_count > 0 and idx >= ch_count):
                errors.append(f"{prefix}: 通道序号无效")
            if idx in seen_idx:
                errors.append(f"{prefix}: 通道序号重复")
            seen_idx.add(idx)

            if ch.get("stft_nfft") not in _VALID_NFFT:
                errors.append(f"{prefix}: STFT FFT 点数无效")
            if ch.get("stft_overlap") not in _VALID_OVERLAP:
                errors.append(f"{prefix}: STFT 重叠率无效")
            if ch.get("spl_weighting") not in _VALID_WEIGHTING:
                errors.append(f"{prefix}: SPL 计权模式无效")
            tc = ch.get("spl_time_constant", 0)
            if not isinstance(tc, (int, float)) or isinstance(tc, bool) or tc <= 0:
                errors.append(f"{prefix}: SPL 时间常数必须大于 0")
            sl = ch.get("spl_lower_limit", 0)
            su = ch.get("spl_upper_limit", 0)
            if not isinstance(sl, (int, float)) or isinstance(sl, bool):
                errors.append(f"{prefix}: SPL 下限必须为数值")
            elif not isinstance(su, (int, float)) or isinstance(su, bool):
                errors.append(f"{prefix}: SPL 上限必须为数值")
            elif sl >= su:
                errors.append(f"{prefix}: SPL 下限必须小于上限")
            if ch.get("oct_weighting") not in _VALID_WEIGHTING:
                errors.append(f"{prefix}: 倍频程计权模式无效")

            for label, key in [("FFT", "fft_band_limits"), ("倍频程", "oct_band_limits")]:
                bands = ch.get(key, [])
                if not isinstance(bands, list):
                    errors.append(f"{prefix}: {label} 频带数据格式无效")
                    continue
                for j, b in enumerate(bands):
                    if not isinstance(b, dict):
                        errors.append(f"{prefix}: {label} 第 {j+1} 行格式无效")
                        continue
                    freq = b.get("freq", 0)
                    lo = b.get("lower", 0)
                    hi = b.get("upper", 0)
                    if not isinstance(freq, (int, float)) or isinstance(freq, bool) or freq <= 0:
                        errors.append(f"{prefix}: {label} 第 {j+1} 行频率必须为正数")
                    if not isinstance(lo, (int, float)) or isinstance(lo, bool):
                        errors.append(f"{prefix}: {label} 第 {j+1} 行下限必须为数值")
                    elif not isinstance(hi, (int, float)) or isinstance(hi, bool):
                        errors.append(f"{prefix}: {label} 第 {j+1} 行上限必须为数值")
                    elif lo >= hi:
                        errors.append(
                            f"{prefix}: {label} 第 {j+1} 行下限必须小于上限"
                        )
        return errors

    @staticmethod
    def _parse_json(text: str, default):
        try:
            return json.loads(text) if text else default
        except (json.JSONDecodeError, TypeError):
            return default
```

- [ ] **Step 2: Verify service works with a quick smoke test**

Run:
```bash
cd D:/Work/jingchengpython/JingChengMechanical && python -c "
from app.service.db_connection_service import DbConnectionService
from PyQt6.QtCore import QObject, pyqtSignal

class FakeBus(QObject):
    config_changed = pyqtSignal()

from PyQt6.QtWidgets import QApplication
import sys
app = QApplication(sys.argv)

dbs = DbConnectionService()
dbs.connect()

from app.service.config_service import ConfigService
bus = FakeBus()
svc = ConfigService(bus)

# create
cid = svc.create_config('test_smoke')
print(f'Created config_id={cid}')

# list
configs = svc.list_configs()
print(f'Configs: {configs}')

# load
data = svc.load_config(cid)
print(f'Loaded: name={data[\"config_name\"]}, channels={len(data[\"channels\"])}')

# delete
ok = svc.delete_config(cid)
print(f'Deleted: {ok}')
print(f'Remaining: {svc.list_configs()}')

dbs.close()
print('All OK')
"
```

Expected: Prints created ID, config list, loaded data, deletion result, and "All OK".

- [ ] **Step 3: Commit**

```bash
git add app/service/config_service.py
git commit -m "feat: add ConfigService with CRUD and validation"
```

---

### Task 5: Register ConfigService in ServiceContainer and add EventBus signal

**Files:**
- Modify: `app/services_container.py`

- [ ] **Step 1: Add config_changed signal to EventBus and register ConfigService**

In `app/services_container.py`:

Add import at top:
```python
from app.service.config_service import ConfigService
```

Add signal to `EventBus` class, after `refresh_channels`:
```python
    # 配置变化（ConfigModule 监听该信号刷新配置列表）
    config_changed = pyqtSignal()
```

Add to `ServiceContainer.__post_init__`, after the input_calibration_service line:
```python
        # 配置服务
        self.config_service = ConfigService(self.bus)
```

- [ ] **Step 2: Verify app still starts**

Run: `cd D:/Work/jingchengpython/JingChengMechanical && python -c "from app.services_container import ServiceContainer; sc = ServiceContainer(); print('bus signals:', [s for s in dir(sc.bus) if not s.startswith('_')]); sc.db.close(); print('OK')"`

Expected: Output includes `config_changed` in the signal list and ends with `OK`.

- [ ] **Step 3: Commit**

```bash
git add app/services_container.py
git commit -m "feat: register ConfigService and add config_changed signal"
```

---

## Chunk 3: UI Widgets

### Task 6: Create CollapsibleGroupBox widget

**Files:**
- Create: `ui/widgets/collapsible_group_box.py`

- [ ] **Step 1: Create the CollapsibleGroupBox widget**

Create `ui/widgets/collapsible_group_box.py`:

```python
from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QLabel, QVBoxLayout, QWidget


class CollapsibleGroupBox(QWidget):
    """
    可折叠的分组容器。
    点击标题行切换内容区域的显示/隐藏。
    """

    def __init__(self, title: str, parent=None):
        super().__init__(parent)
        self._expanded = True

        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(4)

        self._header = QLabel(f"▼ {title}")
        self._header.setStyleSheet(
            "font-weight: 700; color: #334155; font-size: 12px; "
            "padding: 6px 4px; cursor: pointer;"
        )
        self._header.setCursor(Qt.CursorShape.PointingHandCursor)
        self._header.mousePressEvent = self._toggle
        lay.addWidget(self._header)

        self._content = QWidget()
        self._content_layout = QVBoxLayout(self._content)
        self._content_layout.setContentsMargins(4, 0, 0, 0)
        self._content_layout.setSpacing(6)
        lay.addWidget(self._content)

        self._title = title

    def content_layout(self) -> QVBoxLayout:
        return self._content_layout

    def add_widget(self, widget: QWidget):
        self._content_layout.addWidget(widget)

    def _toggle(self, _event=None):
        self._expanded = not self._expanded
        self._content.setVisible(self._expanded)
        arrow = "▼" if self._expanded else "►"
        self._header.setText(f"{arrow} {self._title}")
```

- [ ] **Step 2: Verify widget imports cleanly**

Run: `cd D:/Work/jingchengpython/JingChengMechanical && python -c "from ui.widgets.collapsible_group_box import CollapsibleGroupBox; print('OK')"`

Expected: `OK`

- [ ] **Step 3: Commit**

```bash
git add ui/widgets/collapsible_group_box.py
git commit -m "feat: add CollapsibleGroupBox reusable widget"
```

---

## Chunk 4: UI Module + Integration

### Task 7: Create ConfigModule UI

**Files:**
- Create: `ui/modules/config.py`

- [ ] **Step 1: Create the ConfigModule**

Create `ui/modules/config.py`. This is the largest file — it implements the full left-right config page.

```python
from __future__ import annotations

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFormLayout,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from consts.style import combo_qss
from ui.widgets.cards import Card
from ui.widgets.collapsible_group_box import CollapsibleGroupBox


class ConfigModule(QWidget):
    module_id = "config"

    def __init__(self, services):
        super().__init__()
        self.services = services
        self._current_config_id: int | None = None

        self._init_ui()
        self._connect_signals()
        self._refresh_list()

    # ── UI construction ─────────────────────────────────────

    def _init_ui(self):
        root = QHBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(14)

        # Left panel: config list
        left = QWidget()
        left.setFixedWidth(220)
        left_lay = QVBoxLayout(left)
        left_lay.setContentsMargins(0, 0, 0, 0)
        left_lay.setSpacing(8)

        btn_row = QWidget()
        btn_lay = QHBoxLayout(btn_row)
        btn_lay.setContentsMargins(0, 0, 0, 0)
        btn_lay.setSpacing(6)
        self.btn_new = QPushButton("新建")
        self.btn_new.setObjectName("Primary")
        self.btn_delete = QPushButton("删除")
        self.btn_delete.setObjectName("Danger")
        btn_lay.addWidget(self.btn_new)
        btn_lay.addWidget(self.btn_delete)
        left_lay.addWidget(btn_row)

        self.config_list = QListWidget()
        self.config_list.setSelectionMode(
            QAbstractItemView.SelectionMode.SingleSelection
        )
        self.config_list.setStyleSheet(
            "QListWidget { background: #f8fafc; border: 1px solid #e5e7eb; "
            "border-radius: 8px; padding: 4px; }"
            "QListWidget::item { padding: 8px; border-radius: 6px; "
            "border: 1px solid transparent; margin: 2px 0; }"
            "QListWidget::item:selected { background: #e0edff; "
            "border: 2px solid #3b82f6; color: #1e40af; font-weight: 600; }"
            "QListWidget::item:hover:!selected { background: #f1f5f9; }"
        )
        left_lay.addWidget(self.config_list, 1)
        root.addWidget(left)

        # Right panel: detail
        self.right_panel = QWidget()
        self.right_lay = QVBoxLayout(self.right_panel)
        self.right_lay.setContentsMargins(0, 0, 0, 0)
        self.right_lay.setSpacing(10)

        # Empty state
        self.empty_label = QLabel("暂无配置，请点击「新建」创建")
        self.empty_label.setObjectName("Muted")
        self.empty_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.right_lay.addWidget(self.empty_label)

        # Detail container (hidden when empty)
        self.detail = QWidget()
        detail_lay = QVBoxLayout(self.detail)
        detail_lay.setContentsMargins(0, 0, 0, 0)
        detail_lay.setSpacing(10)

        # Global params
        global_form = QWidget()
        gf_lay = QFormLayout(global_form)
        gf_lay.setContentsMargins(0, 0, 0, 0)
        gf_lay.setHorizontalSpacing(10)
        gf_lay.setVerticalSpacing(8)

        self.ed_name = QLineEdit()
        gf_lay.addRow("配置名称", self.ed_name)

        self.spin_record = QDoubleSpinBox()
        self.spin_record.setRange(0.1, 3600.0)
        self.spin_record.setSingleStep(0.5)
        self.spin_record.setSuffix(" 秒")
        self.spin_record.setValue(5.0)
        gf_lay.addRow("录音时长", self.spin_record)

        self.spin_analysis = QDoubleSpinBox()
        self.spin_analysis.setRange(0.1, 3600.0)
        self.spin_analysis.setSingleStep(0.5)
        self.spin_analysis.setSuffix(" 秒")
        self.spin_analysis.setValue(3.0)
        gf_lay.addRow("分析时长", self.spin_analysis)

        self.chk_scan = QCheckBox("扫码枪直接触发测试")
        gf_lay.addRow("扫码触发", self.chk_scan)

        detail_lay.addWidget(Card("全局参数", global_form))

        # Channel warning
        self.channel_warning = QLabel("请先在硬件页面选择并保存输入设备")
        self.channel_warning.setObjectName("Muted")
        self.channel_warning.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.channel_warning.setStyleSheet(
            "background: #fefce8; border: 1px solid #fde68a; "
            "border-radius: 8px; padding: 12px;"
        )
        self.channel_warning.setVisible(False)
        detail_lay.addWidget(self.channel_warning)

        # Channel count mismatch note
        self.channel_mismatch_note = QLabel(
            "当前硬件通道数与已保存配置不同，多余通道配置将在保存时移除"
        )
        self.channel_mismatch_note.setObjectName("Muted")
        self.channel_mismatch_note.setStyleSheet("color: #ca8a04; font-size: 11px;")
        self.channel_mismatch_note.setVisible(False)
        detail_lay.addWidget(self.channel_mismatch_note)

        # Channel tabs
        self.tab_channels = QTabWidget()
        detail_lay.addWidget(self.tab_channels, 1)

        # Save button
        save_row = QWidget()
        save_lay = QHBoxLayout(save_row)
        save_lay.setContentsMargins(0, 0, 0, 0)
        save_lay.addStretch(1)
        self.btn_save = QPushButton("保存配置")
        self.btn_save.setObjectName("Primary")
        save_lay.addWidget(self.btn_save)
        detail_lay.addWidget(save_row)

        self.detail.setVisible(False)
        self.right_lay.addWidget(self.detail, 1)
        root.addWidget(self.right_panel, 1)

    def _connect_signals(self):
        self.btn_new.clicked.connect(self._on_new)
        self.btn_delete.clicked.connect(self._on_delete)
        self.btn_save.clicked.connect(self._on_save)
        self.config_list.currentItemChanged.connect(self._on_item_changed)
        self.services.bus.refresh_channels.connect(self._on_hardware_changed)

    # ── Channel tab building ────────────────────────────────

    def _build_channel_tabs(self, ch_count: int, channel_data: list[dict] | None = None):
        self.tab_channels.clear()
        if ch_count <= 0:
            self.channel_warning.setVisible(True)
            self.tab_channels.setVisible(False)
            return

        self.channel_warning.setVisible(False)
        self.tab_channels.setVisible(True)

        saved_count = len(channel_data) if channel_data else 0
        self.channel_mismatch_note.setVisible(
            saved_count > 0 and saved_count > ch_count
        )

        for i in range(ch_count):
            ch = (
                channel_data[i]
                if channel_data and i < len(channel_data)
                else None
            )
            tab = self._build_channel_tab(i, ch)
            self.tab_channels.addTab(tab, f"通道 {i + 1}")

    def _build_channel_tab(self, ch_index: int, data: dict | None) -> QWidget:
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)

        container = QWidget()
        container.setProperty("channel_index", ch_index)
        lay = QVBoxLayout(container)
        lay.setContentsMargins(4, 4, 4, 4)
        lay.setSpacing(8)

        # STFT section
        stft = CollapsibleGroupBox("STFT 参数")
        stft_form = QWidget()
        sf_lay = QFormLayout(stft_form)
        sf_lay.setContentsMargins(0, 0, 0, 0)
        sf_lay.setHorizontalSpacing(10)
        sf_lay.setVerticalSpacing(6)

        combo_nfft = QComboBox()
        combo_nfft.setStyleSheet(combo_qss)
        for v in [256, 512, 1024, 2048, 4096, 8192]:
            combo_nfft.addItem(str(v), v)
        combo_nfft.setObjectName("stft_nfft")
        val = data.get("stft_nfft", 1024) if data else 1024
        idx = combo_nfft.findData(val)
        combo_nfft.setCurrentIndex(idx if idx >= 0 else 2)
        sf_lay.addRow("FFT 点数", combo_nfft)

        combo_overlap = QComboBox()
        combo_overlap.setStyleSheet(combo_qss)
        for v in [0.25, 0.5, 0.75]:
            combo_overlap.addItem(str(v), v)
        combo_overlap.setObjectName("stft_overlap")
        val = data.get("stft_overlap", 0.5) if data else 0.5
        idx = combo_overlap.findData(val)
        combo_overlap.setCurrentIndex(idx if idx >= 0 else 1)
        sf_lay.addRow("重叠率", combo_overlap)

        lb_window = QLabel("Hann（固定）")
        lb_window.setObjectName("Muted")
        sf_lay.addRow("窗函数", lb_window)

        stft.add_widget(stft_form)
        lay.addWidget(stft)

        # SPL section
        spl = CollapsibleGroupBox("SPL 参数")
        spl_form = QWidget()
        sp_lay = QFormLayout(spl_form)
        sp_lay.setContentsMargins(0, 0, 0, 0)
        sp_lay.setHorizontalSpacing(10)
        sp_lay.setVerticalSpacing(6)

        combo_spl_w = QComboBox()
        combo_spl_w.setStyleSheet(combo_qss)
        combo_spl_w.addItems(["A", "C", "Z"])
        combo_spl_w.setObjectName("spl_weighting")
        val = data.get("spl_weighting", "A") if data else "A"
        idx = combo_spl_w.findText(val)
        combo_spl_w.setCurrentIndex(idx if idx >= 0 else 0)
        sp_lay.addRow("计权模式", combo_spl_w)

        spin_tc = QDoubleSpinBox()
        spin_tc.setObjectName("spl_time_constant")
        spin_tc.setRange(0.001, 10.0)
        spin_tc.setDecimals(3)
        spin_tc.setSingleStep(0.001)
        spin_tc.setSuffix(" 秒")
        spin_tc.setValue(data.get("spl_time_constant", 0.125) if data else 0.125)
        sp_lay.addRow("时间常数", spin_tc)

        spin_lo = QDoubleSpinBox()
        spin_lo.setObjectName("spl_lower_limit")
        spin_lo.setRange(-200.0, 200.0)
        spin_lo.setSuffix(" dB")
        spin_lo.setValue(data.get("spl_lower_limit", 40.0) if data else 40.0)
        sp_lay.addRow("SPL 下限", spin_lo)

        spin_hi = QDoubleSpinBox()
        spin_hi.setObjectName("spl_upper_limit")
        spin_hi.setRange(-200.0, 200.0)
        spin_hi.setSuffix(" dB")
        spin_hi.setValue(data.get("spl_upper_limit", 85.0) if data else 85.0)
        sp_lay.addRow("SPL 上限", spin_hi)

        spl.add_widget(spl_form)
        lay.addWidget(spl)

        # FFT band limits section
        fft_section = CollapsibleGroupBox("FFT 频带上下限")
        fft_table = self._build_band_table(
            "fft_band_table",
            data.get("fft_band_limits", []) if data else [],
        )
        fft_section.add_widget(fft_table)
        lay.addWidget(fft_section)

        # Octave section
        oct_section = CollapsibleGroupBox("1/3 倍频程参数")
        oct_form = QWidget()
        of_lay = QVBoxLayout(oct_form)
        of_lay.setContentsMargins(0, 0, 0, 0)
        of_lay.setSpacing(6)

        oct_w_row = QWidget()
        ow_lay = QFormLayout(oct_w_row)
        ow_lay.setContentsMargins(0, 0, 0, 0)
        combo_oct_w = QComboBox()
        combo_oct_w.setStyleSheet(combo_qss)
        combo_oct_w.addItems(["A", "C", "Z"])
        combo_oct_w.setObjectName("oct_weighting")
        val = data.get("oct_weighting", "A") if data else "A"
        idx = combo_oct_w.findText(val)
        combo_oct_w.setCurrentIndex(idx if idx >= 0 else 0)
        ow_lay.addRow("计权模式", combo_oct_w)
        of_lay.addWidget(oct_w_row)

        oct_table = self._build_band_table(
            "oct_band_table",
            data.get("oct_band_limits", []) if data else [],
        )
        of_lay.addWidget(oct_table)
        oct_section.add_widget(oct_form)
        lay.addWidget(oct_section)

        # AI placeholder
        ai_section = CollapsibleGroupBox("AI 分析")
        ai_label = QLabel("待配置，后续扩展")
        ai_label.setObjectName("Muted")
        ai_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        ai_label.setStyleSheet("padding: 16px;")
        ai_section.add_widget(ai_label)
        lay.addWidget(ai_section)

        lay.addStretch(1)
        scroll.setWidget(container)
        return scroll

    def _build_band_table(self, object_name: str, bands: list[dict]) -> QWidget:
        wrapper = QWidget()
        w_lay = QVBoxLayout(wrapper)
        w_lay.setContentsMargins(0, 0, 0, 0)
        w_lay.setSpacing(4)

        table = QTableWidget()
        table.setObjectName(object_name)
        table.setColumnCount(3)
        table.setHorizontalHeaderLabels(["频率 (Hz)", "下限 (dB)", "上限 (dB)"])
        table.verticalHeader().setVisible(False)
        table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        table.setMinimumHeight(120)

        for band in bands:
            self._add_band_row(table, band.get("freq", 1000), band.get("lower", 0), band.get("upper", 100))

        w_lay.addWidget(table)

        btn_row = QWidget()
        br_lay = QHBoxLayout(btn_row)
        br_lay.setContentsMargins(0, 0, 0, 0)
        br_lay.setSpacing(6)
        btn_add = QPushButton("添加频带")
        btn_del = QPushButton("删除选中")
        btn_del.setObjectName("Danger")
        br_lay.addWidget(btn_add)
        br_lay.addWidget(btn_del)
        br_lay.addStretch(1)

        btn_add.clicked.connect(lambda: self._add_band_row(table, 1000, 0, 100))
        btn_del.clicked.connect(lambda: self._delete_selected_rows(table))

        w_lay.addWidget(btn_row)
        return wrapper

    @staticmethod
    def _add_band_row(table: QTableWidget, freq: float, lower: float, upper: float):
        row = table.rowCount()
        table.insertRow(row)
        for col, val in enumerate([freq, lower, upper]):
            item = QTableWidgetItem(str(val))
            item.setTextAlignment(
                Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignCenter
            )
            table.setItem(row, col, item)

    @staticmethod
    def _delete_selected_rows(table: QTableWidget):
        rows = sorted(set(idx.row() for idx in table.selectedIndexes()), reverse=True)
        for r in rows:
            table.removeRow(r)

    # ── Data collection ─────────────────────────────────────

    def _collect_data(self) -> dict:
        data = {
            "config_id": self._current_config_id,
            "config_name": self.ed_name.text(),
            "record_duration": self.spin_record.value(),
            "analysis_duration": self.spin_analysis.value(),
            "scan_triggers_test": self.chk_scan.isChecked(),
            "channels": [],
        }

        for tab_idx in range(self.tab_channels.count()):
            scroll = self.tab_channels.widget(tab_idx)
            container = scroll.widget()
            ch_index = container.property("channel_index")

            ch = {"channel_index": ch_index}

            combo_nfft = container.findChild(QComboBox, "stft_nfft")
            ch["stft_nfft"] = combo_nfft.currentData() if combo_nfft else 1024

            combo_overlap = container.findChild(QComboBox, "stft_overlap")
            ch["stft_overlap"] = combo_overlap.currentData() if combo_overlap else 0.5

            combo_spl_w = container.findChild(QComboBox, "spl_weighting")
            ch["spl_weighting"] = combo_spl_w.currentText() if combo_spl_w else "A"

            spin_tc = container.findChild(QDoubleSpinBox, "spl_time_constant")
            ch["spl_time_constant"] = spin_tc.value() if spin_tc else 0.125

            spin_lo = container.findChild(QDoubleSpinBox, "spl_lower_limit")
            ch["spl_lower_limit"] = spin_lo.value() if spin_lo else 40.0

            spin_hi = container.findChild(QDoubleSpinBox, "spl_upper_limit")
            ch["spl_upper_limit"] = spin_hi.value() if spin_hi else 85.0

            combo_oct_w = container.findChild(QComboBox, "oct_weighting")
            ch["oct_weighting"] = combo_oct_w.currentText() if combo_oct_w else "A"

            ch["fft_band_limits"] = self._read_band_table(container, "fft_band_table")
            ch["oct_band_limits"] = self._read_band_table(container, "oct_band_table")
            ch["ai_config"] = {}

            data["channels"].append(ch)

        return data

    @staticmethod
    def _read_band_table(container: QWidget, table_name: str) -> list[dict]:
        table = container.findChild(QTableWidget, table_name)
        if not table:
            return []
        bands = []
        for r in range(table.rowCount()):
            try:
                freq = float(table.item(r, 0).text()) if table.item(r, 0) else 0
                lower = float(table.item(r, 1).text()) if table.item(r, 1) else 0
                upper = float(table.item(r, 2).text()) if table.item(r, 2) else 0
            except ValueError:
                # Preserve row with sentinel values — validate() will flag freq <= 0
                freq, lower, upper = -1, 0, 0
            bands.append({"freq": freq, "lower": lower, "upper": upper})
        return bands

    # ── Populate UI from data ───────────────────────────────

    def _populate_detail(self, data: dict):
        self.ed_name.setText(data["config_name"])
        self.spin_record.setValue(data["record_duration"])
        self.spin_analysis.setValue(data["analysis_duration"])
        self.chk_scan.setChecked(data["scan_triggers_test"])

        ch_count = self.services.config_service.get_channel_count()
        self._build_channel_tabs(ch_count, data.get("channels"))

    # ── Interactions ────────────────────────────────────────

    def _refresh_list(self):
        self.config_list.clear()
        configs = self.services.config_service.list_configs()
        for c in configs:
            item = QListWidgetItem(c["config_name"])
            item.setData(Qt.ItemDataRole.UserRole, c["config_id"])
            self.config_list.addItem(item)

        has_items = len(configs) > 0
        self.empty_label.setVisible(not has_items)
        self.detail.setVisible(has_items)
        self.btn_delete.setEnabled(has_items)
        self.btn_save.setEnabled(has_items)

        if has_items:
            self.config_list.setCurrentRow(0)

    def _select_config_by_id(self, config_id: int):
        for i in range(self.config_list.count()):
            item = self.config_list.item(i)
            if item.data(Qt.ItemDataRole.UserRole) == config_id:
                self.config_list.setCurrentRow(i)
                return
        if self.config_list.count() > 0:
            self.config_list.setCurrentRow(0)

    def _on_item_changed(self, current: QListWidgetItem | None, _prev):
        if current is None:
            return
        config_id = current.data(Qt.ItemDataRole.UserRole)
        self._current_config_id = config_id
        try:
            data = self.services.config_service.load_config(config_id)
            self.empty_label.setVisible(False)
            self.detail.setVisible(True)
            self._populate_detail(data)
        except Exception as e:
            QMessageBox.warning(self, "加载失败", f"无法加载配置：{e}")

    def _on_new(self):
        name, ok = QInputDialog.getText(self, "新建配置", "请输入配置名称：")
        if not ok:
            return
        try:
            cid = self.services.config_service.create_config(name)
            self._refresh_list()
            self._select_config_by_id(cid)
        except ValueError as e:
            QMessageBox.warning(self, "创建失败", str(e))

    def _on_delete(self):
        current = self.config_list.currentItem()
        if current is None:
            return
        config_id = current.data(Qt.ItemDataRole.UserRole)
        config_name = current.text()
        reply = QMessageBox.question(
            self,
            "确认删除",
            f"确定要删除配置「{config_name}」吗？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return
        self.services.config_service.delete_config(config_id)
        self._current_config_id = None
        self._refresh_list()

    def _on_save(self):
        if self._current_config_id is None:
            return
        data = self._collect_data()
        errors = self.services.config_service.validate(data)
        if errors:
            QMessageBox.warning(self, "验证失败", "\n".join(errors))
            return
        try:
            saved_id = self._current_config_id
            self.services.config_service.save_config(data)
            self._refresh_list()
            self._select_config_by_id(saved_id)
            self.services.bus.status.emit("配置已保存")
        except ValueError as e:
            QMessageBox.warning(self, "保存失败", str(e))

    def _on_hardware_changed(self):
        if self._current_config_id is not None:
            try:
                data = self.services.config_service.load_config(self._current_config_id)
                self._populate_detail(data)
            except Exception:
                pass
```

- [ ] **Step 2: Verify module imports**

Run: `cd D:/Work/jingchengpython/JingChengMechanical && python -c "from ui.modules.config import ConfigModule; print('OK')"`

Expected: `OK`

- [ ] **Step 3: Commit**

```bash
git add ui/modules/config.py
git commit -m "feat: add ConfigModule UI for configuration management"
```

---

### Task 8: Integrate ConfigModule into MainWindow

**Files:**
- Modify: `app/main_window.py`

- [ ] **Step 1: Replace config placeholder page**

In `app/main_window.py`:

Add import at top (after the existing module imports):
```python
from ui.modules.config import ConfigModule
```

Replace the line:
```python
        self.tab.addWidget(self.build_placeholder_page("配置"))
```
with:
```python
        self.tab.addWidget(self.build_config_page())
```

Add new method after `build_placeholder_page`:
```python
    def build_config_page(self) -> QWidget:
        from ui.widgets.cards import Card

        page = QWidget()
        lay = QVBoxLayout(page)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)

        self.config_module = ConfigModule(self.services)
        lay.addWidget(Card("测试配置管理", self.config_module), 1)
        return page
```

- [ ] **Step 2: Launch the app and verify the config page is visible**

Run: `cd D:/Work/jingchengpython/JingChengMechanical && python main.py`

Manual verification:
1. App launches without errors
2. Click the "配置" tab — the config page appears
3. Click "新建" — input dialog appears, enter a name, config is created
4. Global params (名称, 录音时长, 分析时长, 扫码触发) are editable
5. Channel tabs appear if hardware is saved (or warning shows if not)
6. Collapsible sections work (click headers to fold/unfold)
7. Band tables: "添加频带" adds a row, "删除选中" removes selected rows
8. "保存配置" saves and refreshes
9. "删除" removes config with confirmation
10. Switching between configs in the left list updates the right panel

- [ ] **Step 3: Commit**

```bash
git add app/main_window.py
git commit -m "feat: integrate ConfigModule into main window config tab"
```
