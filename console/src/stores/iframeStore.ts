/**
 * ============================================================
 * iframe 上下文状态存储
 * Author: Kun He
 * Date: 2026-04-07
 * ============================================================
 *
 * 使用 Zustand 管理从父级 iframe 接收的参数
 * 支持持久化到 sessionStorage
 *
 * 存储字段：
 * - userId: 用户 ID（来自父窗口的 sapId 参数）
 * - userName: 用户名称（从后端接口查询）
 * - clawName: Claw 名称
 * - space: 空间标识
 * - source: 来源标识
 * - hideMenu: 是否隐藏菜单
 * - isSuperManager: 是否为超级管理员
 * - manager: 是否为普通管理员
 * - skipPreviewTracking: 是否跳过 HTML preview 埋点
 * - authHeaders: 自定义 headers 数组
 * - parentOrigin: 父窗口来源 origin
 * - subBranchId: 支行 ID
 *
 * 相关文件：
 * - types/iframe.ts: 类型定义
 * - utils/iframeMessage.ts: 消息处理逻辑
 * ============================================================
 */
import { create } from "zustand";
import { persist } from "zustand/middleware";
import type { IframeContext, AuthHeaderItem } from "../types/iframe";

interface IframeStore extends IframeContext {
  /**
   * 设置 iframe 上下文
   * @param context - 部分上下文参数
   */
  setContext: (
    context: Partial<Omit<IframeContext, "initialized" | "receivedAt">>,
  ) => void;

  /**
   * 标记初始化完成
   */
  markInitialized: () => void;

  /**
   * 清除所有上下文
   */
  clearContext: () => void;

  /** 记录当前页面是否通过 origin=Y 入口访问 */
  setOriginY: (isOriginY: boolean) => void;

  /**
   * 设置自定义 headers
   * @param authHeaders - 自定义 header 数组
   */
  setAuthHeaders: (authHeaders: AuthHeaderItem[]) => void;
  /**
   * ==================== URL 导航参数 (Kun He, 2026-04-15) ====================
   * 设置导航参数（sessionId 和 taskId）
   * @param sessionId - 会话 ID，直接导航
   * @param taskId - 任务 ID，需要查找 chat_id
   */
  setNavigationParams: (sessionId: string | null, taskId: string | null) => void;

  /**
   * 清除导航参数（导航完成后调用，防止重复）
   */
  clearNavigationParams: () => void;

  // ==================== 页面来源优先级 (2026-09-15) ====================
  /**
   * 标记 pageSource/platformSource 已由 URL 参数（最高优先级）写入。
   * 内存级标记，不持久化；设置后消息监听器来源不得覆盖 URL 来源值。
   * @param pageSource - 页面来源
   * @param platformSource - 平台来源
   */
  setEntrySourceFromUrl: (
    pageSource: string | null,
    platformSource: string | null,
  ) => void;


  /**
   * 按优先级写入 pageSource/platformSource。
   * 优先级：URL 参数 > 消息监听器 > origin 逻辑。
   * 同步执行，基于 store 当前状态判断是否允许覆盖，避免异步写入竞态。
   * @param pageSource - 页面来源
   * @param platformSource - 平台来源
   * @param priority - 本次写入的来源优先级
   */
  applyEntrySource: (
    pageSource: string | null,
    platformSource: string | null,
    priority: "url" | "message" | "origin",
  ) => void;
}

/** 初始状态 */
const initialState: IframeContext = {
  initialized: false,
  userId: null,
  userName: null,
  clawName: null,
  space: null,
  source: null,
  hideMenu: false,
  isOriginY: false,
  isSuperManager: false,
  manager: false,
  skipPreviewTracking: false,
  authHeaders: [],
  parentOrigin: null,
  receivedAt: null,
  sysId: null,
  token: null,
  bbk: null,
  orgCode: null,
  subBranchId: null,
  orgLvl: null,
  positionId: null,
  userChange: false,
  sessionId: null,
  taskId: null,
  hideChat: false,
  pageSource: null,
  platformSource: null,
  pageSourceFromUrl: false,
  platformSourceFromUrl: false,
};

