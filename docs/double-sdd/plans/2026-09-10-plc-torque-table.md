# PLC Torque Table Implementation Plan

> **For agentic workers:** REQUIRED: Use the `subagent-driven-development` skill to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在 PLC 开始测试后，根据读取扭矩是否为零自动选择 PLC 值或型号 / SN 扭矩表，完成 REAL 回传、Excel 导入和工单扭矩状态显示。

**Architecture:** 使用独立的扭矩配置及规则表；持久化服务发布不可变内存快照。PLC 所属线程在提交现有测试开始事件和尝试复位之后进行一次扭矩读写，Qt 信号将结果送到界面；后台任务处理 Excel 和数据库变更。

**Tech Stack:** Python 3.12、PyQt6、Peewee / SQLite、python-snap7 3.0.0、openpyxl 3.1.5、pytest 8.4.2。

---

## 依据、执行规则与当前仓库状态

Spec: `docs/double-sdd/specs/2026-09-10-plc-torque-table-design.md`

设计已由用户确认，提交为 `bc0738f`。本计划针对 2026-09-10 主工作区实际文件内容编写；不能假设这个提交已包含所有当前代码。`app/service/plc_service.py`、`app/service/db_connection_service.py`、`app/services_container.py` 及相关测试当前含有其他任务的未提交改动。

必须使用 `subagent-driven-development`：任务顺序执行，每个任务使用新的 implementer，完成后对同一变更范围运行 spec-code-reviewer 和 quality-code-reviewer，按 `code-review` 技能评估发现。实现者负责其任务声明的文件，必须保留其他人的修改。执行代码前记录主工作区 HEAD、主分支和文件快照，并从该精确 HEAD 创建独立 worktree。

### 执行准备：保证 worktree 包含当前代码

- [x] 在开始执行时重新读取 `git status --short`、`git rev-parse HEAD`、`git branch --show-current`，将其保存到 `.worktrees/plc-torque-table.meta.json`，并记录执行路径、源文件哈希清单和随后创建的快照提交。不要把计划编写时的 HEAD 当作执行时集成基点。
- [x] 使用分支 `codex/plc-torque-table`、目录 `.worktrees/plc-torque-table`。若同名已存在，先检查其状态后复用本任务记录，不覆盖别人的 worktree。

```powershell
$torqueMainBase = git rev-parse HEAD
git worktree add .worktrees/plc-torque-table -b codex/plc-torque-table $torqueMainBase
git diff --binary --output=.worktrees/plc-torque-baseline.patch HEAD -- app base consts model ui tests scripts main.py main.spec requirements.txt
git -C .worktrees/plc-torque-table apply --check ../plc-torque-baseline.patch
git -C .worktrees/plc-torque-table apply ../plc-torque-baseline.patch
git ls-files --others --exclude-standard -- app base consts model ui tests scripts native
```

- [x] 将上一步实际列出的未跟踪源码、测试及所依赖的本地运行资产按原相对路径复制到 worktree，记录清单和哈希；禁止复制生产数据库、录音、模型训练产物和打包输出。`app/service/plc_current_history.py`、`app/service/startup_diagnostics.py`、`app/service/capture_process/` 等当前未跟踪模块可能已被现有代码导入，应按实际导入闭包保留，不能仅复制扭矩相关文件。Python 源码所需 native DLL 只作为已有运行资产复制，不重编译。
- [x] 把 `.agents/skills` 及所需 `.codex/agents` 工作流文件按原相对路径复制到 worktree 作为本地工作流支持；不混入功能提交。确认 spec 和本 plan 在 worktree 中可读；若 plan 尚未提交则复制该文档。
- [x] 在隔离 worktree 内对源码快照建立 `tmp/sdd: preserve existing workspace baseline` 临时提交，保存其 ID 为 `FEATURE_BASE`。该提交只能用作实现前状态；后续功能差异、最终评审和集成均使用 `FEATURE_BASE..HEAD`。不得把它或其他临时提交直接合并到主分支。
- [x] 核对依赖，并运行下述现有回归基线。已存在的失败先记录和定位，不能通过删除断言把它们变成通过；使用 `systematic-debugging` 处理会阻断本功能的失败。

所有以下命令在 worktree 根目录运行。当前工作区已有可用 `.venv`，在标准 worktree 路径下：

```powershell
$torquePython = (Resolve-Path '../../.venv/Scripts/python.exe').Path
$env:QT_QPA_PLATFORM = 'offscreen'
$env:APP_DB_PATH = ':memory:'
& $torquePython -c 'import sys, importlib.metadata as m; print(sys.version); print({n:m.version(n) for n in ("pytest", "PyQt6", "peewee", "openpyxl", "python-snap7")})'
& $torquePython -m pytest tests/test_plc_service_workorder.py tests/test_plc_service_current.py tests/test_plc_service_shutdown.py tests/test_plc_service_scheduling.py tests/test_db_connection_service.py tests/test_workorder_module.py -q
```

基线必须使用临时数据库和假 PLC；禁止运行生产 `main.py` 来替代验证。不要更新依赖或修改 Snap7 版本。每个 exec 会话按需重新设置以上任务变量，不依赖先前 shell 的变量。

2026-09-10 计划阶段已在主工作区以上述环境变量运行相同的六个现有测试文件，结果为 **109 passed, 1 skipped**。这只证明规划时的现有基线；执行时仍需在隔离 worktree 重新确认基线，不能用它宣称新功能已经通过测试。

