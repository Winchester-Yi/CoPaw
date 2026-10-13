# Token Core Diagnosis B Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use `superpowers:executing-plans` to execute this plan task-by-task in the current feature workspace. `superpowers:subagent-driven-development` is an alternative when the user selects delegation. Track the checkboxes; complete each dependency before its consumer.

**Goal:** 在现有 SWE/Monitor 独立 Token 记账上交付上下文片段、文本前缀证据、八条可解释诊断规则和三个只读 Console 页面。

**Architecture:** SWE 只在已有运行时边界采集有界元数据及 HMAC，经独立异步队列落库。Monitor 在租约及修订保护下比较历史前缀、执行规则，保存不可变评估历史和当前结果。三页面读取作用域内持久化投影，业务调用和 HTTP 读请求均不临时运行分析。

**Tech Stack:** Python、pytest、FastAPI、aiomysql/MySQL，React/TypeScript、Ant Design、Less Modules、Vitest。

---

## 执行依据及工作区边界

- 已批准设计：[核心诊断 B 完整设计](../specs/2026-10-12-token-diagnostics-core-diagnosis-design.md)。线框：[三页 v3](../mockups/2026-10-12-token-diagnostics-b-v3.html)。Console 视觉以 `console/DESIGN.md` 为准。
- 本文件是实施计划。生成计划不表示已经完成 B 开发、建表、真实 MySQL 验证或页面联调。
- 继续使用 `token_optimize` 工作区，因为 A 实现还包含未提交及未跟踪文件。禁止从 HEAD 创建一个遗漏 A 的工作树，禁止清理/覆盖 `AGENTS.md`、`CLAUDE.md` 或用户根目录文档。若以后需要隔离，必须先完整保留 A 及设计文件，再创建工作树。
- 用户已授权小步本地提交和子代理代码检视。每次提交前完成必要测试与 GitNexus detect-changes；不执行 push、部署、业务数据库写入或真实 Provider 调用。
- 人工反馈、实验工作台、完整策略库、工具安全声明机制、Agent 基线学习、专项 Usage 核对、真实成本及自动整改均不在 B。
- 不用一般属性袋替代类型化列，不增加基础设施服务，不改写旧五规则或其历史结论。B 的低效候选不代表浪费、已确认共同根因或安全删除。

## 文件责任与依赖

| 阶段/任务 | 新增文件 | 既有接入文件 |
| --- | --- | --- |
| 1 版本及契约 | SWE `src/swe/token_diagnostics/context_contract.py`；Monitor `monitor/src/monitor/app/services/token_diagnostics/diagnostic_contract.py`；`tests/unit/token_diagnostics/conftest.py` | SWE `contract.py`、`config.py`，两侧 `storage_schema.py` |
| 2 有界文本表示 | SWE `context_capture.py` | `input_capture.py`、`provenance.py` |
| 3 初始 DDL/事务 | SWE `context_storage.py`；Monitor `diagnostic_storage.py` | `scripts/sql/token_diagnostics_tables.sql`，两侧 `store.py`/`storage_schema.py` |
| 4 采集接入 | SWE `context_operations.py` | `model_observer.py`、`collector.py`、`writer.py`、`runtime.py`、`context.py`、`src/swe/agents/hooks/memory_compaction.py` |
| 5 前缀比较 | Monitor `prefix_analysis.py` | `diagnostic_storage.py` |
| 6 规则 | Monitor `diagnostic_rules.py`、`recommendations.py` | `diagnostic_contract.py`；旧 `rules.py`不改语义 |
| 7 后台集成 | Monitor `diagnostic_analysis.py` | `analysis.py`、`store.py`、`lifecycle.py` |
| 8 只读投影 | Monitor `diagnostic_queries.py`、`diagnostic_filters.py` | `query_service.py`、`lifecycle.py` |
| 9 HTTP | `monitor/src/monitor/app/routers/token_diagnostics_v2.py` | `routers/__init__.py`；复用旧路由 scope/read helpers |
| 10 客户端 | `console/src/api/modules/tokenDiagnosisV2.ts`；`console/src/pages/Analytics/TokenDiagnosisShared/{presentation.ts,useDiagnosisResource.ts,FilterBar.tsx,EvidenceState.tsx,index.module.less}` | 复用 `console/src/api/request.ts`，不改变旧 API 模块 |
| 11 看板 | `console/src/pages/Analytics/TokenOverview/{index.tsx,index.module.less,index.test.tsx}` | Analytics 路由/导航 |
| 12 执行页 | `console/src/pages/Analytics/TokenExecutions/{index.tsx,index.module.less,index.test.tsx}` | Analytics 路由/导航 |
| 13 问题页 | `console/src/pages/Analytics/TokenIssues/{index.tsx,index.module.less,index.test.tsx}` | Analytics 路由/导航 |
| 14 验收 | 测试证据记录 | `docs/deploy/token-diagnostics.md`、`analysis/playbook/token-diagnostics.md`、本计划 |

表中 SWE 短路径均相对 `src/swe/token_diagnostics/`；Monitor 短路径均相对 `monitor/src/monitor/app/services/token_diagnostics/`。下面任务列出测试的完整路径。

顺序为 1 → 2 → 3 → 4 → 5 → 6 → 7 → 8 → 9 → 10 → 11/12/13 → 14。三页面可以分别完成，但不能使用未经定义的 mock 契约替代 9/10 的实际接口。

## 通用实施循环

每个任务均执行以下循环，并把实际结果写到任务记录中：

