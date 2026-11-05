# 会话软租约与代次校验设计

用户已选择“软租约＋代次校验”，沿用现有 schema、索引、NAS 格式和接口。

## 权威规则

- `lease_until` 只描述心跳新鲜度，过期可提示疑似失联；不是提交授权的时间边界。相同 owner、Session generation、active Turn 仍匹配时，允许 start、状态/消息/compact/reset 等 publication、正常终态和续心跳。心跳可在过期后恢复。
- `owner_key + generation + active_turn_id`、完整 scope/Session/Chat/Turn 关联、revision/epoch、删除及 Turn 状态共同决定写权限。任何代次变化、清除 owner、换 Turn、Stop 与终态语义仍按原事务校验拒绝旧写入，不跳过 NAS 读回或原 publication CAS。
- 请求代次高于或低于数据库都拒绝，不能自行填更大数字抢占；代次只能由持有 Session 行锁的准入/恢复事务递增。
- 租期过期不清除 active Turn，不允许不同问题接管，也不允许重复问题重跑。原有恢复必须有 owner stopped + IO drained 的可信证据；fence 在 Session 锁下递增 generation。并发结果以数据库锁和版本先后为准。

## 心跳失败

保留默认 30 秒心跳新鲜度、10 秒间隔和 10 秒单次 SQL 总预算，覆盖池排队、锁、提交与归还；保留同池动态控制容量和隔离连接排空。取消不吞掉，不增加池总连接数。

移除本地周期 25 秒硬截止与 admission 的本地过期拒绝。admission NAS 发布前启动心跳；过慢 reserve/publish 后是否能执行由 start 的当前 SQL 权威验证决定。

心跳超时、网络 OS 错误、驱动连接/运行异常、数据库连接数耗尽、锁等待超时/死锁，统一为 StorageUnavailable，仅在心跳任务中视为可重试。第一次进入异常状态记 warning，首次恢复记 info；后续失败每次至少等待一个常规间隔，防止数据库故障时紧密重试。成功按尝试开始时刻调度，延迟恢复后不补发积压心跳。

明确 ConflictError/IntegrityError、写屏障和非预期编程错误仍终止执行；只在失败同时存在本请求精确已提交 terminal 时保留原清理竞态保护。terminal 查询使用控制容量和独立 lease/3 SQL 时间预算，超时不覆盖原明确拒绝原因。未知 COMMIT 不等同业务发布失败：心跳仅更新新鲜度，在相同代次下再次心跳安全；业务 publication 的 UnknownCommit 继续原对账流程，不能因心跳变软就继续未经确认的业务状态。

## 范围与边界

数据库不可达时可继续已经运行的模型/工具计算；业务保存无法绕过数据库，仍可能失败。代次隔离只能保护本存储写入，不能撤销外部工具副作用。心跳过期、网络分区均不得作为自动重跑工具或确认旧执行死亡的依据。

复用所有原状态与字段，无需 DDL。旧版本仍将过期视为硬拒绝，因此需部署版本一致后验收，不能承诺混合版本下同样可接受迟到结果。此次不新增自动接管、后台恢复扫描或前端疑似失联状态。
