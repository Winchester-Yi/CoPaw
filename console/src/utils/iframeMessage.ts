import type {
  IframeUserDataMessage,
  IframeIncomingMessage,
  IframeOutgoingMessage,
  AuthHeaderItem,
} from "../types/iframe";
import { useIframeStore, getIframeContext } from "../stores/iframeStore";
import {
  fetchCustomerInfo,
  fetchUserInit,
} from "../api/modules/customerInfo";
import {
  fetchUserInfo,
  extractUserInfo,
} from "../api/modules/userInfo";
import {
  ensureValidToken,
  isExternalTokenEnabled,
} from "../api/externalToken";
import { getWPlusCookie } from "./cookie-utils";
import { authApi } from "../api/modules/auth";
import { buildAuthHeaders as buildCookieHeaders } from "../api/authHeaders";
// import mmj from 'xxxx'

/**
 * 允许的来源白名单
 */
const ALLOWED_ORIGINS: string[] = [
  // 开发环境
  // "http://localhost:5173",
  // "http://127.0.0.1:5173",
  // 生产环境 - 从环境变量读取
  // ...(typeof import.meta !== "undefined" &&
  // import.meta.env?.VITE_ALLOWED_PARENT_ORIGINS
  //   ? import.meta.env.VITE_ALLOWED_PARENT_ORIGINS.split(",").filter(Boolean)
  //   : []),
];

/** 是否已注册监听器 */
let isListenerRegistered = false;


/** Cookie刷新定时器 */
let cookieRefreshTimer: ReturnType<typeof setInterval> | null = null;


/** Cookie刷新间隔（30分钟） */
const COOKIE_REFRESH_INTERVAL = 30 * 60 * 1000;


/** 清理函数 */
let cleanupFn: (() => void) | null = null;

/** 当前正在查询用户信息的 userId，用于合并初始化和 iframe 消息触发的并发请求 */
let pendingUserInfoUserId: string | null = null;

/** 当前正在执行的用户信息查询任务 */
let pendingUserInfoRequest: Promise<boolean> | null = null;

/** 正在执行初始化的用户，避免同一用户在接口返回前被重复初始化 */
const pendingUserInitUserIds = new Set<string>();

/**
 * 将值转换为布尔值，用于处理父窗口可能传递的字符串 "true"/"false"
 * @param value - 值
 * @returns 布尔值
 */
function toBoolean(value: boolean | string | undefined): boolean {
  if (typeof value === "boolean") return value;
  if (typeof value === "string") return value.toLowerCase() === "true";
  return false;
}

/**
 * 验证消息来源是否可信
 * @param origin - 消息来源 origin
 * @returns 是否可信
 */
function isValidOrigin(origin: string): boolean {
  // 如果在 iframe 中运行
  if (window.self !== window.top) {
    // 严格模式：检查白名单，白名单为空则允许所有
    return ALLOWED_ORIGINS.length === 0 || ALLOWED_ORIGINS.includes(origin);
  }
  // 不在 iframe 中，不需要验证
  return true;
}

/**
 * 验证消息格式
 * @param data - 消息数据
 * @returns 是否为有效的 iframe 消息
 */
function validateMessage(data: unknown): data is IframeIncomingMessage {
  if (!data || typeof data !== "object") return false;
  const msg = data as Record<string, unknown>;

  // 必须有 type 字段且为字符串
  if (typeof msg.type !== "string") return false;

  // 根据类型验证
  switch (msg.type) {
    case "USER_DATA":
      // 必须有 data 字段且为对象
      return msg.data !== undefined && typeof msg.data === "object";
    case "HEARTBEAT":
      return typeof msg.timestamp === "number";
    case "READY_REQUEST":
      return true;
    default:
      // 未知类型，忽略但不报错
      return false;
  }
}

/**
 * 构建 iframe 上下文中的认证 headers
 * 将 sapId 作为 X-User-Id，并合并父窗口传递的 auth 数组
 */
function buildAuthHeaders(
  message: IframeUserDataMessage,
): AuthHeaderItem[] {
  const authHeaders = [...(message.data.auth ?? [])];
  if (message.data.sapId) {
    authHeaders.push({
      headerName: "X-User-Id",
      headerValue: message.data.sapId,
    });
  }
  return authHeaders;
}

/**
 * 调用用户初始化接口并保存到 localStorage
 */
function initializeUser(userId: string): void {
  if (pendingUserInitUserIds.has(userId)) {
    return;
  }

  pendingUserInitUserIds.add(userId);

  const store = useIframeStore.getState();
  const params = {
    filename: "PROFILE.md",
    text: `\n### 用户身份信息\n分行号：${store.bbk}\n网点机构编号：${store.orgCode}\n岗位编号：${store.positionId}\n客户经理ID：${userId}`,
  };

  void fetchUserInit(params)
    .then((initResponse) => {
      if (!initResponse?.appended) {
        console.warn("[IframeMessage] User init failed");
      }
    })
    .catch((error) => {
      console.error("[IframeMessage] User init error:", error);
    })
    .finally(() => {
      pendingUserInitUserIds.delete(userId);
    });
}

