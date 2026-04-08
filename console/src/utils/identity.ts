/**
 * ============================================================
 * 身份标识 Helper 函数
 * Author: Kun He
 * Date: 2026-04-07
 * ============================================================
 *
 * 提供统一的 userId/channel 获取逻辑
 * 遵循优先级链：iframe > window global > session > default
 *
 * 优先级说明：
 * 1. iframe userId: 父窗口通过 postMessage 传递的 sapId
 * 2. window.currentUserId: session 级别的用户 ID
 * 3. session.user_id: 后端 session 中存储的用户 ID
 * 4. DEFAULT_USER_ID: 兜底默认值
 *
 * 相关文件：
 * - constants/identity.ts: 常量配置
 * - stores/iframeStore.ts: iframe 上下文存储
 * ============================================================
 */

import { DEFAULT_USER_ID, DEFAULT_CHANNEL } from "../constants/identity";
import { getIframeContext } from "../stores/iframeStore";

interface CustomWindow extends Window {
  currentUserId?: string;
  currentChannel?: string;
  currentSessionId?: string;
}

declare const window: CustomWindow;

/**
 * 获取当前用户 ID
 *
 * 优先级：iframe userId > window.currentUserId > sessionUserId > DEFAULT_USER_ID
 *
 * @param sessionUserId - 可选的 session.user_id
 * @returns 解析后的用户 ID
 */
export function getUserId(sessionUserId?: string): string {
  // Priority 1: iframe context (sapId from parent window)
  const iframeContext = getIframeContext();
  if (iframeContext.userId) {
    return iframeContext.userId;
  }

  // Priority 2: window global (session-scoped)
  if (window.currentUserId) {
    return window.currentUserId;
  }

  // Priority 3: session backend value
  if (sessionUserId) {
    return sessionUserId;
  }

  // Priority 4: default fallback
  return DEFAULT_USER_ID;
}

/**
 * 获取当前渠道
 *
 * 优先级：window.currentChannel > sessionChannel > DEFAULT_CHANNEL
 *
 * @param sessionChannel - 可选的 session.channel
 * @returns 解析后的渠道名称
 */
export function getChannel(sessionChannel?: string): string {
  // Priority 1: window global (session-scoped)
  if (window.currentChannel) {
    return window.currentChannel;
  }

  // Priority 2: session backend value
  if (sessionChannel) {
    return sessionChannel;
  }

  // Priority 3: default fallback
  return DEFAULT_CHANNEL;
}

// Re-export constants for convenience
export { DEFAULT_USER_ID, DEFAULT_CHANNEL, DEFAULT_TENANT_ID } from "../constants/identity";