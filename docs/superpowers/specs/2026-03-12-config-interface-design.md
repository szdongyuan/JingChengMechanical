# Configuration Interface Design Spec

## Overview

Add a multi-profile test configuration interface to the "配置" tab. Users can create, edit, and delete named configuration profiles that define recording parameters, trigger behavior, and per-channel analysis settings (STFT, SPL, FFT band limits, 1/3 octave band limits, AI placeholder). Configurations are persisted in the SQLite database and selected during test execution.

### Scope Boundary

This spec covers only the configuration CRUD interface (the "配置" tab). How configuration is consumed by the test execution pipeline (selecting active config in the workorder, feeding params to real analysis, scan-trigger hardware integration) is out of scope and will be addressed when `TestService` is replaced with real implementation. The `config_changed` signal is provided so future work can subscribe to it.

## Data Model

### TestConfigTable

| Field | Type | Constraint | Description |
|-------|------|------------|-------------|
| `config_id` | `AutoField` | PK | Auto-increment primary key |
| `config_name` | `TextField` | UNIQUE, NOT NULL | Profile name, e.g. "电机-A型号" |
| `record_duration` | `FloatField` | > 0 | Recording duration in seconds |
| `analysis_duration` | `FloatField` | > 0, <= record_duration | Analysis selection duration in seconds |
| `scan_triggers_test` | `BooleanField` | default False | Whether barcode scan also triggers test start |
| `created_time` | `TextField` | | Timestamp string (UTC+8, via `now_str()`) |
| `updated_time` | `TextField` | | Timestamp string (UTC+8, via `now_str()`) |

File: `model/test_config_table.py`
Table name: `test_config_table`

### ChannelConfigTable

| Field | Type | Constraint | Description |
|-------|------|------------|-------------|
| `channel_config_id` | `AutoField` | PK | Auto-increment primary key |
| `config` | `ForeignKeyField(TestConfigTable)` | ON DELETE CASCADE | Parent configuration |
| `channel_index` | `IntegerField` | >= 0 | Channel number (0-based) |
| `stft_nfft` | `IntegerField` | power of 2 | STFT FFT size, e.g. 1024 |
| `stft_overlap` | `FloatField` | 0 < x < 1 | STFT overlap ratio, e.g. 0.5 |
| `spl_weighting` | `TextField` | A/C/Z | SPL frequency weighting |
| `spl_time_constant` | `FloatField` | > 0 | SPL time constant in seconds, e.g. 0.125 |
| `spl_upper_limit` | `FloatField` | | SPL upper limit in dB |
| `spl_lower_limit` | `FloatField` | < spl_upper_limit | SPL lower limit in dB |
| `fft_band_limits` | `TextField` | JSON | FFT per-band upper/lower limits |
| `oct_weighting` | `TextField` | A/C/Z | 1/3 octave frequency weighting |
| `oct_band_limits` | `TextField` | JSON | 1/3 octave per-band upper/lower limits |
| `ai_config` | `TextField` | JSON | AI analysis config placeholder, default `"{}"` |

File: `model/channel_config_table.py`
Table name: `channel_config_table`
Unique constraint: `(config, channel_index)`

### Band Limits JSON Format

Both `fft_band_limits` and `oct_band_limits` use the same JSON structure:

```json
[
  {"freq": 100, "lower": 30.0, "upper": 60.0},
  {"freq": 200, "lower": 35.0, "upper": 65.0},
  {"freq": 500, "lower": 40.0, "upper": 70.0}
]
```

Array of objects, each with `freq` (Hz), `lower` (dB), and `upper` (dB). Array format (not dict) preserves insertion order and allows duplicate-frequency entries if needed.

## UI Layout

### Page Structure: Left List + Right Detail

The configuration page follows the same left-right split pattern as the hardware page.

```
┌──────────────┬─────────────────────────────────────────────┐
│ Config List   │ Config Detail: 电机-A型号                    │
│               │                                             │
│ [New] [Delete]│ ┌ Global Parameters ─────────────────────┐  │
│               │ │ Name:       [电机-A型号              ]  │  │
│ ● 电机-A型号  │ │ Record:     [5.0] sec                   │  │
│ ○ 电机-B型号  │ │ Analysis:   [3.0] sec                   │  │
│ ○ 水泵测试    │ │ Scan trigger: [✓]                       │  │
│               │ └─────────────────────────────────────────┘  │
│               │                                             │
│               │ [Ch 1] [Ch 2] [Ch 3] [Ch 4]                │
│               │ ┌ Channel content (scrollable) ──────────┐  │
│               │ │ ▼ STFT    (QFormLayout)                │  │
│               │ │ ▼ SPL     (QFormLayout)                │  │
│               │ │ ▼ FFT     (QTableWidget)               │  │
│               │ │ ▼ 1/3 Oct (QTableWidget)               │  │
│               │ │ ▼ AI      (placeholder)                │  │
│               │ └────────────────────────────────────────┘  │
│               │                              [Save Config]  │
└──────────────┴─────────────────────────────────────────────┘
```

