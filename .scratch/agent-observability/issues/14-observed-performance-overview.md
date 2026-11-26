# 14: 展示口径明确的观测性能总览

Status: open
Type: task
Blocked by: 07
Triage: ready-for-agent
Priority: P0

**What to build:** 运营人员在独立页面查看明确样本范围内的操作性能、调用与失败热点，知道数据覆盖和未知用量，并获得可复现的查询与资源容量报告。

**Blocked by:** 07：追踪重试、恢复及分页Events/Links

**Status:** ready-for-agent

依据已确认的独立可观测设计、Trace视图对照、领域词汇及ADR 0047/0048。与旧SDK、旧埋点、旧追踪ID和旧可观测存储查询没有运行依赖；只输出元数据，正文仅经现有业务授权从NAS读取。采集由用户处理，本票不增加ES、TTL/清理或新身份体系；授权仍沿用项目已有身份判定。

- [ ] 独立观测查询按授权scope、时间窗、操作类型及业务状态生成P50/P95/P99、调用次数、Tool失败率、类型热点、真实Loop及TTFT/首可见延迟覆盖；从规范投影到API和UI可演示。
- [ ] 每组返回population/window/generated_at、样本量、start/end证据、Usage已知覆盖和质量；缺值不补零，不把观测分布冒充全业务总体或保证采集完整。
- [ ] Run elapsed包含审批等待，active为Attempt区间并集；无对应区间时不伪造活动时间。父子/并行操作不简单加总，热点说明可能重叠。
- [ ] 采用明确样本集nearest-rank分位，不平均子组P95；成功/失败/取消可分开筛选，已恢复Tool错误仍计工具失败，不自动计根失败。
- [ ] 逻辑模型调用、实际可见请求尝试及Usage不重复累计，Provider不可见重试不推算；成本、关键路径、TPOT、CPU分解仍属后续范围。
- [ ] 聚合最多50000个满足条件的已结束样本，超限明确range_too_large，不暗中抽前N或隐蔽采样；执行/响应资源预算不阻塞Agent。
- [ ] 给出可复现数据集、查询计划、数据量、延迟P50/P95及CPU/内存报告，不宣称未经验证生产TPS/SLA；缺平台条件明确报告。
- [ ] 覆盖空集、未知Usage、并行、重试、恢复、重复乱序、边界样本和权限隔离的指标测试；完成Chromium108/当前浏览器展示验收。

验收环境不可用时必须明确记录未验证项，不以构建成功或新浏览器模拟替代真实Chromium108验收。

## Comments

2026-11-15：用户已确认15张票的拆分及阻塞关系，现发布为本地实施事项。尚未领取或开工；本次发布不构成功能开发授权。

2026-11-17：Performance API 增加实际样本 `sample_start_at`/`sample_end_at` 和 Span `complete/partial/unknown` 质量计数，显式区分空样本与查询已完整读取的观测子集；SQL 读取质量列。单测验证边界和混合质量，目标 pre-commit 通过。CPU/内存、真实 MySQL 延迟分布及大样本 EXPLAIN/资源报告尚未完成；现有 50k 合成筛选证据没有 CPU/内存数据，不能作为容量验收。UI/浏览器按用户要求暂缓，工单保持 open。

2026-11-17：在隔离 `swe_agentobs_codex_20261117_test` 中使用唯一 Source/Run 插入 50,001 条合成 LLM Span。首次直接样本读取在 MySQL 5 秒执行预算内超时（3024），说明不能依赖 `LIMIT 50001` 全行拉取完成超限判定；该组数据随后按唯一 Run ID 清理，确认 run 行为0。

同日修正：Performance API 现在先执行相同权限/筛选条件的 50,001 项有界候选计数，超限直接返回 `422 range_too_large`，只有不超限才读取聚合样本。重新在相同隔离 schema、唯一 Source 下重复插入50,001条 Span，候选查询5次均返回50,001；耗时 254.94/260.28/271.96/272.17/281.88ms，nearest-rank P50=271.96ms、P95=272.17ms。EXPLAIN: Run 选择 `ix_agentobs_runs_source_time`；Span 选择 `ix_agentobs_spans_type`，查询估算行数1但实际有50,001，优化器估算偏差需留意。完整50,001行样本读取仍会超过5秒，但新 API 会在其前拒绝，避免超限请求拉取完整结果。客户端 Python 进程 CPU 增量0.024秒，进程峰值 RSS 60,276,736 bytes（约57.5MiB，包含解释器与测试工具）；未取得 MySQL 主机 CPU/内存指标，不能声称服务器资源预算达标。隔离数据已清理并确认 Run 行为0。

独立只读复核确认候选探测与实际样本读取使用相同 Source/service/window/status/span type/operation 条件，超限在样本 fetch 前返回422，查询超时继续返回503；无具体实现问题。定向观测/审批单测80 passed，目标 pre-commit 全绿。资源报告仍不包含 MySQL 主机 CPU/内存，五次查询不足以支持生产 SLA，故工单保持 open。

2026-11-18：active duration 区间并集现在要求 Run 下每个 Attempt 都有有效的 start/end；任一 Attempt 开放或时间逆序时返回 unknown，不再把已结束子集当成完整活动时长。终态 Run 收到迟到 `attempt.started` 时也重算，使既有值失效。新增缺失/逆序纯函数回归及“完整 Attempt→Run ended→迟到开放 Attempt”MySQL 合同；performance/query 单测 36 passed，目标 pre-commit 通过，独立复核无阻塞问题。真实 MySQL 合同因未配置 `SWE_AGENTOBS_TEST_DB_*` 被 skip；该限制、服务端资源指标和浏览器验收仍未闭合。