1. 针对被修改的既有符号执行 upstream impact，报告 direct callers/processes/risk；HIGH/CRITICAL 先向用户说明具体风险。UNKNOWN 需补文本确认，不能依据空 caller 集编辑。
2. 先写下面定义的行为测试，运行该任务命令，确认因缺失功能或行为不符而失败；环境/import 配置错误不是有效红灯。
3. 按本任务合同完成最小实现；普通函数参数不超过 7，复杂度建议不超过 15，不重构无关代码。
4. 同命令变绿，再运行列出的既有回归；保留真实命令/输出，不预填“已通过”。
5. 检查 diff，每行可追溯到本任务；测试及 diff 可审阅后才勾选任务。

现有符号 gate 命令示例（必须使用当时实际文件/符号）：

```bash
node .gitnexus/run.cjs impact AttemptObserver --direction upstream --repo .
node .gitnexus/run.cjs impact DiagnosticWriter --direction upstream --repo .
node .gitnexus/run.cjs impact MemoryCompactionHook --direction upstream --repo .
node .gitnexus/run.cjs impact Analyzer --direction upstream --repo .
```

图先用于探索，再读源文件。动态类型/回调缺口不表示没有影响。计划编写阶段不修改上述生产符号，因此这些是执行时检查，不是已完成的风险检查。

## Task 1：冻结 B 契约、状态与服务端预算

**Files:** 新建 `context_contract.py`、`diagnostic_contract.py`；修改 SWE `contract.py`、`config.py`。测试 `tests/unit/token_diagnostics/test_b_contract.py`。

- [ ] 添加契约测试：接受事件 1.0/1.1，拒绝其他版本；拒绝不支持的状态；B disabled 时继续产生 A 事件 1.0；启用时新证据为 1.1。
- [ ] 在 Token 子系统 `conftest.py` 注册 Monitor 源路径，使新增测试统一可导入 Monitor；不移动或改写既有测试的导入行为。

```python
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[3]
MONITOR_SOURCE = str(ROOT / "monitor" / "src")
if MONITOR_SOURCE not in sys.path:
    sys.path.insert(0, MONITOR_SOURCE)
```

- [ ] 冻结下列常量；旧 `SCHEMA_VERSION` 的调用点逐一检查，不能把读旧数据支持删掉。增加独立 `SWE_TOKEN_DIAGNOSTICS_CONTEXT_ENABLED`，默认 false，且只在既有总采集启用时有效。部署先更新 Monitor/DDL，最后开此开关。

```python
B_SCHEMA_VERSION = "1.1"
SUPPORTED_SCHEMA_VERSIONS = frozenset({"1.0", "1.1"})
REPRESENTATION_VERSION = "client-ordered-text-v1"
PREFIX_BLOCK_BYTES = 256
CONTEXT_MAX_BYTES = 256 * 1024
CONTEXT_MAX_BLOCKS = 1024
CONTEXT_CAPTURE_SECONDS = 0.005
CANDIDATE_LIMIT = 100
HISTORY_WINDOW_HOURS = 24
RULE_VERSION = 1
THRESHOLD_VERSION = 1
EVALUATION_STATES = frozenset({
    "candidate", "not_hit", "insufficient_evidence", "not_applicable",
})
RULE_IDS = (
    "context_growth", "segment_carryover", "tool_context_volume",
    "safe_tool_repeat", "suspected_loop", "text_prefix_stability",
    "static_context_volume", "cohort_usage_outlier",
)
```

- [ ] 定义明确的 DTO（dataclass 或 TypedDict）：`CapturedContext` 具有 `context_id, context_role, representation_version, estimator_version, key_version, capture_state, capture_reason, text_bytes, encoded_bytes, expected_item_count, occurrences, blocks`；`PrefixBounds` 具有 `lower_text_bytes, upper_text_bytes, first_difference_index`；`Evaluation` 具有 `evaluation_id, rule_id, rule_version, threshold_version, input_revision, snapshot_id, state, values, gaps, refs, findings`。未知值用 None，禁止 raw_text/prompt/arguments/result 正文字段。
- [ ] 为后续任务冻结 helper 合同：SWE `capture_context(messages, tools, scope, config, *, context_id, context_role)`；Monitor `compare_prefix(current, historical)`、`aggregate_prefix(comparisons)`、`evaluate_context_rules(snapshot)`。上述均在对应任务定义，不能在 Task 1 写空实现。
- [ ] 配置边界测试使用 `dataclasses.replace(config, context_enabled=True)`，确认负数、无效字符串、超大上限不扩大已批准预算。队列及 retention 仍沿用 A 默认值。

```python
def test_rule_ids_do_not_replace_legacy_rules():
    from monitor.app.services.token_diagnostics.diagnostic_contract import RULE_IDS
    assert len(RULE_IDS) == len(set(RULE_IDS)) == 8
    assert set(RULE_IDS).isdisjoint({
        "repeated_tool_result", "duplicate_injection", "repeated_tool_call",
        "history_growth", "output_budget",
    })
```

**Run:** `venv/bin/python -m pytest tests/unit/token_diagnostics/test_b_contract.py tests/unit/token_diagnostics/test_foundation_runtime.py -q`。完成时预期通过，旧事件/身份语义不变。

## Task 2：有界输入表示、来源实例及 HMAC 块

**Files:** 新建 SWE `context_capture.py`；修改 `input_capture.py`、`provenance.py`；测试 `tests/unit/token_diagnostics/test_context_capture.py`。

