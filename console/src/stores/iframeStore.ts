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
  plantformSource: null,
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

      // ==================== URL 导航参数 (Kun He, 2026-04-15) ====================
      setNavigationParams: (sessionId, taskId) =>
        set({ sessionId, taskId }),

      clearNavigationParams: () => set({ sessionId: null, taskId: null }),
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
        plantformSource: state.plantformSource,
        // isOriginY 仅描述本次页面入口，不持久化到后续访问。
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