/**
 * 处理 USER_DATA 消息
 * 父窗口发送的用户数据消息处理逻辑
 */
async function handleUserDataMessage(
  message: IframeUserDataMessage,
  origin: string,
): Promise<void> {
  const store = useIframeStore.getState();
  const authHeaders = buildAuthHeaders(message);
  const nextUserId = message.data.sapId || message.data.userId || null;

  store.setContext({
    userId: nextUserId,
    ...(store.userId !== nextUserId ? { userName: null } : {}),
    clawName: message.data.clawName ?? null,
    space: message.data.space ?? null,
    source: message.data.source ?? null,
    hideMenu: toBoolean(message.data.hideMenu),
    isSuperManager: toBoolean(message.data.isSuperManager),
    manager: toBoolean(message.data.manager),
    skipPreviewTracking: toBoolean(message.data.skipPreviewTracking),
    authHeaders,
    parentOrigin: origin,
    bbk: message.data.bbkId || message.data.bbkOrgId || null,
    hideChat: toBoolean(message.data.hideChat),
  });

  // 等待 userName 获取完成后再标记初始化完成
  // 确保 X-User-Name header 在后续请求中可用
  await fetchAndSetUserName();

  store.markInitialized();
  // sendMessageToParent({ type: "READY_RESPONSE", initialized: true });
  // 只在W发起
  // const headers = buildCookieHeaders();
  // const cookieValue = headers["x-header-cookie"] || document.cookie;
  // void authApi.sendCronAuth(cookieValue);

  // initMmj(message?.data?.sapId ?? null)
}

/**
 * 处理心跳消息
 * @param timestamp - 心跳时间戳
 */
function handleHeartbeatMessage(timestamp: number): void {
  console.debug("[IframeMessage] Heartbeat received:", timestamp);
  // 可用于检测父窗口连接状态
}

/**
 * 处理就绪查询消息
 */
function handleReadyRequest(): void {
  // 父容器不需要知道初始化状态，已注释
  // const context = getIframeContext();
  // sendMessageToParent({
  //   type: "READY_RESPONSE",
  //   initialized: context.initialized,
  // });
}

/**
 * 消息处理中心
 * @param event - MessageEvent
 */
function handleMessage(event: MessageEvent): void {
  // 安全检查：验证来源
  if (!isValidOrigin(event.origin)) {
    return;
  }

  // 验证消息格式
  if (!validateMessage(event.data)) {
    return;
  }
  const message = event.data as IframeIncomingMessage;

  switch (message.type) {
    case "USER_DATA":
      // 异步处理，等待 userName 获取完成后再标记 initialized
      void handleUserDataMessage(message, event.origin);
      break;
    case "HEARTBEAT":
      handleHeartbeatMessage(message.timestamp);
      break;
    case "READY_REQUEST":
      handleReadyRequest();
      break;
  }
}

/**
 * 向父窗口发送消息
 * @param message - 出站消息
 */
export function sendMessageToParent(message: IframeOutgoingMessage): void {
  // 检查是否在 iframe 中
  if (window.parent === window.self) {
    return;
  }

  const context = getIframeContext();
  const targetOrigin = context.parentOrigin && context.parentOrigin !== "null" ? context.parentOrigin : "*";

  window.parent.postMessage(message, targetOrigin);
}

/**
 * 初始化 iframe 消息监听器
 * 应在 main.tsx 中尽早调用，确保不遗漏任何消息
 *
 * 初始化流程：
 * 1. 检查是否已在 iframe 中运行（非 iframe 环境跳过）
 * 2. 注册 message 事件监听器
 * 3. 发送 READY_RESPONSE (initialized: false) 通知父窗口
 * 4. 等待父窗口发送 USER_DATA 消息
 */
export function initIframeMessageListener(): void {
  // 防止重复注册
  if (isListenerRegistered) {
    return;
  }

  // 检查是否在 iframe 中
  if (window.self === window.top) {
    return;
  }

  // 注册消息监听器
  window.addEventListener("message", handleMessage);
  isListenerRegistered = true;

  // 注册清理函数
  cleanupFn = () => {
    window.removeEventListener("message", handleMessage);
    isListenerRegistered = false;
    stopCookieRefreshTimer();
    cleanupFn = null;
  };

  // 页面卸载时自动清理
  window.addEventListener("beforeunload", cleanupFn);
}