- [ ] 写测试覆盖工具数组顺序、消息角色、文本空白/Unicode、Schema 键顺序规范化、不同来源相同内容、缺密钥、媒体/未知块、短尾块和 256KiB/1024 块边界。
- [ ] 实现增量表示迭代器：工具条目在前，消息/块在后；Schema 对象键固定排序，数组不排序；保留文本原样。结构边界使用固定标签和无歧义转义编码；编码按小块产出，不先对整个大对象调用 json.dumps/encode。
- [ ] occurrence 保存 `source_object_id`/`source_version`（可得时）、来源类别、content/structure digest、原点引用、顺序及估算/字节区间。没有可靠身份时 source_object_id 为 None，作用域 source_id/tenant_id 始终非空；消息位置不得作为跨轮稳定身份。HMAC 查找只校验内容，不能仅因摘要相同继承另一个来源身份。
- [ ] 链式 HMAC 计入 scope/key/representation/index 和前块摘要，实际消息/操作 ID 不计入内容指纹。每块保存编码起止及文本累计起止，块大小含结构标记，比例只用文本字节。
- [ ] 实现 deadline/长度/条目预算，每块前检查，不遍历剩余输入补总数；`expected_item_count=None` 在总数不可得时合法。超限返回 partial 和具体 reason，不能伪装 complete。原输入不被修改，原文不进入返回值或日志。

```python
def test_same_text_does_not_prove_same_source():
    from swe.token_diagnostics.context_capture import capture_context
    from swe.token_diagnostics.config import DiagnosticConfig
    from dataclasses import replace
    cfg = replace(DiagnosticConfig(), fingerprint_key="test-only-key", context_enabled=True)
    result = capture_context(
        [{"role": "user", "content": "PRIVATE_SENTINEL"}], [],
        ("source", "tenant"), cfg,
        context_id="ctx-1", context_role="prepared",
    )
    assert result["occurrences"][0]["source_object_id"] is None
    assert "PRIVATE_SENTINEL" not in repr(result)
    assert result["capture_state"] == "complete"
```

**Run:** `venv/bin/python -m pytest tests/unit/token_diagnostics/test_context_capture.py tests/unit/token_diagnostics/test_capture.py -q`。预算测试注入可控时钟，不能依靠机器速度形成随机红灯。

## Task 3：全量初始 DDL 和独立两侧存储合同

**Files:** 两侧 `storage_schema.py`，SWE `context_storage.py`/`store.py`，Monitor `diagnostic_storage.py`/`store.py`，`scripts/sql/token_diagnostics_tables.sql`。测试 `tests/unit/token_diagnostics/test_b_storage.py`、既有 `test_relational_storage.py`。

- [ ] 先扩展 schema/parity 测试：A 11 表保持，新增 13 表，下列集合全部存在；PK、列/表注释、显式 SELECT、无禁用类型/外键/视图。两侧 COLUMNS/DEFAULTS 完全一致。

```python
B_TABLES = {
    "swe_token_contexts", "swe_token_context_occurrences",
    "swe_token_prefix_blocks", "swe_token_context_operations",
    "swe_token_operation_segments", "swe_token_prefix_comparisons",
    "swe_token_prefix_candidates", "swe_token_diagnostic_evaluations",
    "swe_token_diagnostic_findings", "swe_token_diagnostic_values",
    "swe_token_diagnostic_gaps", "swe_token_diagnostic_refs",
    "swe_token_diagnostic_heads",
}
```

- [ ] 按设计存储表逐项写 DDL：ID VARCHAR(36)，source VARCHAR(64)/tenant VARCHAR(128) NOT NULL，受控状态/原因 VARCHAR，HMAC CHAR(64)，计数/字节 BIGINT，比例 DOUBLE，时间 DATETIME(6)；禁止正文列。复合主键/唯一键必须覆盖对象/版本/修订或明细序号。
- [ ] Context 增加 `context_revision, agent_id, provider_id, model_name, assembly_stage, representation_version, key_version, call_id, operation_id, request_started_at, request_ended_at`；操作快照没有 call 请求时间，不进入候选。兼容组摘要只用于检索加速，读取后核对全部标量组条件。
- [ ] 让 `TokenStore._write_record` 分派 `context`/`context_operation`；一个 Context 头和 occurrences/blocks 原子写入，旧修订不可覆盖新修订。operation 初始/完成按修订更新，关系行同事务；A call/input_items 仍可读，不删除旧五规则表。
- [ ] prefix candidates 保存每候选修订、界限、首差、排序时间；comparison 保存下界最佳与上界最大两个独立候选引用。evaluations 追加历史，heads 原子更新，refs/values/gaps 类型化落库。
- [ ] 回归故障测试：插入第 N 个 block 时抛异常，事务回滚后无 complete 头；operation 缺 after 可读但不可成对；多租户同引用查不到其他域；输入/分析幂等唯一键不重复写命中。

```python
def test_initial_schema_contains_b_tables():
    from pathlib import Path
    import re
    ddl = Path("scripts/sql/token_diagnostics_tables.sql").read_text()
    names = set(re.findall(r"CREATE TABLE IF NOT EXISTS (\w+)", ddl))
    assert B_TABLES <= names
    assert len(names) == 24
    assert not re.search(r"\b(JSON|ENUM|SET|LOB|BLOB|CLOB|TEXT)\b", ddl, re.I)
```

**Run:** `venv/bin/python -m pytest tests/unit/token_diagnostics/test_b_storage.py tests/unit/token_diagnostics/test_relational_storage.py tests/unit/token_diagnostics/test_store_boundaries.py -q`。使用既有 disposable MySQL fixture；无 `TEST_TOKEN_MYSQL_DSN` 时明确仅 SQLite/adapter 路径通过，不宣称 MySQL 已验证。

## Task 4：调用及真实上下文操作采集接入

**Files:** SWE `context_operations.py`、`model_observer.py`、`collector.py`、`writer.py`、`runtime.py`、`context.py`，`src/swe/agents/hooks/memory_compaction.py`。测试 `tests/unit/token_diagnostics/test_b_runtime.py`。

