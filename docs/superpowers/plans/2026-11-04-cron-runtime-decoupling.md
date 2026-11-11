---
status: completed
---

# CronManager 业务解耦与 Scheduler 查询收口

## 已确认边界

- 新版通知、心跳/梦境执行、会话裁剪及配置刷新不要求旧 CronManager 实例。
- 招呼任务进度及技能就绪检查的定时任务数据改由 Scheduler 查询；排程计算放 Scheduler，支持年份、工作日和可执行日期范围。
- 旧任务、旧 HTTP 接口、旧文件仓库、旧回调、旧管理器启动恢复与凭据预热继续保留。
- 不修改 swe_cron_jobs 同步写入、执行历史写入、共享模型池、聊天页、旧管理页；不做存量接管或迁移按钮。
- 本轮不提交、不推送、不打包，待用户另行要求。

## U1 独立 SWE 业务服务（测试先行）

从当前 CronManager 提取通知发送、系统执行和会话裁剪；参考本地历史 c6af48667，但保留当前已修复的 await 预热及所有旧业务语义。旧管理器继续暴露兼容方法；旧通知仍走原管理器分支，新通知直接接入 sender。系统清理保留旧清理分支，但不让旧分支失败阻止新版清理。

- 文件：src/swe/app/crons/{manager,notification_sender,notification_worker,system_runtime,system_actions,session_cleanup,managed_session_cleanup}.py；src/swe/app/source_system_config/task_scheduler.py。
- 测试：test_cron_business_services.py；test_managed_cron_notifications.py；test_system_cron_actions.py；test_cron_task_session_cleanup.py；test_managed_cron_session_cleanup.py；test_source_system_task_scheduler.py。
- RED：工作区 cron_manager 属性抛错时新版通知/系统动作不能运行；新版清理不能脱离旧 manager 的算法导入。
- GREEN：相同测试通过，既有旧通知/清理/来源上下文回归通过。

## U2 配置和工作区刷新挂点（测试先行）

配置先持久化再直接提交 system_bridge；把新版系统任务启动刷新从旧 manager 的 initialize 移到独立工作区服务挂点，保留旧用户任务恢复和旧兼容注册方法，避免重复刷新。

- 文件：src/swe/app/crons/{system_bridge,system_config,manager}.py；routers/{agent,config}.py；workspace/{workspace,service_factories}.py。
- 测试：test_system_cron_config_submission.py、test_system_cron_legacy_cutoff.py、独立工作区挂点测试。
- 失败语义：平台受理失败不能回滚已保存配置，仍返回同步失败信息；工作区刷新失败不阻塞其他服务启动。

## U3 Scheduler 业务查询（测试先行）

新增只读按日任务进度接口，复用 Scheduler 完整身份校验、新版分页仓库、日历/日期/年份能力，不读 SWE 文件或内存。进度是排程及最近状态的展示，不伪造逐次历史。技能检查使用新版分页列表；SWE 客户端校验身份、分页游标与响应结构，无旧数据降级。

- 文件：scheduler/app/routers/managed_cron.py、services/cron/managed/task_progress.py；src/swe/app/crons/managed_query_client.py。
- 测试：新 Scheduler progress 单测/HTTP 链路；SWE query client 传输、响应、分页、隔离测试。
- 覆盖：北京时间日期、六段年份、W/LW 与调休日、区间外/停用任务、批任务子任务逻辑日期、跨身份、异常响应、游标重复。

## U4 业务调用方切换（测试先行）

招呼卡片使用 Scheduler 日进度，不在 SWE 重算 cron；技能绑定与模型检测使用 Scheduler 列表，并保留本地模型/凭据检测逻辑。查询错误不得回退旧任务或被当成“没有任务”。

- 文件：src/swe/app/channels/zhaohu/channel.py；skill_readiness/{runner,strategies}.py。
- 测试：test_zhaohu_channel.py、skill_readiness/test_strategies.py、test_runner.py；新增无旧管理器场景。

## U5 兼容边界与验证

- 保留旧管理器实例创建、旧 API、迁移 capture_loaded_states、平台恢复/回调、旧表 projection 的现有行为。
- 明确过渡引用清单，避免将尚未停用说成已删除；不迁移本轮之外的入口。
- 运行 Windows 可用查询测试；含 fcntl 的 SWE 测试在 WSL 项目虚拟环境运行。
- 命令：PYTHONPATH=.:src:scheduler/src:monitor/src /mnt/d/Projects/CoPaw/venv/bin/python -m pytest <上述相关测试> -q --tb=short。
- 扩大回归：tests/unit/scheduler、相关 tests/unit/app、channels、workspace、skill_readiness；执行 diff --check、Black/Flake8 与复杂度检查。
- 自检按旧行为、身份隔离、日期日历、通知/清理状态更新、生命周期五个角度进行。用户约定主线程顺序执行，不声称独立审查。
- 分块交付边界：业务提取；查询适配；兼容与文档。仅记录边界，本轮不创建 Git 提交。
