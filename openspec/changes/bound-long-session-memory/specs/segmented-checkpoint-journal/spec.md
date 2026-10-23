## ADDED Requirements

### Requirement: Checkpoint appends SHALL operate on bounded segments under the existing Chat lock

系统 MUST在同一次Chat flock内校验epoch、分配连续sequence并持久追加事件及发布提交根，不得读取或重写全部旧events。常规段 SHALL限制为1MiB或512条，新事件编码上限为64KiB；超限明确失败，不静默截断。迁移遗留大事件按设计保留原文。

#### Scenario: Two request memory instances append concurrently
- **WHEN** 它们向同Chat当前epoch追加不同事件
- **THEN** 存储层在锁内分配两个不同且连续的序号，不让调用方先读后算同一个next sequence

#### Scenario: A very long journal receives one event
- **WHEN** 已有10万条历史事件
- **THEN** 单次追加只操作小型根、当前段和新frame，封段成本限于当前段，不读取全部旧事件

### Requirement: Checkpoint metadata and append visibility SHALL be defined by an atomic root

v2根 MUST同时引用生效epoch、checkpoint、manifest/evidence版本及当前journal提交边界。新文件先fsync、根后发布；reader SHALL只看根声明的完整视图，不自动导入孤儿文件。

#### Scenario: Event data is durable but root was not published
- **WHEN** 进程在追加fsync后死亡
- **THEN** 超过committed_bytes的尾部不可见，并在下次持锁写入前移除

#### Scenario: Checkpoint activation crashes between individual file writes
- **WHEN** 部分新版本文件已写但根未发布
- **THEN** reader继续使用旧epoch/checkpoint/manifest组合，不暴露未提交boundary

### Requirement: Journal readers SHALL use explicit watermarks and fail on committed corruption

读取 MUST按固定根快照及epoch/sequence范围逐frame校验，跳过已应用旧段而不构建全历史list。投影可用有界deque；归纳及验证消费者 MUST处理其指定完整前缀，不能静默只保留末几条。已提交段本阶段 MUST保留。发现已提交损坏 SHALL在Chat锁内持久化quarantine标记，所有Pod的后续writer MUST检查并拒绝写入，修复验证后才显式解除。

#### Scenario: Projection follows a high applied watermark
- **WHEN** 大部分历史事件已纳入checkpoint
- **THEN** reader定位交界段并读取要求的delta，不解码所有已应用段

#### Scenario: A committed middle frame is corrupt
- **WHEN** 出现hash不符、缺段、epoch不符或序号间隙
- **THEN** 该Chat停止checkpoint写入并报告错误，不跳过损坏继续推进水位

### Requirement: Journal evolution SHALL preserve archive and epoch contracts

系统 MUST保持boundary UUID、归档行位置、现有签名cursor和证据隔离。reset SHALL原子切换epoch；旧epoch不自动进入模型上下文。Chat删除 SHALL继续先tombstone再删除对应新旧数据。迁移/回退 MUST覆盖当前完整数据。

#### Scenario: Existing archive cursor is used after journal migration
- **WHEN** 用户请求先前有效的归档分页cursor
- **THEN** 相同boundary和行位置继续解析，分页不会因journal分段重排而改变

#### Scenario: Append races with reset
- **WHEN** 旧epoch追加与new/clear争抢Chat锁
- **THEN** 只有当前epoch可提交，旧epoch请求失败或按原恢复规则处理，不写入新epoch