- [ ] 在 AttemptObserver 调用前使用同一个 call.context_id 生成 prepared Context；disabled 时只走 A。上下文哈希失败仍调用原模型、保留 Usage；Context 和 attempt 不重复 reserve 同一内存。增大 DTO 时队列计入真实全部元数据大小。
- [ ] 扩展 writer kind identity 映射，`context→context_id`，`context_operation→operation_id`，维持 pending/active/inflight 合计预算、旧修订覆盖及退避。Context 作为一个有界记录，不能半头半块分别入队。
- [ ] 定义 `begin_context_operation`/`finish_context_operation`：返回 operation ID、before Context 引用和原点身份；finish 捕获 after 或记录 error/skipped/missing。所有 adapters fail-open，取消继续传播，不吞业务取消。缺口计数沿用 A 的有限保证。
- [ ] 在 `_compact_tool_results_if_enabled` 的真实变换、`_install_checkpoint_stage` 的安装、legacy `_run_context_compaction` 到 `_persist_compaction_result` 的操作区间接入 before/after。后台只生成待安装摘要时不当成在线上下文已改变；安装边界才捕获在线变化。legacy 空摘要为失败/跳过，不捏造 after。
- [ ] 操作快照捕获同一业务输入层、相同表示/估算器版本；不为观测额外请求模型或无界复制全 memory。运行时拿不到安全快照时保留事实/缺侧。summary attempt 能取得 operation 关联时保存，异步跨 scope 时明确不可关联，不按时间猜测。
- [ ] 覆盖原工具返回与注入差异、同来源版本改变、明确 removed/created/replaced 关系；缺关联不生成替换事实。真实 Step 缺失时不把 A 的随机 call.step_id 改名成逻辑 Step；R03/R05 保持证据不足。

关键接入形态（实际 snapshot 来源按上述三个边界传入，不改业务参数）：

```python
operation = safe_call(begin_context_operation, before, "compact")
try:
    result = await existing_business_operation()
except BaseException:
    safe_call(finish_context_operation, operation, None, "error")
    raise
else:
    safe_call(finish_context_operation, operation, after, "ok")
```

这是接入模式而非要求新建 `existing_business_operation` 抽象；在原调用位置保留原 await、原异常处理及 return。after 取自同一次操作完成边界，禁止用下一次模型调用代替。

**Run:** `venv/bin/python -m pytest tests/unit/token_diagnostics/test_b_runtime.py tests/unit/token_diagnostics/test_model_observer.py tests/unit/token_diagnostics/test_writer.py tests/unit/token_diagnostics/test_runtime.py tests/unit/token_diagnostics/test_factory_integration.py -q`。对应 compaction 既有测试用 `rg --files tests | rg 'compaction|checkpoint|tool_result'` 找到后逐项加入执行记录。

## Task 5：历史检索、分块区间与加权比例

**Files:** Monitor `prefix_analysis.py`、`diagnostic_storage.py`。测试 `tests/unit/token_diagnostics/test_prefix_analysis.py`。

- [ ] 写可复现序列测试：相同开头、首块差异、条目末尾变化、块跨条目、短序列结束、UTF-8；已知真实首差必须落在输出区间内，结构标记不混入文本字节。
- [ ] `compare_prefix` 对链式摘要从起点二分查首差；内容不同后不再累计。若起点的首条目即不同但只知首块摘要，返回 `[0, first_block_text_end]` 而不是伪造精确 0；只有可证明首个文本位置不等才返回精确零。
- [ ] 查询完整 prepared/已结束请求：source/tenant/实际 Agent/Provider/model/assembly/representation/key 兼容，ended_at < 当前 started_at，历史结束在 `[当前开始−24h, 当前开始)`。按 ended_at DESC、call_id DESC，LIMIT 101，仅比较前 100 个；operation 不进入查询。
- [ ] 分析事务固定可见快照；保存 snapshot ID、实际候选引用/修订、策略/上限及超限标志；late arrival 不暗中替换历史。候选 99/100/101 与同时间排序分别测试。
- [ ] `aggregate_prefix` 只选完整输入、有候选、检索成功且未超限的非空文本请求；sum lower / sum text_bytes，返回 byte 分子分母、参与数和互斥主原因计数。0 个参与显示 null；候选限制不计成零。

```python
def test_prefix_total_uses_byte_weighting():
    from monitor.app.services.token_diagnostics.prefix_analysis import aggregate_prefix
    rows = [
        {"text_bytes": 100000, "lower_text_bytes": 10000, "state": "ready", "candidate_count": 1, "candidate_limited": False, "capture_state": "complete"},
        {"text_bytes": 1000, "lower_text_bytes": 900, "state": "ready", "candidate_count": 1, "candidate_limited": False, "capture_state": "complete"},
    ]
    value = aggregate_prefix(rows)
    assert value["matched_text_bytes"] == 10900
    assert value["eligible_text_bytes"] == 101000
    assert value["request_count"] == 2
    assert value["ratio"] == 10900 / 101000
```

**Run:** `venv/bin/python -m pytest tests/unit/token_diagnostics/test_prefix_analysis.py -q`。本任务不依赖网络 Provider，所有序列/时间为受控样本。

## Task 6：八规则及对应建议合同

**Files:** Monitor `diagnostic_rules.py`、`recommendations.py`、`diagnostic_contract.py`。测试 `tests/unit/token_diagnostics/test_context_rules.py`。