### Left Panel — Config List

- `QListWidget` with items keyed by `config_id` (stored in `Qt.ItemDataRole.UserRole`)
- Each item shows `config_name`
- Selected item highlighted with blue border (matching hardware page style)
- Top buttons: "新建" (Primary style), "删除" (Danger style)
- Clicking an item loads that config into the right panel

**Empty state (0 configs):** Right panel shows a centered muted label "暂无配置，请点击「新建」创建" with delete button disabled and save button disabled.

### Right Panel — Config Detail

#### Global Parameters Section

A `Card` with `QFormLayout` containing:
- `config_name`: `QLineEdit`
- `record_duration`: `QDoubleSpinBox` (min 0.1, step 0.5, suffix " 秒")
- `analysis_duration`: `QDoubleSpinBox` (min 0.1, step 0.5, suffix " 秒")
- `scan_triggers_test`: `QCheckBox`

#### Channel Tabs

- `QTabWidget` with tabs labeled "通道 1", "通道 2", etc.
- Tab count determined by `get_channel_count()` from saved hardware input channel count
- If no hardware is saved, or `input_channel_count` is null/non-numeric/0, show a warning label: "请先在硬件页面选择并保存输入设备" instead of tabs. Global parameters remain editable.

#### Channel Count Change Behavior

When hardware changes (via `refresh_channels` signal):
- If new channel count > saved channels: extra tabs are created with default values
- If new channel count < saved channels: excess channel configs are preserved in DB but hidden from UI. Saving will only write channels visible in the current tab set. A muted note below tabs reads: "当前硬件通道数与已保存配置不同，多余通道配置将在保存时移除"

#### Per-Channel Content (inside each tab, scrollable via QScrollArea)

Collapsible sections are implemented as custom `CollapsibleGroupBox` widgets: a clickable header that toggles `setVisible()` on the content widget below it. Standard `QGroupBox.setCheckable()` does not collapse content — it only enables/disables children — so a custom widget is needed.

**STFT Section** — `CollapsibleGroupBox`:
- `stft_nfft`: `QComboBox` with options [256, 512, 1024, 2048, 4096, 8192]
- `stft_overlap`: `QComboBox` with options [0.25, 0.5, 0.75]
- Window function fixed to Hann (display as `QLabel`, not configurable)

**SPL Section** — `CollapsibleGroupBox`:
- `spl_weighting`: `QComboBox` [A, C, Z]
- `spl_time_constant`: `QDoubleSpinBox` (min 0.001, step 0.001, suffix " 秒")
- `spl_lower_limit`: `QDoubleSpinBox` (suffix " dB")
- `spl_upper_limit`: `QDoubleSpinBox` (suffix " dB")

**FFT Band Limits Section** — `CollapsibleGroupBox`:
- `QTableWidget` with columns: 频率(Hz), 下限(dB), 上限(dB)
- Cells are editable `QDoubleSpinBox` delegates
- "添加频带" inserts a new row with defaults: freq=1000, lower=0.0, upper=100.0
- "删除选中" removes selected rows
- Frequencies must be positive. Duplicate frequencies are allowed (for flexibility). Rows need not be sorted — display order matches insertion order.

**1/3 Octave Section** — `CollapsibleGroupBox`:
- `oct_weighting`: `QComboBox` [A, C, Z]
- `QTableWidget` with columns: 频带(Hz), 下限(dB), 上限(dB)
- Same editable table pattern and row-add defaults as FFT
- "添加频带", "删除选中"

**AI Analysis Section** — `CollapsibleGroupBox`:
- Placeholder label: "待配置，后续扩展"
- Empty content area ready for future parameters

#### Save Button

- Bottom-right "保存配置" button with Primary style
- Triggers validation then save

### Styling

All widgets follow existing patterns from `consts/style.py`:
- Cards use `Card` widget from `ui/widgets/cards.py`
- Buttons use `objectName` = "Primary" / "Danger"
- Muted text uses `objectName` = "Muted"
- ComboBoxes use `combo_qss`
- GroupBox sections use custom `CollapsibleGroupBox` (clickable header toggles content visibility)

