# 04 Run 摘要和请求详情

Status: ready-for-agent
Type: task
Blocked by: 02, 03

## 目标

在现有 Run 详情中展示模型调用摘要，并支持点开每次请求查看真实执行事实。 [首批 Spec](../spec.md) · [Console 规格](../../agent-observability-console/spec.md)

## 验收

- [ ] Run 摘要展示逻辑调用数、请求尝试数和已获取的输入、输出 Token。部分覆盖时标明「已观测」，历史 Run 缺少请求级用量时留空。
- [ ] 点开请求能查看请求序号、模型、流式模式、结束原因、状态、耗时和用量。缺值显示「未获取」，真实零值显示 `0`。
- [ ] 用包含生产 SSE、重试、取消和用量缺失的执行链路核对页面和 API。用当前浏览器、真实 Chromium 108 及适用的 `hideMenu=true` 完成验收并记录实际版本。