export const useIframeStore = create<IframeStore>()(
  persist(
    (set) => ({
      ...initialState,
      setContext: (context) =>
        set((state) => ({
          ...state,
          ...context,
          receivedAt: Date.now(),
        })),
      markInitialized: () => set({ initialized: true }),
      clearContext: () => set(initialState),
      setOriginY: (isOriginY) => set({ isOriginY }),
      setAuthHeaders: (authHeaders) => set({ authHeaders }),
      setNavigationParams: (sessionId, taskId) =>
        set({ sessionId, taskId }),
      clearNavigationParams: () => set({ sessionId: null, taskId: null }),
      setEntrySourceFromUrl: (pageSource, platformSource) =>
        set({
          pageSource,
          platformSource,
          pageSourceFromUrl: pageSource != null,
          platformSourceFromUrl: platformSource != null,
        }),
      applyEntrySource: (pageSource, platformSource, priority) =>
        set((state) => {
          // 判断每个字段当前是否允许写入
          let nextPageSource = state.pageSource;
          let nextPlatformSource = state.platformSource;

          // URL 来源为最高优先级，始终可写并锁定
          const pageLockedByUrl = state.pageSourceFromUrl;
          const platformLockedByUrl = state.platformSourceFromUrl;

          if (priority === "url") {
            // URL 参数来源：直接覆盖并锁定，防止后续消息监听器来源覆盖
            nextPageSource = pageSource;
            nextPlatformSource = platformSource;
          } else if (priority === "message") {
            // 消息监听器来源可写入，但不能覆盖已锁定的 URL 值
            if (!pageLockedByUrl) nextPageSource = pageSource;
            if (!platformLockedByUrl) nextPlatformSource = platformSource;
          } else {
            // origin 逻辑（最低优先级），仅在当前无任何来源值时写入
            if (state.pageSource == null) nextPageSource = pageSource;
            if (state.platformSource == null) {
              nextPlatformSource = platformSource;
            }
          }
          return {
            pageSource: nextPageSource,
            platformSource: nextPlatformSource,
            // 仅 URL 来源且成功写入对应字段时锁定；消息/origin 来源保持原锁定状态
            pageSourceFromUrl:
              priority === "url" ? pageSource != null : pageLockedByUrl,
            platformSourceFromUrl:
              priority === "url" ? platformSource != null : platformLockedByUrl,
          };
        }),
    }),
    {
      name: "swe-iframe-context",
      partialize: (state) => ({
        userId: state.userId,
        userName: state.userName,
        clawName: state.clawName,
        space: state.space,
        source: state.source,
        hideMenu: state.hideMenu,
        isSuperManager: state.isSuperManager,
        manager: state.manager,
        skipPreviewTracking: state.skipPreviewTracking,
        authHeaders: state.authHeaders,
        parentOrigin: state.parentOrigin,
        sysId: state.sysId,
        token: state.token,
        bbk: state.bbk,
        orgCode: state.orgCode,
        subBranchId: state.subBranchId,
        orgLvl: state.orgLvl,
        positionId: state.positionId,
        userChange: state.userChange,
        hideChat: state.hideChat,
        pageSource: state.pageSource,
        platformSource: state.platformSource,
        // isOriginY 仅描述本次页面入口，不持久化到后续访问
        // 导航参数不需要持久化，只在首次加载时使用
      }),
      storage: {
        getItem: (name) => {
          try {
            const value = sessionStorage.getItem(name);
            return value ? JSON.parse(value) : null;
          } catch {
            return null;
          }
        },
        setItem: (name, value) => {
          try {
            sessionStorage.setItem(name, JSON.stringify(value));
          } catch {
            // ignore storage errors
          }
        },
        removeItem: (name) => {
          sessionStorage.removeItem(name);
        },
      },
    },
  ),
);

/**
 * 非 React 组件获取 iframe 上下文的辅助函数
 * @returns 当前 iframe 上下文状态
 */
export function getIframeContext(): IframeContext {
  return useIframeStore.getState();
}