### CollapsibleGroupBox Widget

File: `ui/widgets/collapsible_group_box.py`

A simple custom widget:
- Header: clickable `QLabel` with arrow indicator (▼ expanded, ► collapsed)
- Content: a `QWidget` container whose visibility is toggled on header click
- Default state: expanded
- No animation needed — simple `setVisible(True/False)`

## Service Layer

### ConfigService

File: `app/service/config_service.py`

```python
class ConfigService:
    def __init__(self, bus):
        self.bus = bus

    def list_configs(self) -> list[dict]:
        """Return [{"config_id": int, "config_name": str}, ...]"""

    def load_config(self, config_id: int) -> dict:
        """Return full config with global params and channel configs.
        Returns:
            {
                "config_id": int,
                "config_name": str,
                "record_duration": float,
                "analysis_duration": float,
                "scan_triggers_test": bool,
                "channels": [
                    {
                        "channel_index": int,
                        "stft_nfft": int,
                        "stft_overlap": float,
                        "spl_weighting": str,
                        "spl_time_constant": float,
                        "spl_upper_limit": float,
                        "spl_lower_limit": float,
                        "fft_band_limits": list[dict],
                        "oct_weighting": str,
                        "oct_band_limits": list[dict],
                        "ai_config": dict,
                    }, ...
                ]
            }
        Raises DoesNotExist if config_id is invalid.
        JSON fields are deserialized; on parse failure, defaults to [] or {}.
        """

    def create_config(self, config_name: str) -> int:
        """Create a new config with defaults and save it immediately to DB.
        Validates name is non-empty and unique; raises ValueError on failure.
        On IntegrityError (e.g. race condition on unique name), raises ValueError.
        Creates channel configs for each channel (based on get_channel_count()).
        If channel count is 0, creates the parent config with no channel rows.
        All within db.atomic().
        Returns config_id.
        Emits bus.config_changed after success.
        """

    def save_config(self, data: dict) -> int:
        """Update an existing configuration.
        data must contain "config_id".
        Validates all fields (see Validation Rules).
        Raises ValueError with message on validation failure.
        Wrapped in db.atomic(): updates parent row, deletes old channel
        configs, inserts new channel configs for visible channels only.
        If channel count is 0 (no hardware), only updates global fields
        and preserves existing channel configs untouched.
        Returns config_id.
        Emits bus.config_changed after success.
        On IntegrityError (e.g. name collision), raises ValueError.
        """

    def delete_config(self, config_id: int) -> bool:
        """Delete config and all associated channel configs (CASCADE).
        Wrapped in db.atomic().
        Emits bus.config_changed after success.
        Returns True on success, False if config_id not found.
        """

    def get_channel_count(self) -> int:
        """Read input_channel_count from saved hardware (DeviceTable id=1).
        Returns 0 if no hardware is saved, or if value is null/non-numeric.
        Parses the TextField to int, catches ValueError.
        """

    def validate(self, data: dict) -> list[str]:
        """Validate config data. Returns list of error messages (empty = valid).
        Rules:
        1. config_name non-empty
        2. config_name unique (excluding own config_id)
        3. record_duration > 0
        4. analysis_duration > 0 and <= record_duration
        5. scan_triggers_test is bool
        Per-channel rules (only validated when channels are present in data):
        6. stft_nfft in [256, 512, 1024, 2048, 4096, 8192]
        7. stft_overlap in [0.25, 0.5, 0.75]
        8. spl_weighting in ["A", "C", "Z"]
        9. spl_time_constant > 0
        10. spl_lower_limit < spl_upper_limit
        11. oct_weighting in ["A", "C", "Z"]
        12. For each FFT band row: freq > 0, lower < upper
        13. For each octave band row: freq > 0, lower < upper
        14. fft_band_limits and oct_band_limits must be valid JSON arrays of {freq, lower, upper} dicts
        15. channel_index must be >= 0 and < current channel count, no duplicates
        """
```

### ServiceContainer Registration

In `app/services_container.py`:
```python
self.config_service = ConfigService(self.bus)
```

### EventBus Addition

In `EventBus` class:
```python
config_changed = pyqtSignal()  # emitted after config create/save/delete
```

## UI Module

### ConfigModule

File: `ui/modules/config.py`