- [ ] 按设计八规则表分别建立边界样本，包含阈值前/等于/超过值、缺字段、缺分母、调用缺口/重试、已证明不适用；把评估对象绑定至实际 branch/call/Step，不跨分支构造连续窗口。
- [ ] `evaluate_context_rules(snapshot)` 返回八规则评估及其 values/gaps/refs/findings。先验证适用性和完整性；增长序列仅同单位/估算器，源身份与版本相同才生成 R02 生命周期候选；R03 需要真正业务 Step 终态，工具结束不能替代。
- [ ] R04 不猜工具安全属性；R05 只做确定性行动/结果签名重复，进展未知仅可疑；R08 本轮固定缺失 cohort/complexity/quality 证据，不输出统计异常。缺真实逻辑 Step 的 R05 不触发。
- [ ] R06 验证当前+至少两历史完整请求、可靠静态来源至少 2000 estimated tokens，max(candidate.upper/current.text_bytes) < 0.2；候选超限、区间跨阈值或上界不可得为不足。静态重复证据不从 prefix 匹配反推。
- [ ] 对每条规则提供不可自动执行的建议记录：action_code、运行位置、前提、例外、风险及验证步骤。R01 摘要/预算；R02 来源摘要；R03 工具投影；R04 只读复用前置检查；R05 排查重试/进展；R06 稳定结构；R07 按需工具/指令；R08 业务可比组及评测。
- [ ] 签名仅可靠结构/配置身份形成，包含 scope、Agent/Provider/model、rule/threshold/signature version；unknown 实例保留独立 finding ID。同一来源的 operation ID 仅实例定位，不用于跨 Task 聚合。

R06 核心边界 helper 与测试（在 `diagnostic_rules.py` 定义，不另建通用规则 DSL）：

```python
def classify_low_prefix(lower, upper, candidate_limited, threshold=0.2):
    if candidate_limited or lower is None or upper is None:
        return "insufficient_evidence"
    if upper < threshold:
        return "candidate"
    if lower >= threshold:
        return "not_hit"
    return "insufficient_evidence"

def test_prefix_interval_crossing_threshold_is_insufficient():
    assert classify_low_prefix(0.15, 0.25, False) == "insufficient_evidence"
    assert classify_low_prefix(0.15, 0.19, False) == "candidate"
    assert classify_low_prefix(0.15, 0.19, True) == "insufficient_evidence"
    assert classify_low_prefix(0.20, 0.25, False) == "not_hit"
```

其余适用性/静态内容门槛在调用 helper 之前检查，helper 不能独立完成 R06。

**Run:** `venv/bin/python -m pytest tests/unit/token_diagnostics/test_context_rules.py tests/unit/token_diagnostics/test_rules.py -q`。旧五规则 tests 必须继续通过。

## Task 7：后台租约、评估历史与清理

**Files:** Monitor `diagnostic_analysis.py`、`diagnostic_storage.py`、`analysis.py`、`store.py`、`lifecycle.py`。测试 `tests/unit/token_diagnostics/test_b_analysis.py`、`test_b_retention.py`。

- [ ] 新测试复用实际 DB adapter：同 lease 两次提交不重复命中、失效/被替代 lease 不能改 heads、输入修订增长时旧评估只进历史、显式重分析生成新 snapshot、late candidates 只影响新分析。
- [ ] 分支同一次 claim 执行旧五规则和 B 分析；保留旧分析存储路径，将 B history/head 提交接到同一租约验证。每请求 prefix 独立 comparison，规则评估引用对应 revision/snapshot。
- [ ] 后台总分析 deadline 5 秒，DB 查询超时及计算协作式 deadline 一致；避免仅 `wait_for(to_thread())` 然后让工作线程无限继续。纯计算定期检查时间并终止，禁止发布部分“当前完成”结果。
- [ ] `_write_evaluation` 原子插入 values/gaps/refs/findings 与头指针；同输入/规则/阈值/snapshot 重试复用唯一键，新的快照不覆盖旧历史。逐 branch 检查数据修订，不能以 sum(revisions) 判断 Task 追平。
- [ ] cleanup 覆盖全部 24 表，明细→比较/评估→Context/operation/heads→旧根记录，保留活动/近期 child 保护；共享历史候选引用过期后仍标记 evidence_expired，不从其他租户补证据。
- [ ] enabled 但 B 表缺失/契约不支持时分析 failed/unavailable，有受控错误码；生命周期退避和关闭有限等待不影响旧服务。关闭 B 后五规则和旧读接口照常工作。

测试的关键事务断言应对实际 adapter 查询，不只验证 mock 被调用：

```python
assert saved_old["evaluation_id"] != saved_new["evaluation_id"]
assert head["current_evaluation_id"] == saved_new["evaluation_id"]
assert len(replayed_findings) == len(original_findings)
assert stale_claim_commit is False
assert task_state["stale_branch_count"] == 1
```

以上变量在测试内由提交后 SELECT 结果取得，不使用硬编码模拟“成功”。

**Run:** `venv/bin/python -m pytest tests/unit/token_diagnostics/test_b_analysis.py tests/unit/token_diagnostics/test_b_retention.py tests/unit/token_diagnostics/test_monitor.py tests/unit/token_diagnostics/test_store_boundaries.py -q`。

## Task 8：窗口指标、完整性及问题组查询

**Files:** Monitor `diagnostic_queries.py`、`diagnostic_filters.py`、`query_service.py`、`lifecycle.py`。测试 `tests/unit/token_diagnostics/test_b_queries.py`。