/**
 * 清理独立访问时残留的 iframe 上下文
 *
 * 同一个浏览器标签页可能先以 iframe 方式打开，再以普通页面方式访问。
 * 这时 sessionStorage 中的 iframe 身份会污染后续请求头，因此独立访问时主动清理。
 */
export function resetIframeContextForStandalone(): void {
  const urlParams = new URLSearchParams(window.location.search);
  if (isInIframe() || urlParams.get("origin") === "Y") {
    return;
  }

  const store = useIframeStore.getState();
  if (
    store.userId ||
    store.userName ||
    store.source ||
    store.bbk ||
    store.authHeaders.length > 0
  ) {
    store.clearContext();
  }
}

/**
 * 处理 URL 参数 origin=Y 的场景
 * 当父应用通过 URL 传递参数时，从 cookie 读取用户信息并初始化
 */
export async function handleUrlOriginParam(): Promise<void> {
  const urlParams = new URLSearchParams(window.location.search);
  const isOriginY = urlParams.get("origin") === "Y";
  const store = useIframeStore.getState();
  store.setOriginY(isOriginY);

  if (!isOriginY) {
    return;
  }

  // 读取 sessionId 和 taskId 参数，用于自动跳转到聊天页面
  const sessionIdParam = urlParams.get("sessionId");
  const taskIdParam = urlParams.get("taskId");
  // 从 cookie 读取用户信息
  const userId = getWPlusCookie("userid");
  const sysId = getWPlusCookie("sysid");
  const vbbk = getWPlusCookie("vbbk");
  const vorgcode = getWPlusCookie("vorgcode");
  const subBranchId = getWPlusCookie("subBranchId");
  const vorglvl = getWPlusCookie("vorglvl");
  const positionId = getWPlusCookie("positionID");

  if (!userId) {
    return;
  }

  // 设置初始上下文，hideMenu=true 隐藏 MainLayout 侧边栏
  store.setContext({
    userId,
    ...(store.userId !== userId ? { userName: null } : {}),
    sysId: sysId ?? null,
    bbk: vbbk ?? null,
    orgCode: vorgcode ?? null,
    subBranchId: subBranchId ?? null,
    orgLvl: vorglvl ?? null,
    positionId: positionId ?? null,
    hideMenu: true, // URL origin=Y 时隐藏 MainLayout 侧边栏
    source: "RMASSIST",
  });

  // 设置导航参数，Chat 页面会在首次加载时检查并执行导航
  if (sessionIdParam || taskIdParam) {
    store.setNavigationParams(sessionIdParam, taskIdParam);
  }

  // 异步调用客户信息接口和用户初始化
  await initFromUrlParams(userId);
}

/**
 * 从 URL 参数初始化时的异步处理
 * 调用客户信息接口和用户初始化
 */
async function initFromUrlParams(userId: string): Promise<void> {
  // 首次进入时客户信息接口可能较慢或被嵌入环境阻塞，用户初始化不能依赖它完成。
  initializeUser(userId);

  // 调用客户信息接口（使用 cookie 中的参数）
  await fetchAndApplyCustomerInfoFromCookie(userId);

  // 客户信息接口可能修正 userId，修正后的用户仍需要初始化。
  const latestStore = useIframeStore.getState();
  const currentUserId = latestStore.userId;
  if (currentUserId && currentUserId !== userId) {
    initializeUser(currentUserId);
  }

  latestStore.markInitialized();

  const headers = buildCookieHeaders();
  const cookieValue = headers["x-header-cookie"] || document.cookie;
  void authApi.sendCronAuth(cookieValue);
  // initMmj(currentUserId);

  // 启动Cookie定时刷新机制
  startCookieRefreshTimer();
}

// async function initMmj()
// 省略实现













// 省略实现结束

/**
 * 从 cookie 参数调用客户信息接口
 */
async function fetchAndApplyCustomerInfoFromCookie(userId: string): Promise<void> {
  try {
    const sysId = getWPlusCookie("sysid") ?? "";
    const vbbk = getWPlusCookie("vbbk") ?? "";
    const vorgcode = getWPlusCookie("vorgcode") ?? "";
    const subBranchId = getWPlusCookie("subBranchId") ?? "";
    const vorglvl = getWPlusCookie("vorglvl") ?? "";
    const positionId = getWPlusCookie("positionID") ?? "";

    const targetUserData = {
      inputParams: {
        userId,
        sysId,
        bbk: vbbk,
        orgCode: vorgcode,
        orgLvl: vorglvl,
        positionId,
      },
    };

    const response = await fetchCustomerInfo(targetUserData);
    const store = useIframeStore.getState();

    if (response?.returnCode === "SUC0000") {
      const result = response.body.output.result;
      if (result.userChange) {
        // 接口返回了新的cookie，使用接口返回的值
        store.setContext({
          userId: result.userId ?? userId,
          sysId: result.sysId ?? sysId,
          token: result.token ?? null,
          bbk: result.bbk ?? vbbk,
          orgCode: result.orgCode ?? vorgcode,
          subBranchId,
          orgLvl: result.orgLvl ?? vorglvl,
          positionId: result.positionId ?? positionId,
          userChange: result.userChange ?? false,
        });
      } else {
        // 接口未返回新cookie，刷新document.cookie的值到store
        store.setContext({
          token: getWPlusCookie("token"),
          userChange: false,
        });
      }
    }
  } catch (error) {
    console.error("[IframeMessage] Customer info fetch error:", error);
  }
}

