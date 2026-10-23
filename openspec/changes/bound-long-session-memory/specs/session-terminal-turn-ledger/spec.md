## ADDED Requirements

### Requirement: Execution snapshots SHALL retain bounded terminal history without dropping unsettled state

新格式session SHALL保留全部未结算/受保护turn，以及最多64条且256KiB的可归档近期终态窗口。只有此前revision已持久结算的记录才可移出；移出记录的完整正文、身份、权威终态和admission顺序 MUST 保存在session所属的冷存储中，不以TTL删除。

#### Scenario: Historical terminal turns exceed the hot budget
- **WHEN** 会话有大量已持久结算的turn且不存在受保护引用
- **THEN** 后续归档提交按admission顺序移出最旧终态，热窗口满足条数和字节边界
- **AND** 普通执行不读取所有冷记录，旧turn仍可按msgid查询

#### Scenario: A pending settlement exceeds the nominal budget
- **WHEN** admitted或待结算重试状态超过近期终态预算
- **THEN** 系统保留这些状态并单独统计，不截断或丢弃用户正文

### Requirement: Cold turn publication SHALL share the session revision commit point

系统 MUST 在同一session锁内先持久化pending intent及不可变冷版本，再原子发布携带revision/generation/nonce的session根。写入方 MUST 先恢复pending才能开始下一次写入；reader SHALL按一次根快照选择已提交版本。

#### Scenario: Process dies before session root publication
- **WHEN** 冷文件已落盘但session仍为base revision
- **THEN** reader不能看到这些未来版本
- **AND** 下一写入方删除intent列出的未提交文件后才继续

#### Scenario: Process dies after publication before intent removal
- **WHEN** 根revision和nonce已匹配intent目标
- **THEN** 恢复判为已提交，只完成收尾，不重复归档或回退终态

#### Scenario: A session JSON was removed
- **WHEN** 后续请求发现session不存在但旧ledger目录仍在
- **THEN** 新session使用新generation，不从旧ledger自动恢复内容

### Requirement: Turn consumers SHALL preserve authoritative history semantics

admission、终态写入、recovery、answer-turn及sharing MUST通过热/冷统一查询访问记录。相同msgid的消息比较、chat_id绑定、稳定ID、权威status、隐藏上下文脱敏和既有完整历史响应 MUST保持；冷记录不能自动加入Agent上下文。

#### Scenario: A completed turn is retried with the same message
- **WHEN** 原记录已冷移出且收到相同msgid和相同message
- **THEN** admission得到与原完整dict比较相同的结果，不因热dict未命中重建admitted状态

#### Scenario: A failed or stopped historical turn is selected for sharing
- **WHEN** 它的状态已在冷存储
- **THEN** 资格判断读取权威终态，不能根据assistant输出推断为completed

#### Scenario: Recovery selects the latest Chat turn
- **WHEN** 新旧状态分布在热窗口与冷存储
- **THEN** 按原admission顺序及Chat绑定选择同一最新turn，不因回填顺序改变结果

### Requirement: New storage SHALL support controlled migration and complete rollback export

系统 SHALL默认保持旧格式，所有writer升级排空后才按租户灰度转换。旧二进制回退前 MUST停写、合并导出当前完整数据并验证；备份/恢复/删除工具 MUST识别新目录。

#### Scenario: Format creation is disabled after a session was migrated
- **WHEN** 开关关闭但该session已有新格式根
- **THEN** 新版reader/writer继续按该格式工作，不读取旧备份作为当前真源

#### Scenario: Rollback follows new writes
- **WHEN** 启用后已发生新的turn提交
- **THEN** 导出的旧格式包含这些提交和正确顺序，不能直接恢复迁移前备份代替导出
