# 批调度独立运行状态：升级与验收

## 行为与兼容范围

- 普通任务不初始化控制记录，不改变自身启停与普通定时器。批调度子任务没有整批开关。
- 只有当前、未删除的批调度父任务才初始化。缺少控制记录时，首次按父任务自身 enabled 保守继承：开启为运行，关闭为暂停，包括未读自动关闭。已有独立状态永不重新继承。
- 初始化后，父任务自身关闭只跳过自身执行；其他启用子任务仍可派发。恢复父任务自身不会恢复已独立暂停的整批。
- 整批暂停不创建新批次、不发放新交接许可；待执行和重试保留队列。尚未交接的新领取退回并返还领取次数；已交接、运行任务自然收尾。
- 单任务关闭在交接前检查，终结为 skipped，释放占用，不自动重试，不计入 Worker 成功或失败反馈。恢复整批不复活已跳过任务。
- 恢复按原顺序与共享 Worker 限制继续，不补建暂停期间错过的时点。

批次结果 received/pending/running/completed/failed 与整批运行状态分开。全部终结且无失败的批次为 completed，但成功数与“跳过／取消”数分别统计，跳过不算成功。

## 增量结构

| 位置 | 增量 | 用途 |
|---|---|---|
| swe_cron_dispatch_controls | 新表，source/tenant/parent 联合主键 | paused、版本、恢复时间、最后操作人 |
| swe_cron_dispatch_intents | claim_token VARCHAR(36) NOT NULL DEFAULT '' | 一次领取的唯一标识，隔离退回后的旧协程 |
| swe_cron_dispatch_batches | skipped_count INT NOT NULL DEFAULT 0 | 跳过及历史取消计数 |

控制表由 Scheduler 写入，Monitor 只读。不把状态写入会被定义同步覆盖的 job meta。SWE 通过内部 HTTP 操作 Scheduler，不 import Scheduler 包。

批次内优先策略和独立暂停／恢复位于定时任务管理操作列三个点下拉菜单的“批调度配置”。普通任务及批调度子任务的菜单项置灰；已启用批模式的父任务即使自身关闭或整批暂停，仍可配置。广播弹窗保留接收人、调度方式及散列设置，不再加载策略和运行状态。

SWE→Scheduler 的批调度状态、初始化、失败预览和重试接口不要求 Scheduler token。Scheduler→SWE 的 `/api/internal/cron/callback` 同样不发送或校验内部 token，普通外部调度平台回调也不再依赖它。SWE 管理入口仍校验 manager/admin、来源和任务归属，Scheduler 仍要求来源与操作人并校验业务范围及版本。双方信任内部调用者，身份请求头本身不是鉴权证明，必须通过内网访问限制／网关隔离这些管理及执行入口，禁止公网直连。其他 SWE 内部接口、Market 认证、模型鉴权和防旧领取的 claim_token 不变。

## 上线顺序

1. 在维护窗口阻止新批回调和旧 Scheduler 派发，保留在途任务结果接收。冻结任务定义修改，确认 Monitor 定义同步没有积压；不要把在途意图批量改回 pending。
2. 备份相关表，确认 Scheduler 和 Monitor 访问同一批次、意图及执行数据，SWE 的 Scheduler 地址已配置；限制 Scheduler 管理接口和 SWE Cron 回调只允许可信内部调用。同步升级两端，避免旧 SWE 仍要求 token 而新 Scheduler 已不发送；其他服务使用的 SWE_INTERNAL_TOKEN 不要全局删除。
3. 由数据库维护人员执行下方建表及补列 SQL。先确认两列尚不存在；已经存在的列不要重复添加。
4. Scheduler 启动时自动初始化缺失的合格父任务控制记录，不需要调用初始化接口或执行迁移命令。已有独立状态不覆盖；缺失身份、异常元数据和非批调度父任务不会默认启用。检查因未读自动关闭而首次初始化为暂停的父任务，按需单独恢复批调度。
5. 升级 Scheduler、SWE、Monitor；确认全部旧派发实例退出后再恢复批回调。**不能新旧 Scheduler 混跑**：旧进程不知道控制表与 token，不能保证暂停和领取隔离。
6. 更新 Console，检查独立开关和结果查询。在测试环境完成下述真实 MySQL 并发验收，再放开真实调度。

本次上线采用手工 SQL、启动自动补齐，**不进行历史回填**。以下 SQL 仅执行于确认过的业务库；Scheduler 和 Monitor 共用这些表时只执行一次：

