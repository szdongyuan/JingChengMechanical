# Configuration Interface Design Spec

## Overview

Add a multi-profile test configuration interface to the "配置" tab. Users can create, edit, and delete named configuration profiles that define recording parameters, trigger behavior, and per-channel analysis settings (STFT, SPL, FFT band limits, 1/3 octave band limits, AI placeholder). Configurations are persisted in the SQLite database and selected during test execution.

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

- `QListWidget` or vertical `QVBoxLayout` with styled items
- Each item shows `config_name`
- Selected item highlighted with blue border (matching hardware page style)
- Top buttons: "新建" (Primary style), "删除" (Danger style)
- Clicking an item loads that config into the right panel

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
- If no hardware is saved, show a warning message instead of tabs

#### Per-Channel Content (inside each tab, scrollable via QScrollArea)

**STFT Section** — collapsible `QGroupBox`:
- `stft_nfft`: `QComboBox` with options [256, 512, 1024, 2048, 4096, 8192]
- `stft_overlap`: `QComboBox` with options [0.25, 0.5, 0.75]
- Window function fixed to Hann (display as label, not configurable)

**SPL Section** — collapsible `QGroupBox`:
- `spl_weighting`: `QComboBox` [A, C, Z]
- `spl_time_constant`: `QDoubleSpinBox` (min 0.001, step 0.001, suffix " 秒")
- `spl_lower_limit`: `QDoubleSpinBox` (suffix " dB")
- `spl_upper_limit`: `QDoubleSpinBox` (suffix " dB")

**FFT Band Limits Section** — collapsible `QGroupBox`:
- `QTableWidget` with columns: 频率(Hz), 下限(dB), 上限(dB)
- Cells are editable `QDoubleSpinBox` delegates
- Buttons below table: "添加频带", "删除选中"

**1/3 Octave Section** — collapsible `QGroupBox`:
- `oct_weighting`: `QComboBox` [A, C, Z]
- `QTableWidget` with columns: 频带(Hz), 下限(dB), 上限(dB)
- Same editable table pattern as FFT
- Buttons below table: "添加频带", "删除选中"

**AI Analysis Section** — collapsible `QGroupBox`:
- Placeholder label: "待配置，后续扩展"
- Empty `QGroupBox` ready for future parameters

#### Save Button

- Bottom-right "保存配置" button with Primary style
- Triggers validation then save

### Styling

All widgets follow existing patterns from `consts/style.py`:
- Cards use `Card` widget from `ui/widgets/cards.py`
- Buttons use `objectName` = "Primary" / "Danger"
- Muted text uses `objectName` = "Muted"
- ComboBoxes use `combo_qss`
- GroupBox sections use standard QSS with collapsible checkable behavior

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
        """

    def save_config(self, data: dict) -> int:
        """Create or update a configuration.
        If data contains "config_id", updates existing.
        Otherwise creates new.
        Deletes existing channel configs and re-creates them.
        Returns config_id.
        Emits bus.config_changed after success.
        """

    def delete_config(self, config_id: int) -> bool:
        """Delete config and all associated channel configs (CASCADE).
        Emits bus.config_changed after success.
        Returns True on success.
        """

    def get_channel_count(self) -> int:
        """Read input_channel_count from saved hardware (DeviceTable id=1).
        Returns 0 if no hardware is saved.
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
config_changed = pyqtSignal()  # emitted after config save/delete
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

1. **Page entry**: calls `config_service.list_configs()`, populates left list, selects first item
2. **List item click**: calls `config_service.load_config(id)`, populates right panel forms
3. **New button**: shows `QInputDialog` for name, creates config with defaults, selects it
4. **Delete button**: shows `QMessageBox.question` confirmation, deletes, selects first remaining item
5. **Save button**: collects all form values, validates, calls `config_service.save_config(data)`, refreshes list
6. **Hardware change** (`refresh_channels` signal): updates channel tab count

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

### Validation Rules (on save)

1. `config_name` must be non-empty
2. `config_name` must be unique (except for current config's own name)
3. `analysis_duration` <= `record_duration`
4. `spl_lower_limit` < `spl_upper_limit`
5. For each FFT band row: `lower` < `upper`
6. For each octave band row: `lower` < `upper`

Validation failure shows `QMessageBox.warning` with specific error message. Config is not saved.

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

### Modified files

| File | Change |
|------|--------|
| `app/services_container.py` | Add `config_changed` signal to EventBus; register `ConfigService` |
| `app/main_window.py` | Replace config placeholder with `build_config_page()` using `ConfigModule` |

### Database

Two new tables need to be created in `data/jingchen.db`:
- `test_config_table`
- `channel_config_table`

Table creation should be done via a one-time setup script or added to the DB connection startup (using Peewee's `create_tables` with `safe=True`).