/**
 * 启动Cookie定时刷新机制
 * 每隔30分钟调用一次 fetchCustomerInfo 来更新cookie
 */
function startCookieRefreshTimer(): void {
  if (cookieRefreshTimer) {
    return; // 定时器已启动
  }

  cookieRefreshTimer = setInterval(async () => {
    const store = useIframeStore.getState();
    const userId = store.userId;

    if (!userId) {
      return;
    }

    try {
      // 调用 fetchCustomerInfo 更新cookie
      await fetchAndApplyCustomerInfoFromCookie(userId);

      // 更新 cron-auth
      const headers = buildCookieHeaders();
      const cookieValue = headers["x-header-cookie"] || document.cookie;
      void authApi.sendCronAuth(cookieValue);

      console.debug("[CookieRefresh] Timer triggered, cookie refreshed");
    } catch (error) {
      console.error("[CookieRefresh] Timer error:", error);
    }
  }, COOKIE_REFRESH_INTERVAL);

  console.info("[CookieRefresh] Timer started, interval:", COOKIE_REFRESH_INTERVAL, "ms");
}

/**
 * 停止Cookie定时刷新机制
 */
function stopCookieRefreshTimer(): void {
  if (cookieRefreshTimer) {
    clearInterval(cookieRefreshTimer);
    cookieRefreshTimer = null;
    console.info("[CookieRefresh] Timer stopped");
  }
}

/**
 * 手动清理监听器
 *
 * 通常不需要手动调用，页面卸载时会自动清理
 * 仅在特殊场景（如测试）中使用
 */
export function cleanupIframeMessageListener(): void {
  if (cleanupFn) {
    cleanupFn();
  }
  stopCookieRefreshTimer();
}

/**
 * 检查是否在 iframe 中运行
 * @returns 是否在 iframe 中
 */
export function isInIframe(): boolean {
  return window.self !== window.top;
}

/**
 * 检查 iframe 上下文是否已初始化
 * @returns 是否已初始化
 */
export function isIframeInitialized(): boolean {
  return getIframeContext().initialized;
}

/**
 * 获取允许的来源白名单
 * @returns 来源白名单数组
 */
export function getAllowedOrigins(): string[] {
  return [...ALLOWED_ORIGINS];
}

/**
 * 查询并设置用户名称和 bbk
 * 在获取 userId 和 token 后调用，将 userName 和 bbk 存入 store
 *
 * @returns 是否成功获取用户信息
 */
export async function fetchAndSetUserName(): Promise<boolean> {
  const store = useIframeStore.getState();
  const userId = store.userId;

  if (!userId) {
    return false;
  }

  if (pendingUserInfoRequest && pendingUserInfoUserId === userId) {
    return pendingUserInfoRequest;
  }

  pendingUserInfoUserId = userId;
  pendingUserInfoRequest = fetchAndApplyUserName(userId);

  try {
    return await pendingUserInfoRequest;
  } finally {
    if (pendingUserInfoRequest && pendingUserInfoUserId === userId) {
      pendingUserInfoRequest = null;
      pendingUserInfoUserId = null;
    }
  }
}

async function fetchAndApplyUserName(userId: string): Promise<boolean> {
  try {
    if (isExternalTokenEnabled()) {
      await ensureValidToken();
    }

    const userInfoData = await fetchUserInfo(userId);
    const { userName, bbk } = extractUserInfo(userInfoData);

    const store = useIframeStore.getState();
    if (store.userId !== userId) {
      return false;
    }

    // 更新 store，只更新有值的字段
    // 如果 store 中已存在 bbk，则不覆盖
    const updates: { userName?: string; bbk?: string } = {};
    if (userName) {
      updates.userName = userName;
    }
    if (bbk && !store.bbk) {
      updates.bbk = bbk;
    }

    if (Object.keys(updates).length > 0) {
      store.setContext(updates);
      return true;
    }
    return false;
  } catch (error) {
    console.error("[IframeMessage] fetchAndSetUserName error:", error);
    return false;
  }
}