```sql
CREATE TABLE IF NOT EXISTS swe_cron_dispatch_controls (
    source_id VARCHAR(64) NOT NULL,
    tenant_id VARCHAR(64) NOT NULL,
    parent_job_id VARCHAR(64) NOT NULL,
    paused TINYINT(1) NOT NULL,
    version BIGINT NOT NULL DEFAULT 1,
    resumed_at DATETIME(6) DEFAULT NULL,
    updated_by VARCHAR(64) NOT NULL DEFAULT '',
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (source_id, tenant_id, parent_job_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

ALTER TABLE swe_cron_dispatch_intents
    ADD COLUMN claim_token VARCHAR(36) NOT NULL DEFAULT '';
ALTER TABLE swe_cron_dispatch_batches
    ADD COLUMN skipped_count INT NOT NULL DEFAULT 0;
```

DDL 执行需要 CREATE、ALTER 权限；运行服务需要相应 SELECT、INSERT、UPDATE 权限。MySQL DDL 不能整体事务回滚，部分成功时先检查结构，仅补执行尚未成功的语句。

不回填意味着旧批次的 skipped_count 默认是 0，历史跳过／取消计数及部分批次终态可能保留旧值。新批次正常统计，仍在运行的旧批次在后续触发汇总时重新计算；不承诺所有历史批次自动修正。不执行历史重跑、重置重试次数或批量修改 intent 状态。仓库中保留的迁移工具及历史计划不是本次上线步骤，不执行其中的 apply 回填流程。

Scheduler 启动只做 schema 就绪检查及缺失控制初始化，**不会自动执行 DDL**。缺表、缺列或权限错误会阻止派发循环启动；HTTP 服务能响应不代表派发已启动，应检查 `Cron Scheduler loop started` 和迁移错误日志。

## 外部批定时器启停联动与修复

SWE 的 `PUT /api/cron/jobs/{job_id}/batch-dispatch/run-state` 收到 paused=true 和当前 expected_version 后，先持久化 Scheduler 内部暂停，再暂停父任务 meta 中 batch_dispatch_external_job_id 对应的外部批定时器。内部状态更新失败或版本冲突时，不调用外部暂停。已经发出的批回调仍可能到达，但会被内部暂停门控忽略；已交接、运行的任务自然收尾。

外部暂停失败时返回明确的部分失败提示，内部仍保持暂停，不自动恢复。刷新确认内部状态后，可点击仅暂停状态下显示的“同步外部暂停”，用当前版本重复提交 paused=true。升级不会批量同步旧的暂停任务，需要时逐个使用该按钮；GET 和“刷新状态”只读，不会修改外部平台。

恢复顺序保持相反：paused=false 先恢复记录的批定时器，再修改内部状态。即使控制记录本来是运行，也可用该请求定向修复物理定时器。批定时器 ID 缺失、适配器不可用或与普通定时器 ID 冲突时明确报错，绝不回退操作 external_job_id，不遍历其他租户；外部恢复失败不会把控制状态改为运行。

内部控制与外部平台不是同一事务，也不持续轮询对账。若发生部分失败，或有人在外部平台直接修改状态，应核实两端并按上述方式重新同步。本次联动仅需更新 SWE 和 Console，不增加表或字段，不改变父任务自身 enabled、普通定时器或子任务定时器。

若提示“配置已保存，但运行状态初始化失败”，先恢复 Monitor／Scheduler 连通性，再重试该父任务原有启用接口或执行兼容初始化。GET 和页面“刷新状态”保持只读，不会隐式进行迁移。

## 并发与故障边界

- 领取锁序为模型 scope→control→batch→intent；暂停、人工重试和交接遵守 control→batch→intent，不反向获取 scope。
- 在 REPEATABLE READ 下，交接和人工重试在事务外读取不可变的批次身份，进入事务后先获取 control 锁，再检查任务定义，避免等待期间任务已关闭却仍读到旧快照。
- 初始化由已存在的 job 行锁串行化同一父任务；缺失 control 的存在性检查不加 FOR UPDATE，避免不同父任务并发插入同一索引空隙时死锁。唯一键仍保证控制记录不重复。
- 超时清理在领取前的独立回收阶段提交，领取事务不先锁超时意图再获取 control。已有当前尝试终态回执的旧 claimed/acknowledged 也必须先结算，不能被兜底领取重复执行。
- 暂停与交接许可按 control 行锁排序。暂停返回后不再发放新许可，但之前获许可的回调可能仍在路上；页面提示交接中和运行中任务自然收尾。
- 新 claimed 携带唯一 token，退回后旧 token 失效。旧空 token 记录无法证明尚未执行，沿用执行反馈和超时收敛，不立即返还次数或重跑。
- acknowledged 是持久化的交接许可，使用交接后超时预算，不按短领取超时回收。
- 回调不确定，或 SWE 接受／明确跳过后 Scheduler 写状态失败，不立即当作回调失败重试；保留交接占用，由持久化回执和后续恢复收敛。数据库不可用时不能声称结果已成功落库。
- 权限不足在工作区解析前拒绝，source、父 tenant、父 job 精确校验。版本冲突返回 409，刷新后再操作。
- 保存失败时 UI 保留上次结果并标注“状态待确认”，刷新前禁止再次切换，不显示虚假的保存成功。
- 暂停期间授权失败任务重试只入队并提示等待恢复；不绕过必要的旧执行停止确认、一次额外尝试授权或 Worker 限制。鉴权／配置错误不再要求勾选已修复，实际执行仍按真实配置校验；Console 与 Scheduler 需配套升级。