- [ ] 用跨窗口 Task、两个模型、根/子 Agent、未知/部分 Usage、完整/缺子分支数据建立 adapter 样本；断言窗口尝试总量不等于 Task 全生命周期规模，模型筛选不串入另一模型用量。
- [ ] 查询 `dashboard/tasks/task/branches/calls/context/prefix/operations/issues/instances/evaluation`，对应 Task 9 端点。范围 UTC 半开、默认 24h、最多 30d；每次读返回 scope/window/as_of。root Agent 归属与实际执行 Agent 过滤分开命名。
- [ ] Task summary 用整个生命周期尝试汇总，同时显示模型筛选贡献；分布仅终态/完整采集/完整 Usage，展示样本数。统计 SQL aggregate 不依靠当前页 50 个对象代表全局；查询预算不足显示 limited/unavailable。
- [ ] head 输入修订与当前对象逐个比较，落后结果只显示历史，不进入当前问题统计。问题组仅当前可靠签名 grouping，unknown 实例以 finding ID 单列；count hits 与 count distinct tasks 分开。
- [ ] 统一游标编码 `{scope_hash, filter_hash, sort_time, sort_id}`；结构校验、长度上限、修改筛选拒绝旧游标。游标只是分页，scope 始终来自认证头，不能信任游标授权。
- [ ] Context occurrences 和 operation relations 分页；prefix 不返回 raw、HMAC key 或其他域 identity。前后变化只在成功、成对完整、同表示/估算器下计算，否则 null + reason。

```python
def test_window_and_task_totals_are_distinct():
    # 在 relational fixture 插入：Task 昨天开始，模型 A 昨天10、今天20，模型 B今天30。
    assert dashboard["known_total_tokens"] == 50
    assert dashboard_model_a["known_total_tokens"] == 20
    assert task["known_total_tokens"] == 60
    assert task["selected_model_tokens"] == 30
    assert task["usage_complete"] is True
```

测试内用 `TokenQueryService`/新服务和 fixture 实际调用取得四个结果；不能直接构造上述字典当成测试。fixture 插入时间使用固定 UTC instant，并由 query 传入相同窗口。

**Run:** `venv/bin/python -m pytest tests/unit/token_diagnostics/test_b_queries.py tests/unit/token_diagnostics/test_relational_storage.py -q`。

## Task 9：11 个作用域只读端点

**Files:** `monitor/src/monitor/app/routers/token_diagnostics_v2.py`、`routers/__init__.py`；测试 `tests/unit/token_diagnostics/test_b_routes.py`。

- [ ] 创建 APIRouter prefix `/token-diagnostics/v2`，复用现有 `request_scope`/安全 read helper；app 外层 `/api` 前缀不重复加。11 个端点全部 GET，禁止反馈写端点。
- [ ] 精确路径：`/dashboard`、`/tasks`、`/tasks/{task_id}`、`/tasks/{task_id}/branches`、`/tasks/{task_id}/calls`、`/contexts/{context_id}`、`/calls/{call_id}/prefix`、`/tasks/{task_id}/operations`、`/issues`、`/issues/{group_key}/instances`、`/evaluations/{evaluation_id}`。
- [ ] 入参在 `diagnostic_filters.py` 合成 immutable query；约束 ID/字符串长度、limit 1–100、日期合法性及范围≤30d、rule/state allowlist。不存在/越权按 404，身份不匹配按 403，无服务/DB timeout 按 503，bad cursor/filter 按 400。
- [ ] ASGI 行为测试覆盖每个端点的 scope、身份 headers、方法 405、异常安全消息及 SQL 不可用。必须测试 role=admin 不能扩大 source/tenant、group_key/cursor 不能带来跨租户结果。
- [ ] 注册 v2 router，保留旧 router；不改变 proxy/auth middleware。`/api` direct 与 Console helper `/monitor` 是部署映射，测试正确相对路径，不在 helper 里再次写 `/api`。

```python
@pytest.mark.asyncio
async def test_v2_is_read_only(client):
    response = await client.post("/api/token-diagnostics/v2/issues", json={})
    assert response.status_code == 405
    response = await client.get("/api/token-diagnostics/v2/dashboard")
    assert response.status_code == 400
```

本文件 `client` fixture 用 FastAPI+httpx ASGITransport 注册 router 和外层 `/api`，以 Task 8 服务为 dependency；503 测试显式注入抛含 secret 的 DB 异常，响应不得含 secret。

**Run:** `venv/bin/python -m pytest tests/unit/token_diagnostics/test_b_routes.py tests/unit/token_diagnostics/test_routes.py -q`。

## Task 10：Console 类型、请求与公共状态

**Files:** `console/src/api/modules/tokenDiagnosisV2.ts`/`tokenDiagnosisV2.test.ts`，`TokenDiagnosisShared`五个文件及 `presentation.test.ts`/`useDiagnosisResource.test.tsx`。

- [ ] 从 Task 9 实际响应定义 Dashboard/Task/Call/Context/Prefix/Operation/IssueGroup/Evaluation DTO，unknown 数值明确 `number | null`；不使用 `any` 或把字符串 payload 假装成结构化字段。
- [ ] API root 为 `/monitor/token-diagnostics/v2`，通过现有 request helper：所有资源 ID encodeURIComponent，query URLSearchParams，所有请求可 AbortSignal。测试路径/query 不含重复 `/api`，旧 tokenDiagnostics 模块不改。
- [ ] `useDiagnosisResource` 管理 loading/error/unavailable/ready 与取消；筛选/定位变化取消旧请求，迟到响应不能覆写新资源。不要跨作用域保存缓存，不使用页面 localStorage 持久化诊断数据。
- [ ] `presentation.ts` 分离 null/0、actual/estimate、complete/partial/unknown、stale/failed 显示；`EvidenceState` 明确缺字段/超限/过期，颜色之外有文字。FilterBar 绑定时间/Agent/model/规则筛选并清空失效 cursor。

