/**
 * ============================================================
 * 客户信息查询 API
 * Author: Kun He
 * Date: 2026-04-07
 * ============================================================
 *
 * 用于查询真实客户信息，在内嵌模式下调用
 * 当 iframe 内嵌且 URL 参数 origin === "Y" 时触发
 *
 * 相关文件：
 * - utils/iframeMessage.ts: 调用入口
 * - stores/iframeStore.ts: 用户数据存储
 * ============================================================
 */

import type { AuthHeaderItem } from "../../types/iframe";

// ==================== 客户信息查询接口 ====================

/**
 * 客户信息查询请求参数
 */
export interface CustomerInfoRequest {
  /** 用户 ID（sapId） */
  userId: string;
  /** 空间标识 */
  space?: string | null;
  /** 来源标识 */
  source?: string | null;
}

/**
 * 客户信息数据
 */
export interface CustomerInfoData {
  /** 用户 ID */
  userId: string;
  /** 用户名称 */
  clawName?: string;
  /** 空间标识 */
  space?: string;
  /** 来源标识 */
  source?: string;
  /** 是否隐藏菜单 */
  hideMenu?: boolean;
  /** 是否为超级管理员 */
  isSuperManager?: boolean;
  /** 自定义 headers */
  auth?: AuthHeaderItem[];
}

/**
 * 客户信息查询响应
 */
export interface CustomerInfoResponse {
  /** 返回码，SUC0000 表示成功 */
  returnCode: string;
  /** 返回消息 */
  returnMsg?: string;
  /** 用户信息是否变更 */
  userChange: boolean;
  /** 用户信息数据（当 userChange 为 true 时使用此数据覆盖） */
  data?: CustomerInfoData;
}

// ==================== 用户初始化接口 (Kun He) ====================

/**
 * 用户初始化请求参数
 */
export interface UserInitRequest {
  /** 用户 ID */
  userId: string;
  /** 空间标识 */
  space?: string | null;
  /** 来源标识 */
  source?: string | null;
}

/**
 * 用户初始化响应
 */
export interface UserInitResponse {
  /** 是否成功 */
  success: boolean;
  /** 返回消息 */
  message?: string;
  /** 初始化数据（存储到 localStorage） */
  data?: Record<string, unknown>;
}

/**
 * 检查用户是否已初始化
 * @param userId - 用户 ID
 * @returns 是否已初始化
 */
export function isUserInitialized(userId: string): boolean {
  const key = `swe-${userId}`;
  return localStorage.getItem(key) !== null;
}

/**
 * 设置用户已初始化标记
 * @param userId - 用户 ID
 * @param data - 初始化数据
 */
export function setUserInitialized(
  userId: string,
  data?: Record<string, unknown>,
): void {
  const key = `swe-${userId}`;
  localStorage.setItem(key, JSON.stringify(data ?? { initialized: true }));
}

/**
 * 用户初始化 API
 *
 * @param request - 请求参数
 * @returns 初始化响应
 */
export async function fetchUserInit(
  request: UserInitRequest,
): Promise<UserInitResponse | null> {
  try {
    // TODO: 替换为真实的 API 地址
    const apiUrl = "/api/user/init";

    const response = await fetch(apiUrl, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
      },
      body: JSON.stringify(request),
    });

    if (!response.ok) {
      console.error("[UserInit] API request failed:", response.status);
      return null;
    }

    const result: UserInitResponse = await response.json();
    return result;
  } catch (error) {
    console.error("[UserInit] API request error:", error);
    return null;
  }
}

/**
 * 查询客户信息 API
 *
 * @param request - 请求参数
 * @returns 客户信息响应
 */
export async function fetchCustomerInfo(
  request: CustomerInfoRequest,
): Promise<CustomerInfoResponse | null> {
  try {
    // TODO: 替换为真实的 API 地址
    const apiUrl = "/api/customer/info";

    const response = await fetch(apiUrl, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
      },
      body: JSON.stringify(request),
    });

    if (!response.ok) {
      console.error("[CustomerInfo] API request failed:", response.status);
      return null;
    }

    const result: CustomerInfoResponse = await response.json();
    return result;
  } catch (error) {
    console.error("[CustomerInfo] API request error:", error);
    return null;
  }
}

/**
 * ============================================================
 * Mock API - 模拟接口（开发测试用）
 * ============================================================
 *
 * 使用方式：
 * 1. 开发时使用 mockCustomerInfo 替代 fetchCustomerInfo
 * 2. 可以修改 delay 和 mockResponse 来模拟不同场景
 */

