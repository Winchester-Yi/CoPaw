/**
 * 市场 MCP API（调用市场服务）
 */
import { request } from "../request";
import { mergeHeaders } from "../mergeHeaders";
import type {
  MarketMCPItem,
  MarketMCPDetail,
  MCPUploadRequest,
  MCPDistributeRequest,
  MCPDistributeResponse,
  MCPTestResult,
  UpdateMarketMCPMetadataRequest,
  DistributionRecord,
  RecallResponse,
} from "../types";
import type { UploadMCPResponse } from "../types/marketMcp";

export const marketMcpApi = {
  /**
   * 获取市场 MCP 列表
   */
  listMarketMCP: async (
    categoryId?: number,
    bbkIds?: string,
    sourceId?: string,
  ): Promise<MarketMCPItem[]> => {
    let url = "/market/mcp";
    const params = new URLSearchParams();
    if (categoryId !== undefined) {
      params.append("category_id", String(categoryId));
    }
    if (bbkIds !== undefined && bbkIds !== null) {
      params.append("bbk_ids", bbkIds);
    }
    if (params.toString()) {
      url += `?${params.toString()}`;
    }
    const opts = mergeHeaders(sourceId ? { "X-Source-Id": sourceId } : {});
    return request<MarketMCPItem[]>(url, opts);
  },

  /**
   * 获取市场 MCP 详情
   */
  getMarketMCPDetail: async (
    itemId: string,
    sourceId?: string,
  ): Promise<MarketMCPDetail | null> => {
    const opts = mergeHeaders(sourceId ? { "X-Source-Id": sourceId } : {});
    return request<MarketMCPDetail | null>(`/market/mcp/${itemId}`, opts);
  },

  /**
   * 上传 MCP 到市场（管理员）
   *
   * 支持两种上传方式（二选一）：
   * - 传 file：上传 .json 文件
   * - 传 raw_json：直接传 JSON 字符串（前端粘贴方式）
   */
  uploadMCP: async (data: MCPUploadRequest): Promise<UploadMCPResponse> => {
    const formData = new FormData();
    if (data.file) {
      formData.append("file", data.file);
    }
    if (data.raw_json) {
      formData.append("raw_json", data.raw_json);
    }
    formData.append("name", data.name);
    if (data.chinese_name) {
      formData.append("chinese_name", data.chinese_name);
    }
    if (data.description) {
      formData.append("description", data.description);
    }
    if (data.guidance) {
      formData.append("guidance", data.guidance);
    }
    if (data.bbk_ids && data.bbk_ids.length > 0) {
      formData.append("bbk_ids", JSON.stringify(data.bbk_ids));
    }
    const opts: RequestInit = {
      method: "POST",
      ...mergeHeaders({
        "X-Manager": "true",
      }),
      body: formData,
    };
    return request<UploadMCPResponse>("/market/mcp/upload", opts);
  },

  /**
   * 分发 MCP 到用户（管理员）
   */
  distributeMCP: async (
    itemId: string,
    data: MCPDistributeRequest,
  ): Promise<MCPDistributeResponse> => {
    const opts: RequestInit = {
      method: "POST",
      ...mergeHeaders({
        "Content-Type": "application/json",
        "X-Manager": "true",
      }),
      body: JSON.stringify(data),
    };
    return request<MCPDistributeResponse>(
      `/market/mcp/${itemId}/distribute`,
      opts,
    );
  },

  /**
   * 删除市场 MCP（管理员）
   */
  deleteMarketMCP: async (itemId: string): Promise<void> => {
    const opts: RequestInit = {
      method: "DELETE",
      ...mergeHeaders({
        "X-Manager": "true",
      }),
    };
    return request<void>(`/market/mcp/${itemId}`, opts);
  },

  /**
   * 测试市场 MCP 连接（管理员）
   */
  testMarketMCP: async (itemId: string): Promise<MCPTestResult> => {
    const opts: RequestInit = {
      method: "POST",
      ...mergeHeaders({
        "X-Manager": "true",
      }),
    };
    return request<MCPTestResult>(`/market/mcp/${itemId}/test`, opts);
  },

  /**
   * 更新市场 MCP 元数据（管理员）
   */
  updateMarketMCPMetadata: async (
    itemId: string,
    data: UpdateMarketMCPMetadataRequest,
  ): Promise<MarketMCPDetail> => {
    const opts: RequestInit = {
      method: "PUT",
      ...mergeHeaders({
        "Content-Type": "application/json",
        "X-Manager": "true",
      }),
      body: JSON.stringify(data),
    };
    return request<MarketMCPDetail>(`/market/mcp/${itemId}/metadata`, opts);
  },

  /**
   * 查询 MCP 分发记录（管理员）
   */
  getMCPDistributions: async (
    sourceId: string,
    itemId: string,
  ): Promise<DistributionRecord[]> => {
    const opts = mergeHeaders({
      "X-Source-Id": sourceId,
      "X-Manager": "true",
    });
    return request<DistributionRecord[]>(
      `/market/mcp/${itemId}/distributions`,
      opts,
    );
  },

  /**
   * 撤回已分发的 MCP（管理员）
   */
  recallMCP: async (
    sourceId: string,
    itemId: string,
    targetUserIds?: string[],
  ): Promise<RecallResponse> => {
    const opts: RequestInit = {
      method: "POST",
      ...mergeHeaders({
        "Content-Type": "application/json",
        "X-Source-Id": sourceId,
        "X-Manager": "true",
      }),
      body: JSON.stringify({ target_user_ids: targetUserIds }),
    };
    return request<RecallResponse>(`/market/mcp/${itemId}/recall`, opts);
  },
};
