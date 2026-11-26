---
name: cron
description: Use when the user requests future scheduled or recurring work, or asks to inspect or manage an existing scheduled task. | 用户要求未来定时、周期执行，或查询和管理已有定时任务时使用。
metadata:
  builtin_skill_version: "1.5"
  swe:
    emoji: "⏰"
---

# Cron（Scheduler 定时任务）

## 适用范围

用于未来或周期性执行。当前对话里立即完成一次的工作，不应因此创建定时任务。修改已有任务使用 update，保留原任务 ID；不要用 delete + create 代替修改。

所有命令使用 Scheduler 新接口，不查询 SWE 旧任务，不读写 jobs.json。旧任务尚未接管而查询不到时，说明需要迁移，不擅自重建。

## 身份和地址

- 每条命令必须显式传 `--agent-id <agent_id>`，使用系统提示中的实际 Agent。
- tenant/source 默认读取运行时 `SWE_TENANT_ID`、`SWE_SOURCE_ID`，也可逐命令传 `--tenant-id`、`--source-id`。缺少来源要先确认，不能编造默认来源。
- 实际操作者与 tenant 不同时，在子命令前传 `swe cron --user-id <实际操作者> ...`。不得冒充他人或附加管理员身份。
- 地址优先使用命令 `--base-url`，其次是显式全局 `swe --host ... --port ... cron ...`，再用 `SWE_SCHEDULER_API_URL`，最后默认本地 9100 端口。不要把 SWE 8088 地址当作 Scheduler。

## 创建任务

先确认名称、执行内容、时间/周期、时区和渠道；缺少业务信息再向用户澄清。新任务的 ID、聊天和会话绑定由 Scheduler/SWE 准备，不需要让用户提供 task_session_id。

```bash
swe cron --request-id <本次创建请求ID> create \
  --agent-id <agent_id> \
  --type agent \
  --name "每日待办报告" \
  --cron "0 9 * * *" \
  --timezone Asia/Shanghai \
  --channel console \
  --text "汇总今天的待办事项"
```

- `text` 类型发送固定文本；`agent` 类型执行提问；完整定义（包括 workflow）可用 `create -f job_spec.json`。
- 创建 ID 不从文件指定，也不使用旧任务 ID。兼容参数 `--target-session` 不能指定新的任务会话绑定；以返回详情为准。
- 默认普通调度，非工作日默认不执行。复杂日历策略使用当前 Scheduler 支持的完整 JSON 定义，不能凭普通 cron 推断法定工作日。

## 回检（可选）

回检是在任务产出结果后延迟一段时间，回到**原任务会话**里追问一次，确认结果是否真的落地。只有用户明确说“跑完 X 分钟后检查一下”“确认报告有没有生成”时才加；用户没提就不要主动加回检。

```bash
swe cron --request-id <本次创建请求ID> create \
  --agent-id <agent_id> \
  --type agent \
  --name "每日待办报告" \
  --cron "0 9 * * *" \
  --timezone Asia/Shanghai \
  --channel console \
  --text "汇总今天的待办事项" \
  --recheck-after 30m \
  --recheck-instruction "检查今天的待办报告是否已生成并发送，未完成时说明缺了哪一步"
```

- `--recheck-after` 支持 `90s`、`30m`、`2h`、`1d` 或纯秒数，范围 60 秒到 7 天；`--recheck-instruction` 写清“检查什么、什么算成功或失败”，1–4000 字符。用户给的是自然语言延迟（“半小时后”）时换算成前者，不要自己编造更长的延迟。
- 两个参数必须同时提供，且只支持 `--type agent`。只给其中一个、用于 `text` 任务、延迟越界都会直接报参数错误；不要改用 `-f` 文件绕过这些校验。
- 回检在原任务会话里作为一次新的执行运行，不重跑父任务；父任务输出缺失或超长时回检记为 `cron_recheck_parent_output_unavailable` 并从结果里报出来，不会静默丢一档。
- 触发时间是“父任务产出结果后 + 延迟”，不是 cron 到点时间。需要按业务时效判断时，把这一点跟用户说清楚。
- 更新已有任务用同样的参数；取消回检用 `--no-recheck`。只改名称、时间、渠道等元信息不会动已有回检。

## 先核对回执，再报告完成

创建/更新/删除/启停返回 `operation` 和 `request_id`，不是任务定义。创建的任务 ID 从 `operation.job_id` 取得。

- `operation.status=completed`：本次管理动作完成，可以查询任务详情核对。
- `pending/running`：仍在处理，继续查询，不能报告创建或删除已经完成。
- `failed/superseded` 或 HTTP 503/409：可能已落库，必须保留原 job_id、operation_id、request_id；不是可以直接新建另一条任务的信号。