/** Mock 延迟时间（毫秒） */
const MOCK_DELAY = 500;

/** Mock 响应数据 - 无变更 */
const MOCK_RESPONSE_NO_CHANGE: CustomerInfoResponse = {
  returnCode: "SUC0000",
  userChange: false,
};

/** Mock 响应数据 - 有变更 */
const MOCK_RESPONSE_WITH_CHANGE: CustomerInfoResponse = {
  returnCode: "SUC0000",
  userChange: true,
  data: {
    userId: "new-user-id",
    clawName: "新用户名",
    space: "new-space",
    source: "new-source",
    hideMenu: false,
    isSuperManager: true,
  },
};

/**
 * Mock 客户信息查询 API
 *
 * @param request - 请求参数
 * @param shouldChange - 是否模拟用户变更（默认 false）
 * @returns 客户信息响应
 */
export async function mockFetchCustomerInfo(
  request: CustomerInfoRequest,
  shouldChange = false,
): Promise<CustomerInfoResponse> {
  // 模拟网络延迟
  await new Promise((resolve) => setTimeout(resolve, MOCK_DELAY));

  console.info("[CustomerInfo] Mock API called with:", request);

  // 根据参数返回不同的 mock 数据
  if (shouldChange) {
    console.info("[CustomerInfo] Mock response: user changed");
    return {
      ...MOCK_RESPONSE_WITH_CHANGE,
      data: {
        ...MOCK_RESPONSE_WITH_CHANGE.data,
        userId: request.userId, // 保持原 userId 或使用新值
      },
    };
  }

  console.info("[CustomerInfo] Mock response: no change");
  return MOCK_RESPONSE_NO_CHANGE;
}

// ==================== Mock 用户初始化接口 (Kun He) ====================

/**
 * Mock 用户初始化 API
 *
 * @param request - 请求参数
 * @returns 初始化响应
 */
export async function mockFetchUserInit(
  request: UserInitRequest,
): Promise<UserInitResponse> {
  // 模拟网络延迟
  await new Promise((resolve) => setTimeout(resolve, MOCK_DELAY));

  console.info("[UserInit] Mock API called with:", request);

  // 返回成功的 mock 响应
  return {
    success: true,
    message: "User initialized successfully",
    data: {
      userId: request.userId,
      initializedAt: new Date().toISOString(),
    },
  };
}

// ==================== 用户列表接口 (Kun He) ====================

/**
 * 用户信息
 */
export interface UserInfo {
  /** 用户 ID */
  userId: string;
  /** 用户名称 */
  clawName?: string;
  /** 空间标识 */
  space?: string;
}

/**
 * 用户列表响应
 */
export interface UserListResponse {
  /** 是否成功 */
  success: boolean;
  /** 返回消息 */
  message?: string;
  /** 用户列表 */
  data?: UserInfo[];
}

/**
 * 获取用户列表 API
 *
 * @returns 用户列表响应
 */
export async function fetchUserList(): Promise<UserListResponse | null> {
  try {
    // TODO: 替换为真实的 API 地址
    const apiUrl = "/api/user/list";

    const response = await fetch(apiUrl, {
      method: "GET",
      headers: {
        "Content-Type": "application/json",
      },
    });

    if (!response.ok) {
      console.error("[UserList] API request failed:", response.status);
      return null;
    }

    const result: UserListResponse = await response.json();
    return result;
  } catch (error) {
    console.error("[UserList] API request error:", error);
    return null;
  }
}

/**
 * Mock 用户列表数据
 */
const MOCK_USER_LIST: UserInfo[] = [
  { userId: "80000001", clawName: "张三", space: "default" },
  { userId: "80000002", clawName: "李四", space: "default" },
  { userId: "80000003", clawName: "王五", space: "default" },
  { userId: "80000004", clawName: "赵六", space: "default" },
  { userId: "80000005", clawName: "钱七", space: "default" },
];

/**
 * Mock 获取用户列表 API
 *
 * @returns 用户列表响应
 */
export async function mockFetchUserList(): Promise<UserListResponse> {
  // 模拟网络延迟
  await new Promise((resolve) => setTimeout(resolve, MOCK_DELAY));

  console.info("[UserList] Mock API called");

  return {
    success: true,
    message: "User list fetched successfully",
    data: MOCK_USER_LIST,
  };
}