执行准备补充：隔离 worktree 的六文件基线同样为 109 passed, 1 skipped。扩展基线（shutdown_order、production_composition、hardware_module、main_startup）为 168 passed, 2 errors；两个错误均为原有 hardware_module 测试请求未定义的 qapp fixture。已确认环境未安装 pytest-qt，现有 workorder 测试自行定义局部 qapp，而共享 conftest 未提供。Task 2 在已拥有的 tests/conftest.py 中补充复用 QApplication.instance() 的 session qapp fixture，以运行既有硬件回归，不改变硬件生产行为或新增依赖。

## 文件职责与接口边界

| 文件 | 责任 |
| --- | --- |
| `app/service/torque/types.py` | 不可变配置、规则快照、导入预览、处理上下文和结果数据结构 |
| `app/service/torque/validation.py` | REAL 数值、匹配键、地址及重叠校验，纯 Python |
| `app/service/torque/migration.py` | 两个新增表的兼容升级需求检查、DDL 和结构校验 |
| `model/plc_torque_config_table.py` | 每个 PLC 的可选扭矩设置 ORM |
| `model/torque_rule_table.py` | 共享唯一规则 ORM |
| `app/service/torque/store.py` | 持久化 CRUD、版本和不可变规则发布，不依赖 Qt / Snap7 |
| `app/service/torque/excel.py` | .xlsx 模板、解析、行号错误与导入预览 |
| `app/service/torque/runtime.py` | 零 / 非零来源决策与单次读写，依赖注入 I/O |
| `app/service/torque/jobs.py` | 应用持有的后台任务和排队 Qt 通知，负责连接和关闭生命周期 |
| `app/service/db_connection_service.py` | 在现有备份 / 兼容升级边界接入新增迁移 |
| `app/service/plc_service.py` | 开始事件后调度、所属线程中 REAL I/O、配置应用回执 |
| `app/services_container.py` | 装配 store / jobs、共享信号、关闭顺序 |
| `ui/popups/torque_settings_dialog.py` | 每个 PLC 扭矩设置和已保存 / 已应用状态 |
| `ui/popups/torque_table_dialog.py` | 表 CRUD、搜索、Excel 模板、预览和提交 |
| `ui/modules/plc_settings.py` | 两个管理入口、PLC 实例配置、信号接线与删除配置 |
| `ui/modules/workorder.py` | 每个 PLC 的扭矩行及触发标识过滤 |

在 `app/service/torque/__init__.py` 保持无副作用；新增模块使用显式导入。不要导入文件时连接数据库或启动线程。

数据结构字段在任务 1 固定，后续任务共同使用，避免各层自行拼装不同含义的字典：

```python
@dataclass(frozen=True)
class TorqueConfig:
    plc_id: int
    enabled: bool = False
    match_type: str = "model"
    read_db_number: int | None = None
    read_byte_offset: int | None = None
    write_db_number: int | None = None
    write_byte_offset: int | None = None
    revision: int = 0

@dataclass(frozen=True)
class RuleSnapshot:
    revision: int
    values: Mapping[tuple[str, str], float]  # Copy then MappingProxyType.

@dataclass(frozen=True)
class TorqueCycle:
    plc_id: int
    trigger_ns: int
    sn: str
    model: str
    config: TorqueConfig
    rules: RuleSnapshot
    inherited_db: int
    cycle_id: int | None = None

@dataclass(frozen=True)
class TorqueStatus:
    plc_id: int
    trigger_ns: int
    state: str  # reading/plc/table_pending/table_sent/unmatched/read_error/invalid_value/write_error/config_error
    value: float | None
    source: str | None  # plc/table
    match_type: str
    match_key: str
    rules_revision: int
    detail: str = ""
    read_value: float | None = None
    target_db: int | None = None
    target_byte_offset: int | None = None
    cycle_id: int | None = None
```

未启用 / 待读取由配置及当前 UI 状态表示，不伪造一个测试触发时间戳。应用配置回执是另一种数据结构，至少包含 plc_id、service_generation、config_revision、成功标志和详情；不能与测试处理结果混用。

## Task 1: 扭矩数据合同与统一校验

**Files:**
- Create: `app/service/torque/__init__.py`
- Create: `app/service/torque/types.py`
- Create: `app/service/torque/validation.py`
- Test: `tests/torque/test_validation.py`

- [x] **Step 1: 写失败测试。** 覆盖精确匹配键去首尾空白、保留前导零 / 大小写、非法匹配类型；REAL 普通数 / 零 / 负数 / NaN / Infinity / 布尔值 / 超范围 / 非零下溢；字节偏移和 DB 继承后的重叠。使用参数化测试验证行为，不测试数据类 repr。

```python
def test_real_keeps_zero_and_negative_but_rejects_nonzero_underflow():
    assert validate_torque(2.5) == 2.5
    assert validate_torque(-2.5) == -2.5
    assert validate_torque(0.0) == 0.0
    with pytest.raises(ValueError):
        validate_torque(1e-100)
    for text in ("1e-1000", "-1e-1000"):
        with pytest.raises(ValueError):
            validate_torque(text)

def test_addresses_are_validated_after_db_inheritance():
    cfg = TorqueConfig(7, True, "model", None, 20, 1, 22)
    with pytest.raises(ValueError, match="重叠"):
        resolve_addresses(cfg, inherited_db=1)
    assert resolve_addresses(cfg, inherited_db=2) == ((2, 20), (1, 22))
```

