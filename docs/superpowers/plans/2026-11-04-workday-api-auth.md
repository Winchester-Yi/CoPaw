---
status: completed
---

# 工作日接口行内 SM3withSM2 鉴权

## 范围和协议

- 只修改 Scheduler 外部日历 HTTP 请求，SWE、Monitor、数据库结构、旧 cron 接口不变。
- 按用户 demo 精确签名 UTF-8 字节：`appid=<appid>&secret=<appsecret>&sign=<sign>&timestamp=<Unix秒>`；不排序、不 URL 编码、不加入 URL 或请求体。
- 调用行内 SDK `CMBSM2SignWithSM3(com.aschex_to_bcdhex(private_key), message)`，将返回签名字节 `.hex()` 放到 apisign；verify 固定 SM3withSM2。
- URL 继续使用现有 `{date}` 模板和 0/1/-1 响应协议；当前没有新的网关路径，不猜测替换 URL。
- dev/prd JSON 增加 APP_ID、APP_SECRET、PRIVATE_KEY、SIGN；只写空凭据占位，sign 示例默认123。沿用环境变量优先级，不增加免鉴权开关。
- 鉴权配置/SDK/签名失败时不发起无签名请求，不跟随重定向；已有日历缓存、刷新租约、2秒总超时及周末 fallback 策略不变。
- 不记录凭据、原始签名串、签名值或第三方原始异常。真实 SDK 没有安装，不能声称已通过真实验签。

## 实施和验收

1. 测试先行：新增 tests/unit/scheduler/test_workday_api_auth.py，先验证缺失模块导致 RED；覆盖精确消息与 Header、每次新时间戳、配置文件及环境覆盖、签名错误不泄露、不请求、有效缓存绕过签名。
2. 新增 services/cron/workday_api_auth.py，使用 SDK 原函数与延迟导入。SDK 作为 Scheduler 的 `workday-auth` 可选依赖声明，部署从行内批准的软件源安装；不通过公共源寻找同名替代包。
3. _fetch_workday 在已有总超时内在线程中生成签名，再发 GET；既有响应解析及缓存不变。调整原 HTTP 测试使用受控签名替身，不放宽鉴权要求。
4. 验证配置、日历 HTTP、缓存及调用方回归；pre-commit、复杂度<=15、diff检查。主线程进行协议、泄露、失败、兼容性自检，按工作区约定不使用子代理，不宣称独立审查。
5. 更新部署说明，列出必填参数、SDK安装、缓存生效和验证限制。不开实际行内网络请求，不提交、不推送、不打包。

测试命令：`PYTHONPATH=.;src;scheduler/src;monitor/src D:/Projects/CoPaw/.venv/Scripts/python.exe -m pytest tests/unit/scheduler/test_workday_api_auth.py tests/unit/scheduler/test_workday_calendar.py tests/unit/scheduler/test_workday_calendar_config.py -q --tb=short`；完成后运行 scheduler 单测回归。
