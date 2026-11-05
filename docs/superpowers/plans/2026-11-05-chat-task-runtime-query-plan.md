# Chat task runtime query Implementation Plan

> Use superpowers:subagent-driven-development for independent backend units, then integrate and verify inline. Do not commit unrelated changes.

**Goal:** 用两套兼容新增接口替换周期全量任务读取。
**Architecture:** Scheduler 显式 SQL，legacy 已验证文件版本的紧凑只读快照，前端共享协调器按来源顺序批量读取。
**Tech Stack:** Python/FastAPI/MySQL、TypeScript/React、pytest/Vitest。

设计：../specs/2026-11-05-chat-task-runtime-query-design.md。

- [x] Legacy：新增 runtime_snapshot.py，稳定 fstat/path 签名、single-flight、不可变投影；api.py 新增 POST /jobs/-/runtime-statuses。先写并运行失败测试，再实现，覆盖替换、缓存、取消、损坏、身份、无写。
- [x] Scheduler：独立 runtime_query.py/typed models，managed_cron.py 新增 POST /-/runtime-statuses；复用身份与日期规则，显式 SELECT，不读取 spec_json。先失败测试，覆盖 ASGI、真实 SQLite 关系、权限和 200 边界。
- [x] Console：runtime loader/type 与协调器 poll 分支，200 分批、覆盖性验证、来源原子发布、版本/身份检查、404/405 按生命周期降级。先失败测试，保持旧 API。
- [x] 联合验证 Python/Vitest/typecheck/build/受影响 lint，检查旧接口、错误和取消行为；更新设计与 playbook，列明真实 MySQL/NAS/Chromium 未验证项。

现有上一工作包未提交变更保留；本轮按用户明确指令提交。临时依赖 symlink 在验证后清除。

验证结果：Python 94 passed；Console 540 passed、2 skipped（沿用既有失败排除条件）；TypeScript 编译及生产构建通过。受影响前端 lint 无错误、保留 Chat 既有 3 条 hooks 警告；新增/独立后端文件 Ruff 通过，api.py 既有未用导入单独记录。详见[实现报告](../reports/2026-11-05-chat-task-runtime-query-implementation.md)。真实 MySQL/NAS/Chromium 尚未验收，不属于已完成检查。
