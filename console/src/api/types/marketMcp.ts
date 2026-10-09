/**
 * 市场 MCP 相关类型定义
 */

/** MCP 配置详情（脱敏展示） */
export interface MCPConfigDetail {
  /** 显示名称 */
  name: string;
  /** 描述 */
  description: string;
  /** MCP 传输类型 */
  transport: "stdio" | "streamable_http" | "sse";
  /** HTTP/SSE URL */
  url: string;
  /** HTTP headers（脱敏展示） */
  headers: Record<string, string>;
  /** stdio 命令 */
  command: string;
  /** 命令行参数 */
  args: string[];
  /** 环境变量（脱敏展示） */
  env: Record<string, string>;
  /** 工作目录 */
  cwd: string;
}

/** 市场 MCP 列表项 */
export interface MarketMCPItem {
  /** 市场 item ID */
  item_id: string;
  /** MCP client key */
  client_key: string;
  /** 显示名称 */
  name: string;
  /** 中文名称 */
  chinese_name?: string;
  /** 描述 */
  description?: string;
  /** 使用指引 */
  guidance?: string;
  /** 版本 */
  version?: string;
  /** 创建人 ID */
  creator_id?: string;
  /** 创建人名称 */
  creator_name?: string;
  /** 分类 ID */
  category_id?: number | null;
  /** 可见机构 */
  bbk_ids?: string[];
  /** 创建时间 */
  created_at?: string | null;
  /** 更新时间 */
  updated_at?: string | null;
  /** 调用次数 */
  call_count: number;
  /** 使用人数 */
  user_count: number;
  /** 内容未变化，市场版本未增加 */
  version_unchanged?: boolean;
}

/** MCP 用户使用统计 */
export interface MCPUserStat {
  /** 用户 ID */
  user_id: string;
  /** 用户名 */
  user_name: string;
  /** 调用次数 */
  call_count: number;
}

/** 市场 MCP 详情 */
export interface MarketMCPDetail extends MarketMCPItem {
  /** MCP 配置（脱敏展示） */
  config: MCPConfigDetail;
  /** 用户使用统计列表 */
  user_stats: MCPUserStat[];
}

/** MCP 上传请求 */
export interface MCPUploadRequest {
  /** 上传的 MCP 配置文件（与 raw_json 二选一） */
  file?: File;
  /** 直接粘贴的 JSON 配置字符串（与 file 二选一） */
  raw_json?: string;
  /** 显示名称 */
  name: string;
  /** 中文名称 */
  chinese_name?: string;
  /** 描述 */
  description?: string;
  /** 使用指引 */
  guidance?: string;
  /** 关联 BBK ID 列表 */
  bbk_ids?: string[];
}

/** MCP 上传响应 */
export interface UploadMCPResponse {
  /** 是否成功 */
  success: boolean;
  /** 错误信息 */
  error?: string;
  /** 内容未变化，市场版本未增加 */
  version_unchanged?: boolean;
}

/** MCP 分发请求 */
export interface MCPDistributeRequest {
  /** 目标租户 ID 列表 */
  target_tenant_ids: string[];
  /** 是否更新目标 default agent 中已有市场分发的同名 MCP；同名自建 MCP 会跳过 */
  overwrite: boolean;
}

/** MCP 分发结果 */
export interface MCPDistributeResult {
  /** 租户 ID */
  tenant_id: string;
  /** 是否成功 */
  success: boolean;
  /** 是否初始化了目标租户 */
  bootstrapped?: boolean;
  /** 已更新的 default agent 客户端 */
  default_agent_updated?: string[];
  /** 是否因目标租户已有同名自建 MCP 而跳过 */
  skipped?: boolean;
  /** 错误信息 */
  error?: string;
}

/** MCP 分发响应 */
export interface MCPDistributeResponse {
  /** 异步任务 ID */
  task_id: string;
  /** 异步任务状态 */
  status: string;
  /** 是否复用已有任务 */
  reused?: boolean;
  /** 来源条目标识 */
  source_agent_id?: string;
  /** 分发结果列表 */
  results?: MCPDistributeResult[];
}

/** MCP 市场元数据更新请求 */
export interface UpdateMarketMCPMetadataRequest {
  /** 中文名称 */
  chinese_name?: string;
  /** 描述 */
  description?: string;
  /** 使用指引 */
  guidance?: string;
  /** 可见机构 */
  bbk_ids: string[];
}

/** 分发记录 */
export interface DistributionRecord {
  /** 目标用户 ID */
  target_user_id: string;
  /** 目标用户名称 */
  target_user_name: string;
  /** 目标用户所属机构 ID */
  target_bbk_id: string;
  /** 分发时间 */
  distributed_at: string | null;
}

/** 撤回结果项 */
export interface RecallResultItem {
  /** 用户 ID */
  user_id: string;
  /** 是否成功 */
  success: boolean;
  /** 失败原因 */
  reason: string | null;
}

/** 撤回响应 */
export interface RecallResponse {
  /** 撤回成功数量 */
  recalled_count: number;
  /** 撤回失败数量 */
  failed_count: number;
  /** 撤回结果列表 */
  results: RecallResultItem[];
  /** 条目 ID */
  item_id: string;
}