Class: `ConfigModule(QWidget)`
- Receives `services` in constructor
- `module_id = "config"`
- Manages left list and right detail panel
- Subscribes to `services.bus.refresh_channels` to update tab count when hardware changes

### Interaction Flow

1. **Page entry**: calls `config_service.list_configs()`, populates left list. If list is non-empty, selects first item and loads it. If empty, shows empty-state in right panel.
2. **List item click**: calls `config_service.load_config(id)`, populates right panel forms and channel tabs.
3. **New button**: shows `QInputDialog` for name. On OK, calls `config_service.create_config(name)` which immediately persists a config with defaults. On success, refreshes list and selects the new item. On `ValueError` (empty/duplicate name), shows `QMessageBox.warning`.
4. **Delete button**: shows `QMessageBox.question` confirmation. On yes, calls `config_service.delete_config(id)`. Refreshes list, selects first remaining item (or shows empty-state).
5. **Save button**: collects all form values into a dict, calls `config_service.validate(data)`. If errors, shows `QMessageBox.warning` with all error messages joined. If valid, calls `config_service.save_config(data)`. On success, refreshes list and re-selects current item. On `ValueError` (e.g. name collision), shows warning.
6. **Hardware change** (`refresh_channels` signal): calls `get_channel_count()`, updates channel tab count for currently displayed config.

### Default Values for New Config

| Parameter | Default |
|-----------|---------|
| `record_duration` | 5.0 |
| `analysis_duration` | 3.0 |
| `scan_triggers_test` | False |
| `stft_nfft` | 1024 |
| `stft_overlap` | 0.5 |
| `spl_weighting` | "A" |
| `spl_time_constant` | 0.125 |
| `spl_lower_limit` | 40.0 |
| `spl_upper_limit` | 85.0 |
| `fft_band_limits` | `[]` |
| `oct_weighting` | "A" |
| `oct_band_limits` | `[]` |
| `ai_config` | `{}` |

### Validation Rules

Validation is performed in `ConfigService.validate()` (canonical) and echoed in UI as early feedback via widget constraints (spinbox min/max, combobox fixed options).

Global rules:
1. `config_name` must be non-empty
2. `config_name` must be unique (except for current config's own id)
3. `record_duration` > 0
4. `analysis_duration` > 0 and <= `record_duration`
5. `scan_triggers_test` must be bool

Per-channel rules (only validated when channels are present in data):
6. `stft_nfft` must be in [256, 512, 1024, 2048, 4096, 8192]
7. `stft_overlap` must be in [0.25, 0.5, 0.75]
8. `spl_weighting` must be in ["A", "C", "Z"]
9. `spl_time_constant` > 0
10. `spl_lower_limit` < `spl_upper_limit`
11. `oct_weighting` must be in ["A", "C", "Z"]
12. For each FFT band row: `freq` > 0, `lower` < `upper`
13. For each octave band row: `freq` > 0, `lower` < `upper`
14. `fft_band_limits` and `oct_band_limits` must be valid JSON arrays of `{freq, lower, upper}` dicts
15. `channel_index` must be >= 0 and < current channel count, no duplicates

Validation failure returns a list of human-readable error messages. UI shows all errors in a single `QMessageBox.warning`. Config is not saved.

## MainWindow Integration

In `app/main_window.py`:
- Replace `build_placeholder_page("配置")` with `build_config_page()`
- `build_config_page()` creates a `ConfigModule(self.services)` and returns it

## File Summary

### New files

| File | Purpose |
|------|---------|
| `model/test_config_table.py` | TestConfigTable Peewee model |
| `model/channel_config_table.py` | ChannelConfigTable Peewee model |
| `app/service/config_service.py` | ConfigService business logic |
| `ui/modules/config.py` | ConfigModule UI widget |
| `ui/widgets/collapsible_group_box.py` | CollapsibleGroupBox reusable widget |

### Modified files

| File | Change |
|------|--------|
| `app/services_container.py` | Add `config_changed` signal to EventBus; register `ConfigService` |
| `app/service/db_connection_service.py` | Add `create_tables` call for new tables after connect |
| `app/main_window.py` | Replace config placeholder with `build_config_page()` using `ConfigModule` |

### Database

Two new tables need to be created in `data/jingchen.db`:
- `test_config_table`
- `channel_config_table`

Table creation is done in `DbConnectionService.connect()` after opening the connection:
```python
db.create_tables([TestConfigTable, ChannelConfigTable], safe=True)
```
`safe=True` makes this a no-op if tables already exist, so it is safe to run on every startup.