```typescript
export function formatCount(value: number | null): string {
  return value === null ? "未知" : value.toLocaleString("zh-CN");
}

it("keeps unknown different from zero", () => {
  expect(formatCount(null)).toBe("未知");
  expect(formatCount(0)).toBe("0");
});
```

**Run:** `pnpm --dir console test:run src/api/modules/tokenDiagnosisV2.test.ts src/pages/Analytics/TokenDiagnosisShared`，然后 `pnpm --dir console run typecheck`。测试 mock 使用 Task 9 契约固定 fixtures，不修改生产 API 以迁就线框数据。

## Task 11：全局 Token 看板与导航

**Files:** `console/src/pages/Analytics/TokenOverview/{index.tsx,index.module.less,index.test.tsx}`；`console/src/pages/Analytics/index.tsx`、`console/src/layouts/Sidebar.tsx`、`layouts/constants.ts`、`layouts/MainLayout/index.tsx`、`console/src/locales/zh.json`。

- [ ] 先写页面行为 tests：部分报告标题是已知用量，前缀为字节加权保守比例，0参与为不可用，pending/failed 覆盖可见，点击 Task 携带筛选跳执行页；503 不显示零问题。
- [ ] 主体展示已知输入/输出、窗口 Usage 完整率的分子分母、已记录 Task、Agent 归属用量、Task 分布及样本、八规则评估覆盖、前缀参与/排除情况；输入压力仅有预算时用正确名称。
- [ ] 图表和列表使用服务端真实 projection，不能在组件 sum 当前页得到全局。排序叫用量分布，不能写效率/质量排名；单元清晰显示“估算/已知/完整生命周期”。
- [ ] 注册 `/analytics/token-overview`，为三页准备稳定导航 key/title；旧 `/analytics/token-diagnostics` 继续可用。执行导航入口无 Task 时落到本页的任务列表，不虚构 default Task。
- [ ] 使用既有 AntD/Less 和 `console/DESIGN.md`，保留中文长名/英文 ID、空列表、窄 iframe 布局；不要把设计预览里的模拟数值写入生产组件。

页面 test 的可观测断言：

```typescript
expect(screen.getByText("已知总 Token")).toBeInTheDocument();
expect(screen.getByText("共同文本前缀比例（保守估计）")).toBeInTheDocument();
expect(screen.getByText("候选检索超限")).toBeInTheDocument();
expect(screen.queryByText("预计节省金额")).not.toBeInTheDocument();
```

**Run:** `pnpm --dir console test:run src/pages/Analytics/TokenOverview/index.test.tsx`，`pnpm --dir console run typecheck`。

## Task 12：执行诊断页及调用/操作证据下钻

**Files:** `console/src/pages/Analytics/TokenExecutions/{index.tsx,index.module.less,index.test.tsx}`；Analytics 路由与已定义导航映射。

- [ ] 注册 `/analytics/token-executions/:taskId`；读取 branch/call/context/evaluation 和 returnTo query，validate returnTo 仅允许本应用 analytics 路径；无 Task 转到 Task 列表。
- [ ] 先写切换 tests：选择调用 3 使构成、Usage、Context、前缀、规则实例全部切换，unknown output 保留，分支汇总有范围标签；详情关联不得固定调用 2。
- [ ] 分支独立顺序，调用按 logical_seq/attempt_no 展示；重试显示 attempts，缺 Step 不造 Step。不同模型/阶段分组，Tool 来源可靠性按条目显示，unknown 生命周期明确。
- [ ] 前缀 panel 展示真实当前/历史 Context、实际比较数、超限、表示/密钥版本、下界/上界、首差条目/区间；不显示 cache hit/miss。缺历史、缺指纹、失败、超限分别可见。
- [ ] Operation panel 显示 before/after 与变换关系，只有合同条件齐备显示规模变化。summary call 单独链接 Usage，缺关联显示未知；业务质量未知始终可见。
- [ ] 请求分开分页，切 call/branch 取消过时 fetch，受限列表不冒充完整树。调用选取可键盘操作，图形选择有名字及选中状态。

```typescript
await user.click(screen.getByRole("button", { name: /调用 3/ }));
expect(await screen.findByText("ctx-03")).toBeInTheDocument();
expect(screen.getByText("输出用量未知")).toBeInTheDocument();
expect(screen.queryByText("ctx-02")).not.toBeInTheDocument();
```

test 使用受控三个调用不同 prefix/contexts 响应，另测试旧请求晚到仍保持 ctx-03。

**Run:** `pnpm --dir console test:run src/pages/Analytics/TokenExecutions/index.test.tsx src/pages/Analytics/TokenDiagnosisShared/useDiagnosisResource.test.tsx`，`pnpm --dir console run typecheck`。

## Task 13：问题清单、独立实例与覆盖状态

**Files:** `console/src/pages/Analytics/TokenIssues/{index.tsx,index.module.less,index.test.tsx}`；Analytics 路由/导航最终注册。

- [ ] 注册 `/analytics/token-issues`，URL 保留筛选和页游标；页变化不丢作用域，规则/模型/时间变化清 cursor。请求少依赖、取消旧请求。
- [ ] 展示可靠问题组的 hits/distinct Task/first/latest 和不可聚合实例；只有同可靠签名展示分组，无共同根因已确认标签。涉及估算量注明重叠，不相加为节省。
- [ ] 点击组加载该组真实实例分页；点击实例用其 Task/branch/call/context/evaluation 精确定位，并携带当前问题页 returnTo。返回原筛选/分页，越权/已过期状态不可跳转到其他 Task。
- [ ] 详情显示观察值/上下界、当时阈值、版本、建议前提/风险/验证步骤；不足/不适用/待评估/failed 单列，不记命中。历史结果可查看但不混入当前组统计。
- [ ] 不增加确认/误报、处理人、缓存/裁剪/停止执行按钮；Keyboard 和分页/窄屏可操作，保留 A 入口。

