import { uuid } from "@/components/agentscope-chat";
import { useCallback } from "react";
import ReactDOM from "react-dom";
import { useChatAnywhereMessages } from "../../Context/ChatAnywhereMessagesContext";
import AgentScopeRuntimeRequestBuilder from "../../AgentScopeRuntime/Request/Builder";
import { InputProps } from "../Input";
import { withRequestHeaderMeta } from "./headerMeta";
import type { CurrentQARef } from "./currentQARef";

interface UseChatMessageHandlerOptions {
  currentQARef: CurrentQARef;
}

/**
 * 处理消息创建和更新的 Hook
 */
export default function useChatMessageHandler(
  options: UseChatMessageHandlerOptions,
) {
  const { currentQARef } = options;
  const { updateMessage, getMessages, removeMessage } =
    useChatAnywhereMessages();

  /**
   * 创建用户请求消息
   */
  const createRequestMessage = useCallback(
    (data: Parameters<InputProps["onSubmit"]>[0]) => {
      currentQARef.current.abortController = new AbortController();
      const requestTimestamp = Date.now();
      currentQARef.current.request = {
        id: uuid(),
        role: "user",
        cards: [
          {
            code: "AgentScopeRuntimeRequestCard",
            data: withRequestHeaderMeta(
              new AgentScopeRuntimeRequestBuilder().handle(data),
              requestTimestamp,
            ),
          },
        ],
      };

      ReactDOM.flushSync(() => {
        updateMessage(currentQARef.current.request!);
      });

      return currentQARef.current.request;
    },
    [currentQARef, updateMessage],
  );

  const createApprovalMessage = useCallback(
    (data) => {
      currentQARef.current.abortController = new AbortController();
      const requestTimestamp = Date.now();

      currentQARef.current.request = {
        id: uuid(),
        role: "user",
        cards: [
          {
            code: "AgentScopeRuntimeRequestCard",
            data: withRequestHeaderMeta(
              new AgentScopeRuntimeRequestBuilder().handleApproval(data),
              requestTimestamp,
            ),
          },
        ],
      };

      ReactDOM.flushSync(() => {
        updateMessage(currentQARef.current.request!);
      });

      return currentQARef.current.request;
    },
    [currentQARef, updateMessage],
  );

  /**
   * 创建助手响应消息
   */
  const createResponseMessage = useCallback(() => {
    const responseTimestamp = Date.now();
    currentQARef.current.response = {
      id: uuid(),
      role: "assistant",
      cards: [],
      msgStatus: "generating",
      liveHeaderTimestamp: responseTimestamp,
    };

    ReactDOM.flushSync(() => {
      updateMessage(currentQARef.current.response!);
    });

    return currentQARef.current.response;
  }, [currentQARef, updateMessage]);

  /**
   * 获取历史消息（用于 API 请求）
   */
  const getHistoryMessages = useCallback(() => {
    return AgentScopeRuntimeRequestBuilder.getHistoryMessages(getMessages());
  }, [getMessages]);

  const hasMessage = useCallback(
    (id: string) => {
      return getMessages().some((item) => item.id === id);
    },
    [getMessages],
  );

  /**
   * 移除指定消息
   */
  const removeMessageById = useCallback(
    (id: string) => {
      ReactDOM.flushSync(() => {
        removeMessage({ id });
      });
    },
    [removeMessage],
  );

  return {
    createRequestMessage,
    createApprovalMessage,
    createResponseMessage,
    getHistoryMessages,
    updateMessage,
    removeMessageById,
    getMessages,
    hasMessage,
  };
}
