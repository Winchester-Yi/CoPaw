-- 在 Scheduler 实际连接的数据库中执行。
-- 业务日期按北京时间记录，缓存时间和刷新租约时间均按 UTC 记录。
CREATE TABLE IF NOT EXISTS swe_workday_calendar (
    calendar_date DATE NOT NULL COMMENT '业务日期（北京时间）',
    is_workday TINYINT NULL COMMENT '是否工作日：1工作日，0非工作日，NULL尚未查询',
    result_source VARCHAR(16) NULL COMMENT '结果来源：api外部接口，fallback周末规则兜底',
    fetched_at DATETIME NULL COMMENT '结果获取时间（UTC）',
    expires_at DATETIME NULL COMMENT '缓存结果过期时间（UTC）',
    refresh_token VARCHAR(32) NULL COMMENT '当前刷新请求的唯一标识，用于防止并发覆盖',
    refresh_until DATETIME NULL COMMENT '刷新租约过期时间（UTC）',
    PRIMARY KEY (calendar_date)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='工作日日历共享缓存表';
