CREATE TABLE IF NOT EXISTS swe_html_preview_click_events (
  id BIGINT PRIMARY KEY AUTO_INCREMENT,

  source_id VARCHAR(64) NULL COMMENT '来源标识',
  user_id VARCHAR(128) NULL COMMENT '点击用户标识',
  user_name VARCHAR(255) NULL COMMENT '点击用户名称/客户经理姓名',
  bbk_id VARCHAR(128) NULL COMMENT '分行/机构标识，预留给运营看板筛选',

  cron_task_id VARCHAR(128) NULL COMMENT '定时任务ID',
  cron_task_name VARCHAR(255) NULL COMMENT '定时任务名称',

  file_url TEXT NOT NULL COMMENT 'HTML 文件链接',
  file_name VARCHAR(512) NULL COMMENT 'HTML 文件名',
  list_key VARCHAR(1024) NULL COMMENT '名单稳定标识，默认使用文件链接',
  list_name VARCHAR(512) NULL COMMENT '名单展示名称，默认使用文件名',

  button_id VARCHAR(255) NULL COMMENT '按钮稳定标识',
  button_name VARCHAR(255) NULL COMMENT '按钮展示名称',
  button_text VARCHAR(512) NULL COMMENT '按钮文本兜底',
  button_type VARCHAR(32) NULL COMMENT '按钮类型：insight/phone/plan/other',
  customer_id VARCHAR(128) NULL COMMENT '客户唯一标识',
  customer_name VARCHAR(255) NULL COMMENT '客户展示名称',
  customer_info JSON NULL COMMENT '点击按钮所在行的客户扩展信息',

  event_type VARCHAR(32) NOT NULL DEFAULT 'button_click'
    COMMENT '事件类型：button_click/preview_view/module_exposure',
  template_type VARCHAR(16) NULL COMMENT '模板类型：main/sub',
  template_id BIGINT NULL COMMENT '当前事件所在模板ID',
  result_id VARCHAR(128) NULL COMMENT '当前模板生成结果ID',
  event_target_id VARCHAR(255) NULL COMMENT '模块的稳定标识',
  event_target_name VARCHAR(512) NULL COMMENT '模块的展示名称',
  trace_id VARCHAR(128) NULL COMMENT '方案生成与浏览链路标识',
  page_source VARCHAR(50) NULL COMMENT '打开 HTML 的页面来源',
  platform_source VARCHAR(50) NULL COMMENT '打开 HTML 的平台来源',

  clicked_at DATETIME NOT NULL COMMENT '前端点击时间',
  created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '入库时间',

  INDEX idx_clicked_at (clicked_at),
  INDEX idx_task_clicked (cron_task_id, clicked_at),
  INDEX idx_button_clicked (button_id, clicked_at),
  INDEX idx_button_type_clicked (button_type, clicked_at),
  INDEX idx_customer_clicked (customer_id, clicked_at),
  INDEX idx_list_clicked (list_key(255), clicked_at),
  INDEX idx_bbk_clicked (bbk_id, clicked_at),
  INDEX idx_source_clicked (source_id, clicked_at),
  INDEX idx_source_event_target_clicked (
    source_id, event_type, event_target_id, clicked_at
  ),
  INDEX idx_source_template_type_event_clicked (
    source_id, template_type, event_type, clicked_at
  ),
  INDEX idx_source_template_result_clicked (
    source_id, template_id, result_id, clicked_at
  ),
  INDEX idx_trace_clicked (trace_id, clicked_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='HTML 预览行为事件明细';

CREATE TABLE IF NOT EXISTS swe_html_preview_list_snapshots (
  id BIGINT PRIMARY KEY AUTO_INCREMENT,

  source_id VARCHAR(64) NULL COMMENT '来源标识',
  bbk_id VARCHAR(128) NULL COMMENT '分行/机构标识',

  cron_task_id VARCHAR(128) NULL COMMENT '定时任务ID',
  cron_task_name VARCHAR(255) NULL COMMENT '定时任务名称',

  list_key VARCHAR(1024) NOT NULL COMMENT '名单稳定标识，默认使用文件链接',
  list_name VARCHAR(512) NOT NULL COMMENT '名单展示名称，默认使用文件名',
  file_url TEXT NOT NULL COMMENT 'HTML 文件链接',
  file_name VARCHAR(512) NULL COMMENT 'HTML 文件名',

  customer_id VARCHAR(128) NULL COMMENT '客户唯一标识',
  customer_name VARCHAR(255) NOT NULL COMMENT '客户展示名称',
  extra_info JSON NULL COMMENT '客户扩展信息',

  snapshot_at DATETIME NOT NULL COMMENT '快照采集时间',
  created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '入库时间',

  INDEX idx_snapshot_source_list (source_id, list_key(255)),
  INDEX idx_snapshot_bbk_list (bbk_id, list_key(255)),
  INDEX idx_snapshot_customer (customer_id),
  INDEX idx_snapshot_at (snapshot_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='HTML 预览名单客户快照';
