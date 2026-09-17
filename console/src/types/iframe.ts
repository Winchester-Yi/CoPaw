/**
 * ============================================================
 * iframe postMessage 通信类型定义
 * Author: Kun He
 * Date: 2026-04-07
 * ============================================================
 *
 * 用于子应用与父级 iframe 应用之间的消息通信
 *
 * 消息类型：
 * - USER_DATA: 父窗口发送的用户数据（初始化消息）
 * - HEARTBEAT: 心跳消息（检测连接状态）
 * - READY_REQUEST/READY_RESPONSE: 就绪查询与响应
 *
 * 相关文件：
 * - stores/iframeStore.ts: 状态存储
 * - utils/iframeMessage.ts: 消息处理逻辑
 * - api/authHeaders.ts: headers 构建
 * - layouts/MainLayout/index.tsx: Sidebar 显示控制
 * ============================================================
 */

/**
 * 自定义 header 项
 * 用于父应用传递自定义 headers
 */
export interface AuthHeaderItem {
  headerName: string;
  headerValue: string;
}

/**
 * 父窗口发送给子窗口的初始化参数
 *
 * 参数说明：
 * - type: 消息类型
 * - data: 数据对象，包含以下字段：
 *   - sapId: SAP ID，存储为 userId，并作为 X-User-Id header
 *   - clawName: Claw 名称
 *   - space: 空间标识
 *   - source: 来源标识
 *   - hideMenu: 是否隐藏菜单（支持 boolean 或字符串 "true"/"false"）
 *   - isSuperManager: 是否为超级管理员
 *   - manager: 是否为普通管理员
 *   - skipPreviewTracking: 是否跳过 HTML preview 埋点（支持 boolean 或字符串 "true"/"false"）
 *   - auth: 自定义 headers 数组
 *   - pageSource: "",  【必传】打开页面：商机中心-SJR、客户洞察-KHA、高级搜索-GJS、电访-GL6
 *   - platformSource: "",  【必传】打开平台：W+
 */
export interface IframeUserDataMessage {
  type: "USER_DATA";
  data: {
    /** SAP ID，会作为 userId 存储 */
    sapId?: string;
    /** Claw 名称 */
    clawName?: string;
    /** 空间标识 */
    space?: string;
    /** 来源标识 */
    source?: string;
    /** 是否隐藏菜单（支持 boolean 或字符串 "true"/"false"） */
    hideMenu?: boolean | string;
    /** 是否为超级管理员（支持 boolean 或字符串 "true"/"false"） */
    isSuperManager?: boolean | string;
    /** 是否为普通管理员（支持 boolean 或字符串 "true"/"false"） */
    manager?: boolean | string;
    /** 是否跳过 HTML preview 埋点（支持 boolean 或字符串 "true"/"false"） */
    skipPreviewTracking?: boolean | string;
    /** 自定义 headers 数组，每项包含 headerName 和 headerValue */
    auth?: AuthHeaderItem[];
    /** 分行ID */
    bbkId?: string;
    /** 其他任意参数 */
    [key: string]: unknown;
    bbkOrgId?: string;
    userId?: string;
    /** 是否隐藏聊天菜单（支持 boolean 或字符串 "true"/"false"） */
    hideChat?: boolean | string;
    /** 打开页面 */
    pageSource?: string;
    /** 打开平台 */
    platformSource?: string;
  };
}

/**
 * 心跳消息
 * 用于检测父窗口连接状态
 */
export interface IframeHeartbeatMessage {
  type: "HEARTBEAT";
  timestamp: number;
}

/**
 * 就绪查询消息
 * 父窗口查询子窗口是否准备就绪
 */
export interface IframeReadyRequest {
  type: "READY_REQUEST";
}

/**
 * 就绪响应消息
 * 子窗口响应父窗口的就绪查询
 */
export interface IframeReadyResponse {
  type: "READY_RESPONSE";
  initialized: boolean;
}

/**
 * 入站消息类型（父 → 子）
 */
export type IframeIncomingMessage =
  | IframeUserDataMessage
  | IframeHeartbeatMessage
  | IframeReadyRequest;

/**
 * 出站消息类型（子 → 父）
 */
export type IframeOutgoingMessage = IframeReadyResponse;

/**
 * 存储从父窗口接收的参数
 *
 * 存储字段：
 * - userId: 用户 ID（来自 sapId，在 headers 中默认为 "default"）
 * - clawName, space, source: 上下文信息
 * - hideMenu: 控制 Sidebar 显示
 * - isSuperManager: 权限标识
 * - manager: 普通管理员标识
 * - authHeaders: 自定义 headers（包含 sapId 转换的 X-User-Id）
 * - parentOrigin: 父窗口来源（用于安全验证）
 */
export interface IframeContext {
  /** 是否已初始化 */
  initialized: boolean;
  /** 用户 ID（来自 sapId） */
  userId: string | null;
  /** 用户名称（从后端接口查询） */
  userName: string | null;
  /** Claw 名称 */
  clawName: string | null;
  /** 空间标识 */
  space: string | null;
  /** 来源标识 */
  source: string | null;
  /** 是否隐藏菜单 */
  hideMenu: boolean;
  /** 当前页面是否通过 origin=Y 入口访问 */
  isOriginY: boolean;
  /** 是否为超级管理员 */
  isSuperManager: boolean;
  /** 是否为普通管理员 */
  manager: boolean;
  /** 是否跳过 HTML preview 埋点 */
  skipPreviewTracking: boolean;
  /** 自定义 headers 数组 */
  authHeaders: AuthHeaderItem[];
  /** 来源 origin */
  parentOrigin: string | null;
  /** 接收消息的时间戳 */
  receivedAt: number | null;
  /** 系统标识 */
  sysId: string | null;
  /** 认证令牌 */
  token: string | null;
  /** 业务板块 */
  bbk: string | null;
  /** 组织编码 */
  orgCode: string | null;
  /** 支行 ID */
  subBranchId: string | null;
  /** 组织层级 */
  orgLvl: string | null;
  /** 职位 ID */
  positionId: string | null;
  /** 用户是否变更 */
  userChange: boolean;
  /** ==================== URL 导航参数 (Kun He, 2026-04-15) ==================== **/
  /** 会话 ID，用于直接导航到 /chat/:sessionId */
  sessionId: string | null;
  /** 任务 ID，用于查找 task.chat_id 后导航 */
  taskId: string | null;
  /** 是否隐藏聊天菜单 */
  hideChat: boolean;
  /** 打开页面 */
  pageSource: string | null;
  /** 打开平台 */
  platformSource: string | null;
}
