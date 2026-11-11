# 工作日识别接口：行内 SM3withSM2 鉴权

## 配置位置

只需要配置 Scheduler，SWE/Monitor 不持有这组凭据。

- dev：`scheduler/src/scheduler/config/envs/dev.json`
- prd：`scheduler/src/scheduler/config/envs/prd.json`

```json
{
  "SCHEDULER_WORKDAY_API_URL": "http://<行内网关>/<实际接口路径>?date={date}",
  "SCHEDULER_WORKDAY_API_APP_ID": "<应用appid>",
  "SCHEDULER_WORKDAY_API_APP_SECRET": "<应用appsecret>",
  "SCHEDULER_WORKDAY_API_PRIVATE_KEY": "<64位十六进制SM2私钥，无0x前缀或换行>",
  "SCHEDULER_WORKDAY_API_SIGN": "123"
}
```

上面的 URL 仅展示模板写法，不能直接使用。代码保留原有 URL 默认值，不根据 demo 的“您的请求地址”猜测网关路径。实际部署时填入工作日 API 真实地址，保留 `{date}`；请求仍为 GET，响应仍按原来的整数/字符串 0、1、-1 解析。

appid 和 sign 为无换行的可打印 ASCII Header 值。`sign` 是参与签名的业务参数，默认按 demo 填 `123`，不等于计算得到的 `apisign`。appsecret 不会作为请求 Header 或查询参数发送。

配置文件仅提交空凭据占位。真实私钥和 secret 在行内部署配置中填写并限制文件访问，不回传到聊天、不提交 Git、不放入源码传输包。环境变量仍优先于配置文件；已有空环境变量也会覆盖 JSON，因此应核对部署环境中的同名变量。修改文件后重新部署/启动 Scheduler。

**空配置不会发送无鉴权请求。已有有效缓存照常使用，缓存未命中时按原策略降级为周末规则；不能把 fallback 当作法定节假日结果。**

## SDK

在 Scheduler 使用的 Python 环境内，从行内批准的软件源安装用户提供的版本：

```text
python -m pip install ZA33_CMBSM==1.17.43
```

也可在已配置行内软件源的构建环境使用 Scheduler 的 `workday-auth` extra 安装该依赖。普通 Scheduler 安装不强制拉取私有包；未安装 SDK 时不影响服务启动或有效日历缓存读取，但不能成功刷新外部日历。

不要从不明公共源下载同名替代包，也不以其他算法实现替换行内 SDK。运行时仅使用 `CMBSM.CMBSMFunction.CMBSM2SignWithSM3` 和 `CMBSM.utils.com.aschex_to_bcdhex`，没有新增生成密钥接口。

## 请求协议

每次真正访问外部 API 时重新取得 Unix 秒时间戳，签名原文顺序固定为：

```text
appid=<appid>&secret=<appsecret>&sign=<sign>&timestamp=<timestamp>
```

原文以 UTF-8 编码，按 demo 调用 SM3withSM2，返回签名字节转小写 hex。发送 Header：

- `Content-Type: application/json;charset=UTF-8`
- `Connection: close`
- `appid`、`apisign`、`verify: SM3withSM2`、`timestamp`、`sign`

不添加 URL/日期/请求体到签名原文，不 URL 编码签名原文，不 Base64 编码签名。请求不跟随重定向，避免把鉴权 Header 发到其他地址。SDK 导入和签名在线程中执行，仍受原日历刷新总超时约束；超时后不会继续发出这次 HTTP 请求。

## 失败与缓存

只记录固定错误码，不打印 SDK 原始异常、密钥、签名原文或 apisign：

| 错误码 | 含义 |
|---|---|
| `workday_auth_config_missing` | 四个鉴权配置之一缺失或为空 |
| `workday_auth_config_invalid` | 私钥格式或 Header 配置不合法 |
| `workday_auth_sdk_unavailable` | SDK 未安装、无法导入或依赖不可用 |
| `workday_auth_signing_failed` | SDK 转换/签名失败，或没有返回有效签名字节 |

鉴权失败不尝试匿名调用、不增加自动重试。缓存表、刷新租约和旧降级策略均不变：API 结果缓存24小时，fallback结果缓存5分钟。配置修正后，现有缓存未到期可能不会立即重新请求外部接口。

## 行内验证

通过 Postman 调用 Scheduler 的既有入口（身份 Header 沿用当前环境网关要求）：

```text
POST /api/scheduler/cron/calendar/resolve
Content-Type: application/json
```

```json
{
  "at": "2026-11-04T09:00:00+08:00",
  "timezone": "Asia/Shanghai",
  "count": 0
}
```

选择一个尚未缓存或已过期的日期，由环境负责人核对本次网关确实收到请求且验签通过；仅看到 `source=api` 可能命中了旧缓存，不能单独证明本次鉴权成功。再分别核对一个已知假日和一个调休工作日。

失败时查看 `Workday API authentication unavailable: code=...`；401/403 等 HTTP 错误沿用 `Workday API unavailable` 日志和 fallback 行为。持续 fallback 时先核对 SDK 安装环境、四项配置、实际网关 URL 和机器时钟，不要把降级结果视为验收通过。

本机无真实 SDK、行内密钥或网关连通性；本地验证使用 SDK 替身和受控 HTTP，证明协议拼装、错误处理、缓存及调用链，不代表完成真实国密验签。

2026-11-04 验证：Scheduler 单测共1093项通过（265条既有 SQLite/FastAPI 弃用警告）；pre-commit 全部适用检查通过，包含复杂度上限15、mypy、Black、Flake8及Pylint。新增22项鉴权测试覆盖精确签名协议、UTF-8原文、SDK缺失/失败/无效结果、配置覆盖、缓存命中、超时及重定向。按工作区约定完成主线程自检，未进行独立审查。