```bash
# 查询原管理操作；返回裸操作对象，检查 status
swe cron operation <job_id> <operation_id> --agent-id <agent_id>
# 没有操作 ID 时查询当前未完成操作；operation=null 不等于任务不存在
swe cron operation <job_id> --agent-id <agent_id>
# 查明失败原因、确认可以续办后，重试同一管理操作
swe cron retry <job_id> <operation_id> --agent-id <agent_id>
```

创建请求超时、未拿到任务 ID 时，保留 stderr 中的 request_id；只有按完全相同的创建参数复用该 ID 重提，不能换 ID 再创建。其他写操作超时先查原任务及未完成操作，不自动重放。

## 查询和管理

```bash
# list 自动遍历全部分页，只返回新任务
swe cron list --agent-id <agent_id>
# get 返回 spec、state、task，以及定义和管理版本
swe cron get <job_id> --agent-id <agent_id>
# state 包含运行状态、会话视图、调度模式和管理进度
swe cron state <job_id> --agent-id <agent_id>
# 局部修改先读取最新定义，自动附版本；保留 job/session/chat ID 和未修改的执行内容
swe cron update <job_id> --agent-id <agent_id> --name "新名称"
swe cron update <job_id> --agent-id <agent_id> --cron "0 10 * * *"
# 启停只作用于本任务；不要把 pause 当成暂停父任务及全部子任务
# 启停必须独立操作，不在 update 中切换 enabled
swe cron pause <job_id> --agent-id <agent_id>
swe cron resume <job_id> --agent-id <agent_id>
# 删除先逻辑删除，资源清理可能继续；根据 operation 判断是否完成
swe cron delete <job_id> --agent-id <agent_id>
```

版本冲突时重新查询，让用户确认最新定义后再发起新动作，不盲目覆盖。update 不能转移所属用户或替换已有会话绑定；有分发子任务时，源正文更新仍受 Scheduler 限制。

## 手动执行已有任务

```bash
swe cron run <job_id> --agent-id <agent_id>
# 从上一步的 execution.execution_id 取得本次执行 ID
swe cron execution <job_id> <execution_id> --agent-id <agent_id>
```

`run` 返回已受理的 `execution`，不代表执行成功；它不是 management operation，不能用 `retry <operation_id>` 重跑业务。查询同一 execution_id；存在异步子任务时，还要检查 settled_status 等整轮终态，不能只看主执行 success 就宣布全部成功。

手动运行不受自动工作日过滤，但仍受停用和可执行日期区间限制。不要用手动运行验证“非工作日不通知”：手动成功完成提醒有意豁免该来源过滤。

## 常见 cron

```text
0 9 * * *      每天 09:00
0 */2 * * *    每两小时
30 8 * * 1-5   周一到周五 08:30（仍受日历准入影响）
*/15 * * * *   每十五分钟
0 9 LW * *     每月最后一个实际工作日 09:00
0 9 15W * *    每月 15 日（含当天）及之前最近的实际工作日 09:00
0 9 LW * * 2027  仅 2027 年每月最后一个实际工作日 09:00
```

新版任务接受五段，或末尾增加年份的六段；第六段是年份，不是秒。年份可用 `2027`、`2026,2027`、`2026-2029`；不要传秒在最前的六/七段平台表达式。W/LW 不与星期限制组合，工作日以服务端日历为准，异常降级按普通周末规则。

`nW`只向前、不跨月；n号本身是工作日就当天执行，短月从月末开始查找。当月没有符合日期就跳过，例如1号休息时`1W`不会顺延到2号，也不会跨到上月。该规则不补跑已过去的日期；`LW`仍表示当月最后一个实际工作日。

创建省略 `schedule.run_on_non_workday` 时默认 false；需要非工作日也执行时，使用完整 JSON 定义明确设 true。`run_date_range` 是北京时间包含首尾日的区间，不传表示不限；更新名称等字段必须保留现有区间和开关。已有子任务的父任务修改日期须使用 Scheduler 的父组日期接口，不通过普通内容更新绕过限制。

任务执行开关不控制完成通知。来源 `cron_notifications.skip_weekend_zhaohu_enabled=true` 时，自动任务按原定通知的北京时间日期过滤招呼提醒；结果和未读仍保留，已有待发送历史不会因此自动取消。

不要手动写 jobs.json、调用 SWE 旧 /cron/jobs 接口或自行修改数据库。CLI 成功退出仅说明命令已获得有效回执；是否完成以 operation/execution 内容为准。