2026-11-18：首次真实 MySQL 运行发现 `ObservabilityStore.get_run` SELECT 未返回已有的 `active_duration_us` 列，导致未知值读成缺字段并在合同断言触发 `KeyError`。现已将该列加入 detail SELECT；core/query 定向单测 69 passed，目标 store pre-commit 全绿。隔离 MySQL 重跑 active-duration late Attempt 合同后，与终态、approval 和 asset 分页合同共 4 passed，确认 `get_run` 返回 SQL NULL；随机 schema 已清理。独立复核确认列投影和迟到 Attempt 语义一致，无可操作发现。MySQL 主机 CPU/内存容量数据和浏览器验收仍未完成。

同日继续：发现 Performance API 的 `llm.call_count` 和 Usage 覆盖率分母混入 `LLM/request_attempt` 物理 Span。现将逻辑调用、Usage 覆盖和首输出延迟限定于 `LLM/chat`，并新增独立 `request_attempt_count`；全局 Span 时长/按类型样本仍保留两种操作。单测覆盖混合及 request-attempt-only 筛选；真实 ModelWrapper→RetryChatModel→Inbox→Projector→manager Performance API MySQL 合同中，确定性 Provider 首次 429、第二次成功，验证 `call_count=1`、`request_attempt_count=2`、Usage coverage=1，合同 1 passed。随机 `_test` schema 清理核验为0；当前 Console 不显示该新指标，按用户要求未改前端。CPU/内存主机指标、重复 P50/P95 容量报告及页面验收仍未闭合，Ticket 保持 open。

独立只读复核确认 chat/attempt 筛选和重试计数合同与 `RetryChatModel` 回调路径一致，finding 已关闭、无新增可操作问题。Observability query 单测 81 passed；query/unit/pipeline 目标 pre-commit 及 diff check 全绿。

同日容量复测：在随机 `_test` schema 通过单条集合式 INSERT 生成 50,001 个已结束合成 LLM Span，只重复执行授权筛选同形的 bounded candidate-count 查询 7 次。每次准确返回 50,001；客户端往返 253.56/241.81/238.97/240.67/240.77/240.61/241.07 ms，nearest-rank P50=240.77ms、P95=253.56ms。EXPLAIN 选择 Run `ix_agentobs_runs_source_time`、Span `ix_agentobs_spans_run_time`，计划估算匹配50,001行；不会拉取完整样本，超限在候选探测后返回。测试进程 CPU 增量0.0229秒、峰值 RSS 67,928,064 bytes（平台报告单位）；Performance Schema 全局 memory current 在前后快照约增加1,230,594 bytes，但这是全 MySQL 实例且无法归因本查询。检查确认 `events_statements_cpu` consumer 为 NO，不能取得 MySQL 主机/查询 CPU；statement digest 未提供该查询记录。隔离 schema 已删除并确认不存在。该复测是单实例、单一重复查询的测试库观测，不代表生产 SLA/TPS；服务器 CPU 与全局内存归因仍未验证，故容量验收项仍保持未完成。

同日新增可复现工具 `.scratch/agent-observability/benchmark_performance_capacity.py`：仅要求显式 `SWE_AGENTOBS_TEST_DB_*` 测试连接，强制控制库名以 `_test` 结尾，在唯一随机 `swe_codex_capacity_*_test` 子库中创建表、集合式生成数据、重复七次精确候选查询、输出nearest-rank P50/P95、EXPLAIN、客户端 CPU/RSS 与 Performance Schema可用指标，并在 finally 删除且验证余数。脚本真实运行通过：50,001行全部被候选预算准确识别，七次 270.46/255.33/257.45/257.34/257.72/259.41/257.07ms，P50=257.45ms、P95=270.46ms；EXPLAIN 选择 `ix_agentobs_runs_source_time` 和 `ix_agentobs_spans_run_time`，客户端 CPU 0.0259秒、峰值 RSS 67,895,296 bytes。全局 Performance Schema memory 前后差约1.23MB但不可归因，语句 CPU consumer 仍为 NO、statement digest 未记录该 SQL；服务器 CPU 不可测仍明确标未验证。随机 schema 清理为0，目标脚本 pre-commit 全绿。该可复现测试只说明这一远端测试实例上的单查询过滤成本，不构成生产 SLA/TPS 或主机 CPU 资源验收；票保持 open。

同日复验最终脚本：再次生成50,001行并成功清理随机库，七次查询均返回预算上限+1；本轮 P50=238.28ms、P95=244.64ms，客户端 CPU=0.0209秒、峰值 RSS=68,239,360 bytes。两轮结果显示实例负载下延迟有变化，因此记录原始样本而不合并成生产承诺。基准脚本目标 pre-commit 全绿；server CPU consumer 仍关闭，服务器 CPU/查询 CPU 归因仍不可得。

独立只读复核确认随机 schema 所有权检查、创建异常/确认丢失后的 `DROP DATABASE IF EXISTS` 清理、DDL 作用范围及 EXPLAIN/候选查询过滤一致；全局 Performance Schema 指标未被归因为单查询资源。清理 finding 已关闭，无新增可操作发现。

2026-11-26：模型性能总览已展示已结束逻辑模型调用、请求尝试、输入与输出用量覆盖、首有效 chunk 与首可见文本的 P50/P95/P99 及已知样本数。隔离 MySQL 测试验证两次请求仅一次有用量时覆盖率为 50%。Chrome 158.0.8127.88 与 Chromium 108.0.5359.29 在受控 API、1280/1440 宽度及 `hideMenu=true` 下验证通过。