- [x] **Step 2: 跑 RED。** `& $torquePython -m pytest tests/torque/test_validation.py -q`；应因新增模块 / 行为不存在失败。
- [x] **Step 3: 实现纯函数和完整类型。** `normalize_key(value)` 只接受非空 str 并 strip；`parse_offset(value)` 接受 `N` 和 `N.0`，拒绝 bool、负数及非零位；DB 校验为正整数或 None。`resolve_addresses` 要求启用配置的两个偏移完整，并校验相同 DB 下 `[offset, offset+4)` 区间不重叠。关闭配置允许地址为空，但已填写内容仍须语法有效。

REAL 转换核心必须保留以下语义，返回可真正写入 PLC 的 float32 数值：

```python
def validate_torque(value):
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        raise ValueError("扭矩必须为数值")
    try:
        original = Decimal(value.strip() if isinstance(value, str) else value)
        if not original.is_finite():
            raise ValueError("扭矩必须为有限数值")
        raw = float(original)
        converted = struct.unpack(">f", struct.pack(">f", raw))[0]
    except (InvalidOperation, ValueError, TypeError, OverflowError, struct.error) as exc:
        raise ValueError("扭矩不是有效 REAL") from exc
    if not math.isfinite(converted) or (original != 0 and converted == 0.0):
        raise ValueError("扭矩超出 REAL 范围")
    return converted
```

从标准库 `decimal` 导入 Decimal / InvalidOperation；保留原始数值是否为零的信息，防止文本在转换成 Python float 时已下溢而绕过 REAL 校验。

- [x] **Step 4: 跑 GREEN。** 重复该测试文件，应全部 PASS；新增模块不得导入 Qt、Snap7 或连接数据库。
- [x] **Step 5: 建立任务 checkpoint。** 精确 stage 本任务四个文件，commit message `tmp/sdd: torque contracts and validation`，按任务前 HEAD 进行双评审。

## Task 2: 扭矩表与配置的兼容数据库迁移

**Files:**
- Create: `model/plc_torque_config_table.py`
- Create: `model/torque_rule_table.py`
- Create: `app/service/torque/migration.py`
- Modify: `app/service/db_connection_service.py` (`connect`, `_migrate_compatible_schema`)
- Modify: `tests/conftest.py` (`ALL_TABLES` 和缺失的共享 qapp fixture)
- Modify: `tests/test_db_connection_service.py` (兼容新增可选表的成功用例断言)
- Test: `tests/torque/test_migration.py`

- [x] **Step 1: 写失败测试。** 在临时磁盘数据库中建原始必需表，运行 connect 后断言新增两个表、唯一索引、旧业务行保持、已有人工判定字段升级仍有效。覆盖“旧人工判定字段已存在但扭矩表缺失”、第二次启动不再备份 / 重建、备份失败不改结构、DDL 中途失败回滚，以及原有文件 / 必需表缺失仍报错。

先单独运行 tests/test_hardware_module.py 记录缺失 qapp 的既有 RED；在 tests/conftest.py 以 session fixture 提供 QApplication.instance() or QApplication([])，使用离屏环境，保留应用引用供整轮测试使用。不删除 / skip 现有测试，不安装新插件。修正后运行该文件与 tests/test_workorder_module.py 验证两个入口兼容。
- [x] **Step 2: 跑 RED。** `& $torquePython -m pytest tests/torque/test_migration.py tests/test_db_connection_service.py -q`；失败应定位到未创建的新表，而非真实数据库访问。
- [x] **Step 3: 实现两表 ORM 和迁移。** `plc_id` 用 IntegerField 主键；共享表 id 为 AutoField，match_type / match_key 为文本且非空，torque_value 为 DoubleField，复合唯一索引。配置列及缺省与 spec 一致；配置 revision 为进程内发布元数据，不添加未批准的持久化业务字段。

迁移必须按如下顺序，不能把新表提前加入原始必需表集合导致旧库无法升级：

```text
validate existing file and original required schema
discover pending compatible changes (existing manual-judgment change + torque tables)
if changes exist: make one SQLite backup
with db.atomic(): apply all pending compatible changes
validate original final schema and both torque schemas/indexes
```

`migration.py` 提供 `needs_torque_migration(db)`, `apply_torque_migration(db)`, `validate_torque_schema(db)`；缺表可新增，已有表结构错误须明确报错，不丢表重建。保留现有所有非扭矩兼容逻辑，移除的只能是会导致遗漏本次升级的过早 return。`:memory:` 测试由显式建表 fixture 管理，不能连接真实配置库。

- [x] **Step 4: 跑 GREEN。** 重复 Step 2；核对已有失败结构检查仍 PASS。把两个新 ORM 加入测试 fixture，并正确保持创建 / 删除顺序。
- [x] **Step 5: Commit。** 仅本任务列出的文件，`tmp/sdd: torque schema compatibility migration`；双评审重点检查备份与旧库保护。

## Task 3: 原子规则服务与 Excel 解析 / 预览

**Files:**
- Create: `app/service/torque/store.py`
- Create: `app/service/torque/excel.py`
- Modify: `app/service/torque/types.py` (导入行、错误、覆盖预览类型)
- Test: `tests/torque/test_store.py`
- Test: `tests/torque/test_excel.py`

- [x] **Step 1: 写失败测试。** store 的配置缺省关闭、规则唯一键、精确匹配、增删改、PLC 配置删除保留共享规则、提交失败数据库和内存快照均保持旧版本。测试导入时新增 / 覆盖预览、未包含行保留、重复行与错误阻止整批提交、预览后版本冲突。

