## 设计与实施验收

- [x] 核对串行链路、SQL任务归属、共享store及journal全量开销，形成设计。
- [ ] 固化串行基线与阶段耗时、同步IO清单。
- [ ] 实现有界IO执行器、逐workspace临时盘点及全局验证屏障。
- [ ] 实现同task SQL协调与workspace私有执行上下文，workers=1回归。
- [ ] 拆分Publication SQL/NAS阶段，移除共享store切换；实现journal增量更新/分页恢复。
- [ ] 实现workspace并发导入、分享阶段及受限验证/finalize。
- [ ] 接入CLI参数、背压、进度、取消排空与明确失败报告。
- [ ] 通过串行/并行语义、故障续跑、作用域隔离、内存/临时盘边界回归。
- [ ] 完成至少10,000个代表性workspace的专用MySQL和真实NAS性能/恢复验收。
- [ ] 更新迁移手册与放行证据。