## 只读核验

以下 SQL 用于定位，最终归属判断应采用迁移一致的 JSON 解析规则，不能只看 meta LIKE '%batch%'。

```sql
SELECT c.source_id,c.tenant_id,c.parent_job_id,c.paused,c.version,
       c.resumed_at,c.updated_by,j.enabled,j.status,j.deleted_at,j.meta
FROM swe_cron_dispatch_controls c
LEFT JOIN swe_cron_jobs j ON j.id=c.parent_job_id
 AND j.source_id=c.source_id AND j.tenant_id=c.tenant_id;

SELECT batch_id,total_count,completed_count,failed_count,skipped_count
FROM swe_cron_dispatch_batches
WHERE status IN ('completed','failed')
 AND total_count<>completed_count+failed_count+skipped_count;

SELECT b.parent_job_id,i.batch_id,i.id,i.status,i.attempt_count,
       i.claim_token,i.lock_owner,i.locked_at
FROM swe_cron_dispatch_intents i
JOIN swe_cron_dispatch_batches b
 ON b.batch_id=i.batch_id AND b.source_id=i.source_id
JOIN swe_cron_dispatch_controls c
 ON c.source_id=b.source_id AND c.tenant_id=b.tenant_id
 AND c.parent_job_id=b.parent_job_id
WHERE c.paused=1 AND i.status IN ('claimed','acknowledged','dispatched');
```

最后一项不要求为空：acknowledged/dispatched 可能是在途任务，旧空 token 的 claimed 也需原有恢复逻辑。应检查暂停之后是否产生新交接许可，不能直接取消所有运行记录。

## 验收与回滚

本轮相关 Python 测试 312 项、Console 测试 79 项通过，TypeScript、定向 ESLint/Prettier 与构建通过；新 Python 生产模块 Flake8 通过，真实组件在 1280/1440 宽度的隔离浏览器中验证了暂停、结果计数和保存失败状态。保留了既有的测试环境告警和构建大包告警，没有通过更改全局配置隐藏它们。

SQLite 持久化测试去掉了 MySQL 锁语法，不能单独证明行锁行为。2026-09-10 已补充真实 MySQL 8.1.0、REPEATABLE READ 验证：10 个场景连续 10 轮通过，并查询 performance_schema.data_lock_waits 确认实际竞争等待。原 312 项定向回归通过。

实库测试复现并修复了两类问题：不同父任务初始化的间隙锁死锁；交接及人工重试在等待控制锁期间读到旧快照。覆盖还包括同父并发初始化幂等、启停后不覆盖初始状态、暂停先于交接、交接先于暂停、同模型容量限制、旧 token 拒绝、父任务关闭不连带停批、恢复时间微秒精度及入队事务回滚。该结果不代替行内具体版本和部署配置的预发布验收。

### 重跑真实数据库测试

文件：`tests/integrated/scheduler/test_batch_run_state_mysql.py`。默认跳过；只接受显式配置的 loopback 临时实例。测试不读取业务 DB 配置，校验服务端数据目录、server_id=6060910 和非默认高位端口后，才创建随机测试 schema；每个用例结束删除自己的 schema。

运行前启动独立 MySQL 实例：使用 `--no-defaults`，仅绑定 127.0.0.1，端口例如 33379，`--server-id=6060910`，数据目录为 `copaw-cron-mysql-<随机标识>/data`。使用一次性密码，禁止复用业务库。项目虚拟环境需要 Scheduler 已声明的 aiomysql 依赖；本轮使用 aiomysql 0.3.2 / PyMySQL 1.2.0。

```powershell
$env:PYTHONPATH='scheduler/src;monitor/src'
$env:CRON_MYSQL_TEST_PORT='33379'
$env:CRON_MYSQL_TEST_DATADIR='临时实例的绝对数据目录'
$env:CRON_MYSQL_TEST_PASSWORD='该临时实例的一次性密码'
.\.venv\Scripts\python.exe -m pytest tests/integrated/scheduler/test_batch_run_state_mysql.py -q
```

此测试要求能够读取 performance_schema 锁等待和创建／删除测试 schema。运行后关闭临时实例，不保留测试口令；没有上述显式环境变量时不会连接数据库。

回滚执行层前必须停批回调和派发并收敛在途。保留新增表、列、token 和跳过记录，不直接让旧 worker 接管新状态。仅回滚 Console 不删除状态，但旧页面无法管理新开关；执行层回滚后应保持批调度停用，核对数据后另行决定恢复方式。