Excel 使用 `tmp_path` 生成实际 xlsx：重排表头、文本 SN `000123`、数值型 SN 拒绝、长数字字符串保留、首尾空格、公式拒绝、布尔扭矩拒绝、无效 REAL、空行、半空行、重复键、读取第一个工作表、模板列 B 文本格式。固定 Excel 行号为 1 起始，表头为第 1 行。

```python
def test_preview_cannot_overwrite_unreviewed_changes(store):
    preview = store.preview_import([TorqueImportRow(2, "model", "M100", 2.5)])
    store.upsert_rule("model", "M100", 3.0)
    with pytest.raises(TorquePreviewConflict):
        store.commit_import(preview)
    assert store.snapshot().values[("model", "M100")] == 3.0
```

- [x] **Step 2: 跑 RED。** `& $torquePython -m pytest tests/torque/test_store.py tests/torque/test_excel.py -q`。
- [x] **Step 3: 实现 store 和 Excel。** store 接受明确 database 依赖，不偷偷创建库；启动加载配置与规则。用写入锁串行化事务及版本比较，构造新不可变 snapshot 后在短独立锁内发布；PLC 读 snapshot 只持短读锁，不能等待整个导入事务。数据库事务成功前不得发布新版本；准备 snapshot 出错也必须使事务回滚。

核心提交顺序：

```text
acquire mutation lock
verify preview revision == current rule revision
validate all rows, prepare next immutable snapshot
with database.atomic(): upsert all rows
publish prepared snapshot with revision+1
release mutation lock
notify caller after success
```

`preview_import(rows)` 同时读取一份固定 revision 和旧 values，返回不可变预览。配置 mutation 和规则 mutation 可共用写锁，规则 revision 仅在规则发生变化时递增。CRUD 与导入共用规范化 / 校验；DB 持久化重新打开后恢复同样的 float32 值。

Excel `parse_workbook(path)` 用 `load_workbook(read_only=True, data_only=False)`，finally 关闭 workbook；拒绝公式而不使用缓存。接受 .xlsx 后缀，坏 zip / 无表 / 错表头产生可显示错误，不提交任何内容。`write_template(path)` 写首行及设计例子、列 B number_format `@`、单位说明，模板可被同一个 parser 正常导入。不要新增依赖或提供 XLS 转换。

- [x] **Step 4: 跑 GREEN。** `& $torquePython -m pytest tests/torque/test_validation.py tests/torque/test_store.py tests/torque/test_excel.py -q`；事务回滚和版本冲突必须有实际文件 SQLite 测试，不只 mock commit。
- [x] **Step 5: Commit。** 精确 stage 本任务文件，`tmp/sdd: torque store and Excel import`，双评审。

## Task 4: 独立扭矩处理器与 PLC 开始链路

**Files:**
- Create: `app/service/torque/runtime.py`
- Modify: `app/service/plc_service.py` (`__init__`, `poll_once`, 新配置 slot / REAL I/O)
- Test: `tests/torque/test_runtime.py`
- Test: `tests/torque/test_plc_ingress.py`
- Modify: `tests/test_plc_service_workorder.py` (开始复位异常边界断言，保留现有工单断言)

- [x] **Step 1: 写失败测试。** 假 I/O 记录顺序，要求开始提交、复位尝试发生在扭矩读取之前；读 0 命中 2.5 时，仅回传地址写一次正确 4 字节；读非零 3.0 时表有 / 无记录都不写，连续两台读取同样 3.0 都有效。

```python
def test_nonzero_plc_value_remains_authoritative_across_starts(runtime, cycle):
    writes = []
    events = []
    for trigger_ns in (100, 200):
        runtime.process(
            replace(cycle, trigger_ns=trigger_ns),
            read_real=lambda db, offset: 3.0,
            write_real=lambda *args: writes.append(args),
            publish=events.append,
        )
    assert writes == []
    assert [(e.trigger_ns, e.value) for e in events if e.state == "plc"] == [(100, 3.0), (200, 3.0)]
```

其他用例：负非零、微小非零、正负零、表值零；读异常 / 短字节 / NaN / Inf 不回退；匹配空键 / miss；写异常显示已查到值但失败；启用地址冲突不 I/O；功能关闭不新增网络调用。初始高位 / 持续高位不触发，开始回调抛异常不写扭矩，复位异常但开始成功仍执行扭矩边界。

- [x] **Step 2: 跑 RED。** `& $torquePython -m pytest tests/torque/test_runtime.py tests/torque/test_plc_ingress.py -q`。
- [x] **Step 3: 实现完整处理算法。** `TorqueRuntime.process(cycle, *, read_real, write_real, publish)` 返回最终 TorqueStatus，中间发布 reading / table_pending；函数只操作固定 cycle。主要分支如下，每个外部失败转为对应具体状态：

```text
resolve config addresses, or publish config_error
publish reading
read once, or publish read_error
reject nonfinite, publish invalid_value
if read_value != 0.0: publish plc(read_value); return
key = normalized cycle.model or cycle.sn according to config.match_type
if key absent in cycle.rules: publish unmatched; return
value = cycle.rules.values[(match_type, key)]
publish table_pending(value)
write once, or publish write_error(value)
publish table_sent(value)
```

