# W+ active-session 空结果

## 契约与范围

已有且归属匹配的 Chat 没有活动 W+ SOP 时，GET
`/wplus-sop/chats/{chat_id}/active-session` 返回 HTTP 200、JSON `null`。
有活动会话时仍返回原有快照；Chat 不存在、身份或归属不匹配仍返回 404。
查询时机不变，前端必须能从已有快照安全切换为空状态并解除输入锁。

## 实施与验证

1. 在 `tests/unit/app/wplus_sop/test_router.py` 增加真实 HTTP 空结果和 Chat 归属回归；
   在 `console/src/pages/Chat/components/WPlusSopActiveBar/index.test.tsx` 增加空结果及刷新清空回归。
   先运行并确认旧实现失败。
2. 修改 `src/swe/app/wplus_sop/router.py` 的返回类型与空结果；
   修改 `console/src/api/modules/wplusSop.ts` 返回类型并在状态栏保护空快照。
3. 在 `analysis/playbook/location-paths.md` 记录查询时机与空结果契约。
4. 使用 WSL 项目 `venv/bin/python -m pytest tests/unit/app/wplus_sop/test_router.py -q`；
   Console 运行 `npm run test:run -- src/pages/Chat/components/WPlusSopActiveBar/index.test.tsx src/api/modules/wplusSop.test.ts`、
   `npm run typecheck`，并对修改文件检查 ESLint、Prettier。复核 diff，不执行提交。

本次在主线程顺序实施与自查；当前环境未找到 Superpowers 技能。
