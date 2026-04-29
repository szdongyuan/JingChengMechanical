# Config Page Chinese UI Design

## Context

The configuration page is used primarily by Chinese-speaking operators, but some
customer-visible labels remain in English. The visible English is concentrated in
the per-channel golden-sample controls, the golden-sample import dialog launched
from the configuration page, and the configuration package file dialogs.

This is a UI-only text cleanup. Internal identifiers, persisted enum values,
database fields, config package payloads, and analysis behavior must remain
unchanged.

## Goals

1. Replace configuration-page customer-visible English labels with Chinese where
   doing so does not hide established technical terms.
2. Keep approved abbreviations and units visible where they are clearer than a
   translated term.
3. Preserve all stored values and code contracts for golden-sample modes,
   channel indexes, config packages, and AI bindings.
4. Update tests that assert visible UI strings.

## Non-Goals

- Do not add a general i18n framework.
- Do not translate Python identifiers, object names, database columns, JSON
  keys, enum/data values, log/debug strings, or comments.
- Do not change `.dy` package structure, manifest fields, or import/export
  behavior.
- Do not change golden-sample validation, FFT/octave rendering, AI behavior, or
  hardware device matching.
- Do not rename user-provided hardware device names or AI toolchain display
  names.

## Approved Terms That May Remain Visible

- `AI`
- `PLC`
- `FFT`
- `STFT`
- `WAV`
- `DY`
- `Hz`
- `dB`
- `ms`
- `sone`
- `phon`

## Required UI Text Changes

In `ui/modules/config.py`:

- File dialog title `选择 Golden Sample WAV` becomes `选择黄金样本 WAV`.
- File dialog filter `WAV Files (*.wav)` becomes `WAV 文件 (*.wav)`.
- Channel selector and channel checkbox labels `In1`, `In2`, etc. become
  `输入1`, `输入2`, etc.; tab labels should read `输入1（通道 1）` and so on.
- Section title `SPL 参数` becomes `声压级参数`.
- Row label `SPL` becomes `声压级`.
- Row label `平均SPL` becomes `平均声压级`.
- Section title `响度 参数` becomes `响度参数`.
- Section title `Golden Sample` becomes `黄金样本`.
- Button `Import WAV...` becomes `导入 WAV...`.
- Button `Replace WAV...` becomes `替换 WAV...`.
- Button `Clear` becomes `清除`.
- Golden-sample mode choices:
  - `Off` -> `关闭`
  - `Absolute` -> `绝对值`
  - `Delta` -> `差值`
- Golden-sample mode row label `Golden mode` becomes `黄金样本模式`.
- Golden overlay checkbox `Show golden curve` becomes `显示黄金曲线`.
- Golden overlay row label `Golden curve` becomes `黄金曲线`.
- Missing host API fallback display text `N/A` becomes `未知`, while the stored
  fallback host API value remains `N/A` for compatibility with existing matching
  and persisted config data.

In `ui/popups/golden_sample_import_dialog.py`:

- Window title `Golden Sample Import` becomes `导入黄金样本`.
- Summary labels:
  - `Sample rate` -> `采样率`
  - `Channels` -> `通道数`
  - `Total duration` -> `总时长`
  - `Required duration` -> `所需时长`
- Control labels:
  - `Source channel` -> `源通道`
  - `Start` -> `起点`
  - `End` -> `终点`
- Plot labels:
  - `Time` -> `时间`
  - `Amplitude` -> `幅值`
- Ready status `Ready` becomes `就绪`.
- Dialog buttons should display `确定` and `取消` where the dialog creates
  standard OK/Cancel buttons directly.

In `ui/popups/config_package_dialogs.py`:

- File filter `DY Packages (*.dy)` becomes `DY 配置包 (*.dy)`.
- User-facing warning text that mentions `toolchain_id` should describe it as
  `工具链 ID` while leaving the actual stored key unchanged.
- Dialog buttons should display `确定` and `取消` where the dialog creates
  standard OK/Cancel buttons directly.

## Behavior Requirements

- `fft_golden_mode` and `oct_golden_mode` stored values remain `off`,
  `absolute`, and `delta`.
- Overlay-disable behavior still keys off the mode data value `delta`, not the
  translated display text.
- `_collect_data()` output remains byte-for-byte compatible for affected fields
  except for unrelated ordering that Python dictionaries already allow.
- Unknown host API display translation must not change `_collect_data()` output;
  if a host API is unavailable and the internal fallback is `N/A`, collected
  `input_device_hostapi` / `output_device_hostapi` still use `N/A`.
- Config import/export paths and package contents remain unchanged.
- Tests should prefer `currentData()` for stored mode values and assert
  translated `currentText()` only when checking visible labels.

## Compatibility / Migration

- Backward compatibility: partial.
- Protected surfaces:
  - Database schema and persisted configuration fields.
  - `.dy` package format and manifest/config payload keys.
  - Golden-sample mode stored values.
  - AI request/response protocol and AI binding fields.
  - Analysis, validation, and judgment behavior.
  - Hardware device names supplied by the host system.
- Allowed breakage:
  - Customer-visible configuration-page labels may change.
  - UI tests that assert visible strings must update expectations.
- Migration strategy: none. This is a presentation-only change.

## Testing Strategy

- Run focused configuration module tests:
  - `pytest tests/test_config_module.py -q`
- At minimum, tests must cover:
  - golden-sample summary and controls render translated labels
  - golden-sample import dialog renders translated summary/control/status labels
  - translated `差值` mode still disables overlay checkboxes
  - golden mode stored data remains `off` / `absolute` / `delta`
  - unknown host API fallback displays `未知` while collected data remains `N/A`
  - `.dy` file dialog filter uses Chinese visible text
