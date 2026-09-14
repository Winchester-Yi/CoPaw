-- 智能财富工作台 · 规划三表
-- 需手动导入 MySQL；应用启动不自动建表。
-- 对应代码：src/swe/app/wealth_plans/store.py

CREATE TABLE IF NOT EXISTS swe_wealth_plans (
  id VARCHAR(64) NOT NULL COMMENT '规划ID（后端生成）',

  sap_id VARCHAR(128) NOT NULL COMMENT '创建人SAP工号（X-User-Id）',
  creator_name VARCHAR(128) NULL COMMENT '创建人姓名（冗余展示）',
  bbk_id VARCHAR(64) NULL COMMENT '创建人所在分行号',
  source_id VARCHAR(128) NULL COMMENT '来源标识（iframeStore.source，如 RMASSIST）',
  agent_id VARCHAR(64) NULL COMMENT '预留：关联Agent ID，当前可为空',

  name VARCHAR(255) NOT NULL COMMENT '规划名称',
  description TEXT NULL COMMENT '规划说明',
  source_label VARCHAR(32) NULL COMMENT '来源标签：我的关注/行长关注/分行中台（按创建人角色派生）',
  period_start VARCHAR(16) NULL COMMENT '规划周期开始日期 YYYY-MM-DD',
  period_end VARCHAR(16) NULL COMMENT '规划周期结束日期 YYYY-MM-DD',

  status VARCHAR(32) NOT NULL DEFAULT 'publishing'
    COMMENT '发布编排状态：publishing/published/publish_failed',
  publish_error TEXT NULL COMMENT '发布失败原因',

  skill_dispatch_task_id VARCHAR(64) NULL COMMENT '技能/MCP 批量分发批次ID（batch_id，非任务ID）',
  skill_dispatch_status VARCHAR(32) NULL COMMENT '技能/MCP 分发状态',
  skill_dispatch_error TEXT NULL COMMENT '技能/MCP 分发失败原因',

  created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
  updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    ON UPDATE CURRENT_TIMESTAMP COMMENT '更新时间',
  published_at DATETIME NULL COMMENT '发布完成时间',

  PRIMARY KEY (id),
  INDEX idx_sap (sap_id),
  INDEX idx_bbk (bbk_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='财富工作台规划主表';

CREATE TABLE IF NOT EXISTS swe_wealth_plan_scenes (
  id BIGINT NOT NULL AUTO_INCREMENT,
  plan_id VARCHAR(64) NOT NULL COMMENT '关联规划ID',

  scene_id VARCHAR(64) NOT NULL COMMENT '场景技能ID（skillId）',
  item_id VARCHAR(64) NULL COMMENT '外部场景接口 itemId（发布回传用）',
  scene_name VARCHAR(255) NOT NULL COMMENT '场景名称（冗余存储，不依赖场景池展示）',
  category VARCHAR(32) NOT NULL COMMENT '产品大类英文code：insurance/finance/deposit/payroll/cross_border/fund',
  direction VARCHAR(512) NULL COMMENT '经营方向',
  cycle VARCHAR(16) NULL COMMENT '任务周期：本月/本季/今日/T+1日/自定义',
  start_date VARCHAR(16) NULL COMMENT '场景有效期开始日期 YYYY-MM-DD',
  end_date VARCHAR(16) NULL COMMENT '场景有效期结束日期 YYYY-MM-DD',
  cron_expr VARCHAR(64) NOT NULL COMMENT '由排程选择换算的cron表达式（建定时任务用）',
  mcp_relations VARCHAR(512) NULL COMMENT 'MCP依赖列表，逗号分隔存储',

  cron_job_id VARCHAR(64) NULL COMMENT '发布后回填：定时任务ID',
  broadcast_task_id VARCHAR(64) NULL COMMENT '发布后回填：广播分发任务ID（看板状态来源）',
  sort_order INT NOT NULL DEFAULT 0 COMMENT '展示顺序',

  PRIMARY KEY (id),
  INDEX idx_plan (plan_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='财富工作台规划场景明细表';

CREATE TABLE IF NOT EXISTS swe_wealth_plan_targets (
  id BIGINT NOT NULL AUTO_INCREMENT,
  plan_id VARCHAR(64) NOT NULL COMMENT '关联规划ID',

  sap_id VARCHAR(64) NOT NULL COMMENT '分发目标客户经理SAP工号',
  target_name VARCHAR(128) NULL COMMENT '目标姓名（冗余展示）',
  sort_order INT NOT NULL DEFAULT 0 COMMENT '展示顺序',

  PRIMARY KEY (id),
  INDEX idx_plan (plan_id),
  INDEX idx_sap (sap_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='财富工作台规划分发目标表';
