/**
 * ============================================================
 * iframe postMessage 通信类型定义
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
 * 验证请求消息
 * 父窗口查询子窗口的当前状态
 */
export interface IframeVerifyRequest {
  type: "VERIFY_REQUEST";
}

/**
 * 短时效下载 URL 请求消息（父 → 子）
 *
 * 方案2：父页面仅触发下载动作，无需拿到整份 HTML。
 * 父页面点击下载后发送该请求，子页面 ReportView 生成 blob URL
 * 并以 REPORT_URL 消息回传。
 */
export interface IframeReportUrlRequest {
  type: "REPORT_URL_REQUEST";
  /** 请求发起时间，子页面可用于判重/日志 */
  timestamp: number;
}

/**
 * ReportView 侧下载请求消息（父 → 子）
 *
 * 方案3：父页面仅触发下载动作，由 ReportView 自行执行下载。
 * 父页面点击下载后发送该请求，子页面复用 handleDownload 下载，
 * 不向父页面外发任何报告内容。
 */
export interface IframeReportDownloadRequest {
  type: "REPORT_DOWNLOAD_REQUEST";
  /** 请求发起时间，子页面可用于判重/日志 */
  timestamp: number;
}

/**
 * 验证响应消息
 * 子窗口响应父窗口的状态查询
 */
export interface IframeVerifyResponse {
  type: "VERIFY_RESPONSE";
  context: IframeContext;
}

/**
 * 报告原始 HTML 透传消息（子 → 父）
 *
 * 触发场景：URL 查询参数携带报告下载标识（如 needReportHtml=Y）时，
 * 在 renderedHtmlContent 生成完成后，将整份渲染后的 HTML 透传给父页面，
 * 供父页面自行执行下载或二次处理。
 *
 * 说明：透传由 sendReportMessageToParent 统一发送，仅做消息类型限定，
 * 目标源复用 parentOrigin；透传时机/对象由父页面配合管控。
 */
export interface IframeReportHtmlMessage {
  type: "REPORT_HTML";
  data: {
    /** 渲染后的完整报告 HTML 内容 */
    html: string;
    /** 建议下载文件名（含 .html 后缀） */
    fileName: string;
    /** 报告相关唯一标识（当前解析的 resultId，可为空） */
    resultId?: string | null;
    /** 模板 ID（当前解析的 templateId，可为空） */
    templateId?: string | null;
    /** 透传时间戳，便于父页面判重与日志 */
    timestamp: number;
  };
}

/**
 * 报告短时效下载 URL 透传消息（子 → 父）
 *
 * 触发场景：父页面仅需"触发下载动作"，不需要拿到整份 HTML 全文。
 * ReportView 负责生成临时下载 URL（blob URL），仅将 URL 透传给父页面，
 * 父页面通过 <a href download> 或 window.open 发起下载，报告内容不随 message 外发。
 */
export interface IframeReportUrlMessage {
  type: "REPORT_URL";
  data: {
    /** 短时效下载 URL（blob URL，父页面需在有效期内下载） */
    url: string;
    /** 建议下载文件名（含 .html 后缀） */
    fileName: string;
    /** 报告相关唯一标识（当前解析的 resultId，可为空） */
    resultId?: string | null;
    /** 透传时间戳 */
    timestamp: number;
  };
}

/**
 * 报告内容加载完成通知消息（子 → 父）
 *
 * 触发场景：renderedHtmlContent 通过 srcDoc 注入的 iframe 加载完成
 * （iframe onLoad 触发）后，ReportView 主动向父页面发送 REPORT_READY，
 * 告知父页面报告内容已在页面内渲染完成。
 *
 * 用途：父页面可据此感知"报告内容已就绪"，再决定执行下载、触发
 * REPORT_URL_REQUEST / REPORT_DOWNLOAD_REQUEST 或隐藏加载态。
 * 通知本身不携带报告内容，父页面应校验 event.origin 后按需处理。
 */
export interface IframeReportReadyMessage {
  type: "REPORT_READY";
  data: {
    /** 报告相关唯一标识（当前解析的 resultId，可为空） */
    resultId?: string | null;
    /** 模板 ID（当前解析的 templateId，可为空） */
    templateId?: string | null;
    /** 通知时间戳，便于父页面判重与日志 */
    timestamp: number;
  };
}

/**
 * 入站消息类型（父 → 子）
 *
 * 说明：REPORT_URL_REQUEST / REPORT_DOWNLOAD_REQUEST 为方案2/方案3的
 * 父页面下载触发请求，子页面在 frameMessage 监听器中处理。
 */
export type IframeIncomingMessage =
  | IframeUserDataMessage
  | IframeHeartbeatMessage
  | IframeReadyRequest
  | IframeVerifyRequest
  | IframeReportUrlRequest
  | IframeReportDownloadRequest;

/**
 * 出站消息类型（子 → 父）
 *
 * 说明：报告相关消息（REPORT_HTML / REPORT_URL / REPORT_READY）经
 * sendReportMessageToParent 统一发送，目标源复用 parentOrigin，仅做消息
 * 类型限定；透传时机/对象由父页面配合管控，避免向非预期父页面外呼敏感
 * 报告内容。
 */
export type IframeOutgoingMessage =
  | IframeReadyResponse
  | IframeVerifyResponse
  | IframeReportHtmlMessage
  | IframeReportUrlMessage
  | IframeReportReadyMessage;

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
  /** 页面来源：是否已由 URL 参数（最高优先级）锁定 */
  pageSourceFromUrl: boolean;
  /** 平台来源：是否已由 URL 参数（最高优先级）锁定 */
  platformSourceFromUrl: boolean;
}
