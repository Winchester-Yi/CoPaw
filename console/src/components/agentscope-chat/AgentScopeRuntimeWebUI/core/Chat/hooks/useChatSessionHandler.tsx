import { useCallback } from "react";
import ReactDOM from "react-dom";
import { useChatAnywhereSessions } from "../../Context/ChatAnywhereSessionsContext";
import {
  IAgentScopeRuntimeWebUIMessage,
  IAgentScopeRuntimeWebUISessionUpdateOptions,
} from "@/components/agentscope-chat";

/**
 * 处理会话创建和更新的 Hook
 */
export default function useChatSessionHandler() {
  const { createSession, updateSession, getCurrentSessionId, refreshSession } =
    useChatAnywhereSessions();

  /**
   * 确保会话存在，如果不存在则创建
   */
  const ensureSession = useCallback(
    async (query: string) => {
      if (!getCurrentSessionId()) {
        await createSession({ name: query, fromSubmit: true });
      }
    },
    [getCurrentSessionId, createSession],
  );

  /**
   * 更新会话名称（仅在第一次消息时）
   */
  const updateSessionName = useCallback(
    async (
      query: string,
      messages: IAgentScopeRuntimeWebUIMessage[],
      options?: IAgentScopeRuntimeWebUISessionUpdateOptions,
    ) => {
      if (messages.length === 0) {
        await updateSession(
          {
            id: getCurrentSessionId(),
            name: query,
          },
          options,
        );
      }
    },
    [getCurrentSessionId, updateSession],
  );

  /**
   * 同步会话消息
   */
  const syncSessionMessages = useCallback(
    async (messages: IAgentScopeRuntimeWebUIMessage[]) => {
      await updateSession({
        id: getCurrentSessionId(),
        messages: messages,
      });
    },
    [getCurrentSessionId, updateSession],
  );

  const syncSessionMessagesForSession = useCallback(
    async (
      sessionId: string | undefined,
      messages: IAgentScopeRuntimeWebUIMessage[],
      generating?: boolean,
      options?: IAgentScopeRuntimeWebUISessionUpdateOptions,
    ) => {
      if (!sessionId) {
        return;
      }

      await updateSession(
        {
          id: sessionId,
          messages,
          ...(typeof generating === "boolean" ? { generating } : {}),
        },
        options,
      );
    },
    [updateSession],
  );

  const refreshSessionForId = useCallback(
    async (sessionId?: string, reconnectIfGenerating = true) =>
      refreshSession(sessionId, reconnectIfGenerating),
    [refreshSession],
  );

  return {
    ensureSession,
    updateSessionName,
    syncSessionMessages,
    syncSessionMessagesForSession,
    refreshSession: refreshSessionForId,
    getCurrentSessionId,
  };
}