PlcService 添加可选 `torque_store=None`、`torque_config=None`、`service_generation=None` 构造参数，旧构造调用仍可运行。`apply_torque_config(object)` 是 QObject slot，通过排队连接在所属线程应用，过滤 plc_id；generation 不匹配的回执由 UI 丢弃。用 service 自身 signal 发布 TorqueStatus 及配置回执，未接 UI 时也可测试。

`_read_torque_real(db_number, offset)` 直接 `db_read(db_number, offset, 4)`，严格长度 4 后 `snap7.util.get_real`；`_write_torque_real` 用 bytearray(4)、`set_real`、`db_write`。显式 DB 参数，严禁修改 `_db_number` 或加入 `read_plc_values` 的默认值路径。

`poll_once` 保留电流采样、原工单读取和原 trigger timestamp。开始提交前只取内存规则 / 配置快照和工单字符串；提交后执行原复位尝试，复位异常独立记录，随后执行扭矩。旧测试使用 `PlcService.__new__` 创建最小实例，需要按新依赖补齐 fixture 或通过单一默认关闭入口兼容，不能散布吞异常的 hasattr 适配。现有开始回调返回 None 不代表拒绝，沿用当前提交语义。

网络错误无需重试旧请求；Snap7 客户端的既有连接 / 读写超时继续限制单次请求。不得引入新循环等待、忙等或让 UI / 录音线程等待扭矩。

日志由 PlcService 的状态发布包装函数集中记录；纯 runtime 提供状态和诊断数据，不依赖全局 logger。每次最终状态记录 plc_id、trigger_ns、cycle 的型号 / SN、规则 revision、实际读取值（未读到用 None）、最终来源、已解析回传 DB / 偏移、待写或已写值、状态及错误原因。非零 PLC 分支也记录读取值和来源，未发写请求时回传值为 None；读失败不能把读取值记成 0。错误状态既发 UI 信号也发已有日志通道，不弹模态框。用日志捕获测试核对 PLC 优先、表成功、未匹配、读取失败、写失败和配置错误六个最终分支具备上述上下文，不只断言调用过 logger。

- [x] **Step 4: 跑 GREEN 和 PLC 回归。**

```powershell
& $torquePython -m pytest tests/torque/test_runtime.py tests/torque/test_plc_ingress.py tests/test_plc_service_workorder.py tests/test_plc_service_current.py tests/test_plc_service_shutdown.py tests/test_plc_service_scheduling.py -q
```

- [x] **Step 5: Commit。** `tmp/sdd: PLC torque processing after test start`，双评审明确核对现有 dirty baseline 中的电流和调度代码仍在。

## Task 5: 后台作业、运行配置应用与应用装配

**Files:**
- Create: `app/service/torque/jobs.py`
- Modify: `app/service/torque/store.py` (PLC 与扭矩配置的协调删除事务)
- Modify: `app/service/plc_service.py` (配置应用入口拒绝迟到的较旧 revision)
- Modify: `tests/torque/test_plc_ingress.py` (关闭功能用例使用新保存配置的递增 revision，保留原断言)
- Modify: `app/services_container.py` (初始化、bus 信号、shutdown)
- Modify: `ui/modules/plc_settings.py` (`start_connect`, PLC 删除 / 断开)
- Modify: `tests/multi_run_integration/test_shutdown_order.py` (新增后台作业关闭的必要断言)
- Test: `tests/torque/test_jobs.py`
- Test: `tests/torque/test_composition.py`
- Test: `tests/torque/test_threading.py`
- Modify: `tests/torque/test_store.py` (协调删除回滚与缓存发布)

- [x] **Step 1: 写失败测试。** 用实际 QThread + 假 PLC 验证：配置保存和 Excel 解析不在 UI 线程、Snap7 调用仅在 PLC 所属线程；新 store 规则成功提交后无需重连，下个 cycle 用新 revision，已固定 cycle 保留旧 snapshot；配置排队应用有回执，断开时保存显示待应用，连接时带入最新配置。

测试并发提交冲突、后台出错清理连接、窗口关闭后 job 完成、应用关闭等待 / 取消作业但不销毁运行线程、删除 PLC 后不残留配置。文件型临时 SQLite 供跨线程访问，不使用每线程独立的 `:memory:` 数据库伪造成功。

- [x] **Step 2: 跑 RED。** `& $torquePython -m pytest tests/torque/test_jobs.py tests/torque/test_composition.py tests/torque/test_threading.py -q`。
- [x] **Step 3: 实现应用持有的作业控制器。** 单个专用 QThread 和 worker QObject 串行处理 store 变更 / Excel / 模板文件 I/O，使用显式 Qt 排队信号发送请求和结果，每个请求有 ID，UI 按请求 ID 关联。解析和预览不提交数据；写入提交是另一个明确请求。worker 使用 Peewee 的线程本地 connection_context，作业结束关闭该线程连接，不关闭主线程连接。

应用容器在数据库兼容初始化后构造 store 和 jobs，不要求 PLC 已连接。关闭时停止接受新请求；未执行的可取消，已进入事务的操作须完成提交或回滚，再退出 worker / 关闭数据库；不能 terminate QThread。解析 workbook 的循环响应取消。界面关闭仅解除该界面的回调，jobs 由容器持有，防止 QThread 仍运行却被弹窗析构。

