-- 为 HTML 预览行为事件补充打开页面和平台来源。
-- 新列允许为空，以兼容历史数据和尚未升级的客户端。
-- 本迁移是一次性前向迁移，不应在同一数据库重复执行。
-- Forward-only：生产回滚应先停止写入新字段，再通过新的前向迁移处理；
-- 直接 DROP 新列会永久丢失已采集的来源信息，因此不提供破坏性 DOWN。

ALTER TABLE swe_html_preview_click_events
ADD COLUMN page_source VARCHAR(50) NULL COMMENT '打开 HTML 的页面来源',
ADD COLUMN platform_source VARCHAR(50) NULL COMMENT '打开 HTML 的平台来源',
ALGORITHM=INPLACE,
LOCK=NONE;
