# 并行发现的源校验契约

本次并行改造保留 `e7798fa49`（2026-11-09，tenant mtime sync boundary）建立的行为：新 discovery report 的 `fingerprints` 为空，discover/freeze 不计算全源内容哈希，也不因此引入全源权限模式检查。CLI 的语义盘点改为有界 workspace 解析和 spool，但不改变这个源绑定契约。

对已经携带非空 `fingerprints` 的旧 report，盘点前后仍核对既有指纹。对于新报告，输入完整性继续依赖从 discovery 到 finalize 全程停写与可靠存储冻结；租户同步起点后的 mtime 检查只作为辅助，不能证明同长度且保留 mtime 的替换不存在。不能将 CLI 通过描述为源内容不可变的证明。

回归通过 monkeypatch 禁止 `fingerprints` 调用，实际执行 discover、freeze 及后续 inventory/plan/inspect CLI，防止并行辅助函数重新引入整树哈希。