初始化时加载 store；每次 `start_connect` 给 PlcService 传最新 config、共享 store 和新的 generation。将 service torque result 转发到 bus 的 `plc_torque_changed`（object），把 `torque_config_applied` 转发到 UI 状态。store 配置持久化成功后向对应仍存在的 service 排队应用；断开则只记待应用，重连应用最新配置。退出 / 重连后迟到的旧 generation 回执不得标记新实例已应用。

已确认的评审修正：较早 save_config 的完成通知若晚于较新配置持久化或重连，不能把旧配置派发到新实例。coordinator 需过滤已过期的保存完成通知；PlcService 所属线程的 apply_torque_config 也拒绝比当前已应用 revision 更小的配置，保留同 revision 的连接回执。用“新实例已加载 revision 2，迟到 revision 1 完成通知 / 已排队应用到达”的实际 Qt 测试证明运行配置不会倒退，而非仅过滤显示回执。

PLC 删除复用原来的断开 / 删除流程，在同一数据库删除边界删除扭矩配置；共享规则保持。若已排队该 PLC 的配置作业，作业必须重新验证 PlcTable 记录存在，不重新创建已删除 PLC 的扭矩设置。删除时清除其配置缓存并通知界面。

实现衔接说明：Task 3 的 store mutation 必须拥有外层事务，因此不能在 UI 开启事务后嵌套调用 delete_config。Task 5 在 store 增加协调删除操作，将原 PLC 偏移行、扭矩配置行、PLC 行的删除放在同一 mutation lock 和外层事务中，提交后发布失效配置缓存；由后台 job 调用，错误回滚全部删除。删除开始后取消尚未执行的该 PLC 配置请求，防止删除完成后旧请求仍尝试保存。该调整用于落实既有原子删除需求，不扩展业务范围。

- [x] **Step 4: 跑 GREEN。** 重复 Step 2，再运行 `tests/test_plc_service_shutdown.py` 及 `tests/multi_run_integration/test_shutdown_order.py`；只调整与新增关闭作业相关的预期，保留现有资源关闭语义。
- [x] **Step 5: Commit。** `tmp/sdd: torque jobs and PLC configuration lifecycle`，双评审。

## Task 6: 扭矩设置、规则管理与 Excel 导入界面

**Files:**
- Create: `ui/popups/torque_settings_dialog.py`
- Create: `ui/popups/torque_table_dialog.py`
- Modify: `ui/modules/plc_settings.py` (按钮、打开弹窗、状态刷新)
- Test: `tests/torque/test_settings_dialog.py`
- Test: `tests/torque/test_table_dialog.py`

- [x] **Step 1: 写失败测试。** 使用真实 QWidget + 假 jobs 请求记录器，覆盖每个 PLC 的开关 / 匹配依据 / 两地址、关闭允许空地址、启用缺地址 / 重叠拒绝、无倍率字段、保存不发 PLC 写入。覆盖管理表 CRUD / 搜索、模板下载路径、导入预览新增 / 覆盖新旧值 / 错误行、错误阻止提交、取消不变、冲突要求重新预览。
- [x] **Step 2: 跑 RED。** `& $torquePython -m pytest tests/torque/test_settings_dialog.py tests/torque/test_table_dialog.py -q`。
- [x] **Step 3: 实现两个弹窗。** 扭矩设置包含启用、型号 / SN、两个 DB 和偏移（继承 DB 的提示）、REAL 固定说明、“与 PLC 单位一致”、管理表入口及保存 / 应用状态。保存按钮先做纯校验再提交 job，持久化成功与工作线程应用成功分别显示，生产流程不需要打开弹窗。

共享表用 QTableWidget / QTableView 按现有 Qt 样式实现，不引入新 UI 框架。界面仅提交后台请求，解析、保存模板和 DB mutation 不在 UI 主线程执行。预览用独立表格展示行号、匹配类型、键、旧值、新值、动作 / 错误；存在任一错误或预览已失效时禁用确认导入。窗口关闭可以取消未提交预览；已提交事务最终成功 / 失败由 jobs 统一管理。

PLC 设置按钮区改为可容纳新增“扭矩”按钮的布局（必要时两行），避免沿用固定宽度导致窄窗口挤出。访问沿用硬件页现有权限边界，不另建用户角色或生产弹窗。

- [x] **Step 4: 跑 GREEN。** 重复 Step 2；用离屏窗口检查最小尺寸下输入框和按钮可见、导入长错误列表可滚动。断言可观察的字段 / 按钮行为，不给颜色或像素位置编写脆弱测试。
- [x] **Step 5: Commit。** `tmp/sdd: torque configuration and Excel import dialogs`，双评审。

## Task 7: 工单区域扭矩显示与多 PLC 事件关联

**Files:**
- Modify: `ui/modules/workorder.py`
- Test: `tests/torque/test_workorder_torque.py`
- Modify: `tests/test_workorder_module.py` (必要时扩展 bus fixture)

- [x] **Step 1: 写失败测试。** 每个 PLC 的型号下新增只读扭矩行；未配置关闭 / 开启等待读取；PLC 非零显示来源；软件表已回传 / 回传失败显示本次匹配值；读取失败、无效数值、配置错误与未匹配显示文字且无旧成功值。

```python
def test_late_outcome_cannot_replace_newer_plc_cycle(module, emit_status):
    emit_status(plc_id=7, trigger_ns=200, state="reading")
    emit_status(plc_id=7, trigger_ns=100, state="table_sent", value=2.5)
    assert module.torque_text(7) == "读取中"
    emit_status(plc_id=8, trigger_ns=100, state="plc", value=3.0)
    assert module.torque_text(7) == "读取中"
    assert "3" in module.torque_text(8)
```