```typescript
expect(screen.getByText("12 次命中 / 4 个 Task")).toBeInTheDocument();
expect(screen.getByText("无法可靠聚合")).toBeInTheDocument();
expect(screen.queryByRole("button", { name: "确认" })).not.toBeInTheDocument();
expect(screen.queryByRole("button", { name: "自动优化" })).not.toBeInTheDocument();
```

**Run:** `pnpm --dir console test:run src/pages/Analytics/TokenIssues/index.test.tsx src/pages/Analytics/TokenDiagnostics`，`pnpm --dir console run typecheck`。

## Task 14：整体故障验证、真实数据库及文档交付

**Files:** 以上新增行为测试；`docs/deploy/token-diagnostics.md`、`analysis/playbook/token-diagnostics.md`、本计划。

- [ ] 跑完整 Token 子系统：`venv/bin/python -m pytest tests/unit/token_diagnostics/ -q`。期望 A/B 全部通过，报告实际数量；不沿用 A 的历史“70 passed”作为 B 结果。
- [ ] 用已有 `TEST_TOKEN_MYSQL_DSN` opt-in disposable fixture执行 `test_b_storage.py`/`test_b_analysis.py`/`test_b_retention.py`，验证 24 表创建、事务回滚、唯一键、真实两实例 lease 竞争及清理；禁止业务库/生产凭据。缺服务时记录该项未运行，不能把 SQLite 当 MySQL 结论。
- [ ] 跑 `pnpm --dir console test:run src/pages/Analytics/TokenOverview src/pages/Analytics/TokenExecutions src/pages/Analytics/TokenIssues src/pages/Analytics/TokenDiagnosisShared src/pages/Analytics/TokenDiagnostics src/api/modules/tokenDiagnosisV2.test.ts` 及 `pnpm --dir console run build`。新失败逐项定位，预存问题必须有证据及隔离说明。
- [ ] 浏览器按项目 Console 规范检查桌面及窄 iframe/`hideMenu=true`，使用本地 ASGI/受控 API 测试数据联调；实际看板→Task→调用→问题→实例→返回，验证三调用证据切换、error/partial/stale/no-candidates/limited/expired、键盘及长 ID。发现问题修复后重验相关路径，不以 HTML 线框截图代替。
- [ ] 接入 frontend-design/copaw-f2e-review（受 `console/DESIGN.md` 约束）和 verification-before-completion 技能完成 UI/代码质量及交付验证；执行相应 pytest、Black/flake8 repository flags、前端 typecheck/lint changed files 和 `git diff --check`。需要 pre-commit 时避免自动修复无关用户文件。
- [ ] 分别注入队列满/缺 key/大输入/时间耗尽/DB timeout/旧 lease/取消/进程退出；每项验证业务返回及有界缓冲，未知不得变零。测量 5ms 协作预算的实际开销并记录超限/缺口比例，不能宣传 hard latency guarantee。
- [ ] 更新部署顺序：全量初始 DDL→两侧新版本→Monitor 验证→B 开关；不提供 `CREATE IF NOT EXISTS` 迁移承诺。记录 A 旧版本可读、24h/100/256-byte/256KiB/5ms/5s 预算、30d retention、只读页面/身份网关/缺口排查及 C 边界。
- [ ] GitNexus `detect-changes --scope all --repo .`；必须 `partial=false/truncated=false` 后再描述检查完成。未跟踪实现文件纳入分析；禁止顺手 stage 用户文件。按用户授权小步提交，graph 的动态派发边界单独报告。
- [ ] 最终评审按设计验收表逐项记录通过/未运行/限制。修复具体验收差异，重跑涉及测试；完成后在本计划的真实记录区填写结果，不提前勾选。

## 设计覆盖矩阵

| 已批准要求 | 实施任务 |
| --- | --- |
| Q1/Q10 近似文本、有序指纹、首差区间 | 1、2、5、12 |
| Q2 可靠来源优先、独立 occurrence | 2、3、4、6、12 |
| Q3/Q4/Q11 24h/同组/最多100/完成时序 | 5、7、8、12 |
| Q5/Q6/Q9/Q16 八规则/阈值/明确降级 | 1、6、7、11、13 |
| Q7/Q8 三页与深链接、原入口保留 | 9、10、11、12、13 |
| Q12 操作前后/额外摘要独立记账 | 3、4、8、12 |
| Q13 可靠问题组/独立命中/历史当前 | 3、6、7、8、13 |
| Q14 C 反馈/策略/实验边界 | 9、13、14 |
| Q15 加权比例/缺口/零与未知 | 5、8、10、11 |
| 事件版本、预算、故障、多实例 | 1、2、3、4、7、14 |
| 初始 DDL、SQL规范、24表清理 | 3、7、14 |
| 时间窗/模型/根与执行Agent口径 | 8、9、11、12 |
| 隐私/租户隔离/无原文/只读 | 2、3、5、8、9、10、14 |

## 计划自检与执行记录

编写阶段已核对：任务引用与文件责任一致，13 张 B 表+11 张 A 表为24张；全部16项设计决定有覆盖；旧五规则、专项核对暂缓及C边界未扩大。所有复合测试示例明确要求从实际 fixture/服务结果取得变量，不能复制断言后伪造响应使其通过。

执行记录从实际开发开始填写，当前所有任务未执行。每项至少记录变更范围、先失败/后通过测试、图分析限制和未运行检查。未经真实验证，不标记任务完成或宣称页面已上线。