测试 1 / 2 / 3 个 PLC、删除 / 重连、更新 SN 型号仍保持、开始下一 cycle 清除旧显示。上述 `torque_text` 可在测试 fixture 中读取实际控件，不为测试强加生产公共方法。

另外从上一事件 `table_sent` 开始，直接投递较新事件的 `config_error`（该分支不经过 reading），必须显示新配置错误且清除旧成功值；随后较旧 reading / table_sent 均不能覆盖它。

- [x] **Step 2: 跑 RED。** `& $torquePython -m pytest tests/torque/test_workorder_torque.py tests/test_workorder_module.py -q`。
- [x] **Step 3: 接入现有工单布局。** 增加 `_plc_torque_edits`、每个 PLC 最新状态缓存及最新事件标识。每一种 TorqueStatus 都参与标识登记：旧于该 PLC 最新 cycle_id 的消息丢弃；新于最新标识则先清除旧显示并登记，再渲染该消息；相同标识继续渲染。旧直接构造测试未提供 cycle_id 时才回退 trigger_ns，真实路径使用 Task 8 修正后的独立 ID。不能只由 reading 登记，因为 config_error 可作为本次首个且唯一事件。只接受当前列表存在的 PLC；不能把 UI 事件过滤条件用于否定 PLC 的非零值。

新工单到来时清除上一轮显示结果；即使功能已关闭、不会再发 TorqueStatus，也应显示未启用。保存配置本身不改写已有当前 cycle 的结果。新工单重置后保留上一事件标识水位，拒绝小于或等于该水位的旧状态，直到接到严格更新的事件；接受新事件后允许该事件的中间 / 最终状态。保留既有 SN / 型号区域处理删除 PLC 的兼容行为，但独立忽略实际已不存在 PLC 的扭矩更新并清除对应扭矩缓存。

数值格式只影响显示，不在显示层重新参与 PLC 写入。按 spec 显示状态、来源及必要匹配详情，不假定 N·m；详细错误留 tooltip / 日志。增加行后复用 `_update_plc_scroll_policy`，两台以内完整显示，更多 PLC 可滚动。配置状态更新只影响未开始或已关闭显示，不用普通页面刷新发出扭矩请求。

- [x] **Step 4: 跑 GREEN。** 重复 Step 2，检查所有原工单字段和多 PLC 排列测试仍 PASS。
- [x] **Step 5: Commit。** `tmp/sdd: display torque source and outcome per PLC`，双评审。

## Task 8: 完整流程验收、回归与交付说明

端到端验收确认的相关修正：本地 Windows Python 3.12 的 monotonic 使用 GetTickCount64，分辨率 15.625 ms，且 PLC 轮询配置未禁止更短间隔。不能把 trigger_ns 唯一性作为扭矩显示去重前提。为 TorqueCycle / TorqueStatus 添加独立 cycle_id，由 PlcService 的进程内共享单调计数器在每次扭矩开始时分配，跨 PLC 实例重建不重置；运行消息使用该 ID 做工单 UI 关联，原始 trigger_ns 完全保持。可提供默认 None 以兼容既有直接构造测试，None 时才使用旧 trigger_ns 关联；真实 PLC 路径必须提供独立 ID。Task 8 负责 types.py、runtime.py、plc_service.py、workorder.py 和对应回归的最小修正，并加入连续真实处理使用相同 monotonic 时间戳仍正确显示的端到端用例，不能仅修改测试时钟掩盖问题。

**Files:**
- Create: `tests/torque/test_end_to_end.py`
- Create: `docs/torque-table-operator-guide.md`
- Modify: 本计划（完成状态与验证证据）
- Fix only if proven necessary: 任务 1–7 声明的相关生产文件；发现缺陷时走 implementer / 双评审，不由 orchestrator 自行改代码。

- [x] **Step 1: 写端到端失败测试。** 使用临时旧库升级、真实 xlsx 导入、store、假 PLC client、实际 Qt 信号和工单控件贯通：表 M100=2.5 + PLC 0 → 正确地址 REAL 写入并显示成功；PLC 3.0 → 不写且多台持续有效；读取失败 / 未匹配 / 写失败 → 原开始事件仍已提交，状态明确；测试运行期间导入新值仅影响下次 cycle。
- [x] **Step 2: 跑测试并确认失败理由。** `& $torquePython -m pytest tests/torque/test_end_to_end.py -q`。若实现已正确而测试直接 PASS，保留集成验证结果，不人为制造生产缺陷来凑 RED；只有暴露缺陷时补针对性 RED 并最小修复。
- [x] **Step 3: 完成必要修复与用户说明。** 说明两个地址、读取零 / 非零规则、Excel 模板列、导入覆盖预览、状态含义、现场单位一致、异常不阻断声学测试；不得写“必须每台清零”或“软件判断上一台残留”。说明已回传为通讯成功而非 PLC 执行动作确认。
- [x] **Step 4: 完整验收和适当回归。**

```powershell
& $torquePython -m pytest tests/torque tests/test_plc_service_workorder.py tests/test_plc_service_current.py tests/test_plc_service_shutdown.py tests/test_plc_service_scheduling.py tests/test_db_connection_service.py tests/test_workorder_module.py tests/multi_run_integration/test_shutdown_order.py -q
& $torquePython -m pytest tests/multi_run_integration/test_production_composition.py tests/test_hardware_module.py tests/test_main_startup.py -q
git diff --check
```

测试须报告准确结果和与执行准备基线的差异，不能把预先存在的失败宣称为本功能通过。对新增任务做无真实 PLC 的界面检查；若使用截图，只以临时数据库和 fake services 打开专门窗口，不启动完整生产应用。本任务不要求重新打包整套带 native 音频环境的发布程序；现有 openpyxl 显式导入可由打包工具发现，只有实际出现打包依赖缺失证据才改 `main.spec`。

- [x] **Step 5: Commit 与全范围评审。** `tmp/sdd: torque end-to-end verification and usage guide`；文档在 .gitignore 中，需要对本次文档显式 `git add -f -- <exact-path>`。完成任务双评审后，再对 `FEATURE_BASE..HEAD` 发起完整质量评审，运行 `verification-before-completion`，记录未连接真实 PLC 的验证边界。

## 最终集成保护

- [ ] 使用 `finishing-a-development-branch` 处理最终集成。主工作区包含其他人未提交代码，不得清理、stash 覆盖、reset 或把已有修改混入本功能提交。执行过程中主工作区可能继续变化，集成前重新检查所有受影响文件及基线元数据。

- [ ] 只导出 `FEATURE_BASE..HEAD` 的功能差异到文件，先对主工作区当前内容做 apply check。禁止将 worktree 的整个文件直接覆盖主工作区来解决冲突，也禁止把保存已有改动的 baseline checkpoint squash 进主分支。

```powershell
git diff --binary --output=<feature-patch-path> <FEATURE_BASE> HEAD -- app model ui tests docs
git -C <main-worktree> apply --check <feature-patch-path>
```

- [ ] 检查通过后按授权的最终集成方式应用功能 patch；有冲突时交给 implementer 在主工作区当前上下文中解决，按同一范围双评审，保留他人改动。所有变量和占位路径来自执行准备的元数据，不猜测。
- [ ] 集成后运行 Task 8 的相关验证命令，对照源文件快照核对他人改动没有丢失，记录实际结果。
- [ ] 审查暂存差异后再提交。主工作区原有脏改动若导致无法单独 stage 功能差异，先保留完整集成后的工作区状态，按最终集成流程呈现具体可审查差异，不能为了“单一提交”把用户其他修改也提交。临时 checkpoint 保留在 feature 分支，不创建含临时提交的普通 merge，不因主分支进展而回退主分支。

## 验收对应关系

| Spec 行为 | 任务与主要验证 |
| --- | --- |
| REAL 直接数值、零与非零准确分支 | 1、4；validation / runtime / plc_ingress |
| 非零持续有效，无逐台模式选择或残留判断 | 4、6、8 |
| 每个 PLC 的读取 / 回传地址与配置生效 | 1、4、5、6 |
| 型号 / SN 精确查表、共享持久化 | 2、3 |
| Excel 校验、预览、全有全无提交 | 3、5、6、8 |
| 失败仍继续测试、保持原触发与复位 | 4、8；PLC 现有回归 |
| 显示扭矩来源、状态和多 PLC 隔离 | 7、8 |
| 兼容迁移、备份、关闭默认、旧数据保持 | 2、5、8 |
| 无 UI / 录音线程网络访问，后台导入 | 4、5、6 |

完成计划编写及 plan-document-reviewer 评审后，按 writing-plans 的执行交接规则请用户确认是否开始执行；获得执行授权后连续推进所有任务，不在每个任务之间反复要求确认。

## 执行记录（2026-09-10）

- 用户已确认开始实施；Task 1–8 均完成，并分别通过 spec-code-reviewer 和 quality-code-reviewer。
- 主分支：master_0811；执行前 MAIN_BASE：f50391cb0076496154ca0a845863a3dcacf7b3ea。
- 功能分支：codex/plc-torque-table；隔离目录：.worktrees/plc-torque-table。
- 90 个已有改动及未跟踪源码的保存基线 FEATURE_BASE：a720ade12fd884aab2f7523c2c9aa497c8114855。它只用于保留现场，不能作为功能提交集成。
- 最终代码范围：a720ade12fd884aab2f7523c2c9aa497c8114855..2a276619c911e7f0786d5010d4fb542e59ba9479；全范围质量审查 PASS，无待修问题。
- 根代理使用 Task 8 列出的全部测试文件进行一次合并运行：575 passed, 1 skipped，退出码 0；全范围 git diff --check 通过。完整质量审查另行运行 tests/torque：294 passed。
- 8 项端到端测试贯通临时旧库升级、真实 xlsx、后台任务、PLC 字节传输和工单 UI；其中覆盖相同原始时间戳和重连后的独立事件 ID。
- 扭矩设置、共享表、导入预览、双 PLC 工单行截图均已检查；主工作区 outputs/plc-torque-table 保存截图及经过回读验证的 torque-template.xlsx。
- 操作说明：docs/torque-table-operator-guide.md。
- 2026-09-11 用户同意合并回主分支，38 个功能文件已应用到 master_0811 工作区。整合后完整验收为 575 passed, 1 skipped；87 个无关已有文件哈希保持不变。
- 6 个共享文件原有的未提交改动与功能重叠，涉及 PLC / 数据库服务、容器及其测试（186 行新增、47 行删除）。按本计划的集成保护规则暂未创建 Git 提交，等待确认这些已有改动是否一并纳入。功能分支和 worktree 保留，生产数据库未修改，实际 PLC 联调和发布打包未执行。
