import {
  sleep,
  type IAgentScopeRuntimeWebUIMessage,
} from "@/components/agentscope-chat";
import { useCallback, useEffect, useRef } from "react";
import { useContextSelector } from "use-context-selector";
import { ChatAnywhereInputContext } from "../../Context/ChatAnywhereInputContext";
import { ChatAnywhereSessionsContext } from "../../Context/ChatAnywhereSessionsContext";
import {
  emit,
  default as useChatAnywhereEventEmitter,
} from "../../Context/useChatAnywhereEventEmitter";
import { InputProps } from "../Input";
import useChatMessageHandler from "./useChatMessageHandler";
import useChatRequest from "./useChatRequest";
import useChatSessionHandler from "./useChatSessionHandler";
import useSuggestionsPolling from "./useSuggestionsPolling";
import { useChatAnywhereOptions } from "../../Context/ChatAnywhereOptionsContext";
import { ChatAnywhereMessagesContext } from "../../Context/ChatAnywhereMessagesContext";
import ReactDOM from "react-dom";
import {
  FollowUpSubmitCoordinator,
  FOLLOW_UP_SUBMIT_FAILED_EVENT,
  RUNTIME_INPUT_SET_CONTENT_EVENT,
  type FollowUpSubmitData,
} from "./followUpSubmit";
import { emitTaskProgressUpdate } from "@/pages/Chat/taskProgressEvents";
import { shouldEnqueueFollowUpSubmission } from "./followUpSubmitState";
import type { CurrentQARef } from "./currentQARef";
import {
  createChatRequestOwner,
  isActiveChatRequestOwner,
  type ChatRequestOwner,
} from "./requestOwnership";
// import mockdata from '../../mock/mock.json'

/**
 * 聊天控制器 Hook - 协调所有聊天相关操作
 */
export default function useChatController() {
  const setLoading = useContextSelector(
    ChatAnywhereInputContext,
    (v) => v.setLoading,
  );
  const setStopping = useContextSelector(
    ChatAnywhereInputContext,
    (v) => v.setStopping,
  );
  const markStopping = setStopping ?? (() => {});
  const getLoading = useContextSelector(
    ChatAnywhereInputContext,
    (v) => v.getLoading,
  );
  const currentSessionId = useContextSelector(
    ChatAnywhereSessionsContext,
    (v) => v.currentSessionId,
  );
  const setSessionNotFound = useContextSelector(
    ChatAnywhereSessionsContext,
    (v) => v.setSessionNotFound,
  );
  const sessionApi = useChatAnywhereOptions((v) => v.session.api);
  const setMessages = useContextSelector(
    ChatAnywhereMessagesContext,
    (v) => v.setMessages,
  );

  const currentQARef = useRef<CurrentQARef["current"]>({});
  const followUpCoordinatorRef = useRef<FollowUpSubmitCoordinator | null>(null);
  const followUpSessionIdRef = useRef<string | undefined>(undefined);
  const previousSessionIdRef = useRef<string | undefined>(undefined);

  // 消息处理
  const messageHandler = useChatMessageHandler({ currentQARef });

  // 会话处理
  const sessionHandler = useChatSessionHandler();

  const applyRecoverySnapshot = useCallback(
    async (history: unknown, owner: ChatRequestOwner) => {
      const runtimeSessionApi = sessionApi as
        | {
            applyChatSnapshot?: (
              sessionId: string,
              history: unknown,
            ) => IAgentScopeRuntimeWebUIMessage[] | undefined;
          }
        | undefined;
      const messages = runtimeSessionApi?.applyChatSnapshot?.(
        owner.sessionId,
        history,
      );
      const ownerIsActive = isActiveChatRequestOwner(
        currentQARef.current.activeRequestOwner,
        owner,
      );
      if (!ownerIsActive) {
        return;
      }
      if (messages) {
        setMessages(messages);
      } else {
        await sessionHandler.refreshSession(owner.sessionId, false);
      }
      if (
        !isActiveChatRequestOwner(
          currentQARef.current.activeRequestOwner,
          owner,
        )
      ) {
        return;
      }
      setSessionNotFound?.(false);
      currentQARef.current.activeRequestOwner = undefined;
      currentQARef.current.abortController = undefined;
      currentQARef.current.response = undefined;
      setLoading(false);
    },
    [sessionApi, sessionHandler, setLoading, setMessages, setSessionNotFound],
  );

  const recoverAfterNotFound = useCallback(
    async (owner: ChatRequestOwner) => {
      const refreshed = await sessionHandler.refreshSession(
        owner.sessionId,
        false,
      );
      const ownerIsActive = isActiveChatRequestOwner(
        currentQARef.current.activeRequestOwner,
        owner,
      );
      if (
        !refreshed &&
        ownerIsActive &&
        sessionHandler.getCurrentSessionId() === owner.sessionId
      ) {
        setSessionNotFound?.(true);
      }
      if (ownerIsActive) {
        currentQARef.current.activeRequestOwner = undefined;
        currentQARef.current.abortController = undefined;
        currentQARef.current.response = undefined;
        setLoading(false);
      }
    },
    [sessionHandler, setLoading, setSessionNotFound],
  );

  // 建议轮询
  const { pollSuggestions } = useSuggestionsPolling({
    currentQARef,
    updateMessage: messageHandler.updateMessage,
  });

  /**
   * 完成响应
   */
  const finishResponse = useCallback(
    (
      status: "finished" | "interrupted" = "finished",
      owner?: ChatRequestOwner,
    ) => {
      const ownerIsActive =
        !owner ||
        isActiveChatRequestOwner(currentQARef.current.activeRequestOwner, owner);
      if (!currentQARef.current.response) {
        if (ownerIsActive && !currentQARef.current.stopPending) {
          setLoading(false);
        }
        if (ownerIsActive) {
          currentQARef.current.activeRequestOwner = undefined;
          currentQARef.current.abortController = undefined;
        }
        return;
      }

      currentQARef.current.response.msgStatus = status;
      if (!currentQARef.current.stopPending) {
        setLoading(false);
      }
      ReactDOM.flushSync(() => {
        messageHandler.updateMessage(currentQARef.current.response);
      });

      sessionHandler.syncSessionMessagesForSession(
        owner?.sessionId ?? currentQARef.current.activeRequestOwner?.sessionId,
        messageHandler.getMessages(),
        false,
        { refreshList: false },
      );

      if (
        !owner ||
        isActiveChatRequestOwner(currentQARef.current.activeRequestOwner, owner)
      ) {
        currentQARef.current.activeRequestOwner = undefined;
        currentQARef.current.abortController = undefined;
      }

      if (status === "finished") {
        pollSuggestions();
      }
    },
    [setLoading, messageHandler, sessionHandler, pollSuggestions],
  );

  // API 请求处理
  const { request, reconnect, cancelActiveRequest } = useChatRequest({
    currentQARef,
    updateMessage: messageHandler.updateMessage,
    hasMessage: messageHandler.hasMessage,
    getCurrentSessionId: sessionHandler.getCurrentSessionId,
    onFinish: (owner) => finishResponse("finished", owner),
    applyRecoverySnapshot,
    recoverAfterNotFound,
  });

  const createRequestOwner = useCallback(
    (kind: ChatRequestOwner["kind"], sessionId: string): ChatRequestOwner => {
      const runtimeSessionApi = sessionApi as
        | {
            getLogicalSessionId?: (sessionId: string) => string;
            getChatIdForSession?: (sessionId: string) => string | null;
          }
        | undefined;

      return createChatRequestOwner({
        kind,
        sessionId,
        logicalSessionId:
          runtimeSessionApi?.getLogicalSessionId?.(sessionId) ?? sessionId,
        chatId: runtimeSessionApi?.getChatIdForSession?.(sessionId) ?? null,
      });
    },
    [sessionApi],
  );

  const submitTurn = useCallback(
    async (data: FollowUpSubmitData) => {
      await sessionHandler.ensureSession(data.query);
      const activeSessionId = sessionHandler.getCurrentSessionId();
      if (!activeSessionId) {
        return;
      }

      const messages = messageHandler.getMessages();
      if (activeSessionId) {
        await sessionHandler.updateSessionName(data.query, messages, {
          refreshList: false,
        });
      }

      messageHandler.createRequestMessage(data);
      await sessionHandler.syncSessionMessagesForSession(
        activeSessionId,
        messageHandler.getMessages(),
        true,
        { refreshList: false },
      );
      setLoading(true);
      await sleep(100);

      currentQARef.current.abortController = new AbortController();
      messageHandler.createResponseMessage();
      const owner = createRequestOwner("submit", activeSessionId);
      currentQARef.current.activeRequestOwner = owner;

      const historyMessages = messageHandler.getHistoryMessages();
      await sessionHandler.syncSessionMessagesForSession(
        activeSessionId,
        messageHandler.getMessages(),
        true,
        { refreshList: false },
      );

      await request(historyMessages, data.biz_params, owner);
    },
    [createRequestOwner, messageHandler, request, sessionHandler, setLoading],
  );

  const isSessionGenerating = useCallback(async () => {
    const sessionId = sessionHandler.getCurrentSessionId();
    if (!sessionId || !sessionApi?.getSession) {
      return false;
    }

    try {
      const session = await sessionApi.getSession(sessionId);
      return Boolean(session?.generating);
    } catch {
      return false;
    }
  }, [sessionApi, sessionHandler]);

  const restorePendingInput = useCallback((data: FollowUpSubmitData) => {
    emit({
      type: RUNTIME_INPUT_SET_CONTENT_EVENT,
      data: {
        content: data.query,
        fileList: data.fileList,
        biz_params: data.biz_params,
      },
    });
  }, []);

  const notifyFollowUpFailure = useCallback(() => {
    emit({
      type: FOLLOW_UP_SUBMIT_FAILED_EVENT,
    });
  }, []);

  const stopActiveRunInBackground = useCallback(async () => {
    const owner = currentQARef.current.activeRequestOwner;
    currentQARef.current.stopPending = true;
    markStopping(true, owner?.sessionId);
    setLoading(true);
    try {
      await cancelActiveRequest();

      if (currentQARef.current.response) {
        currentQARef.current.response.msgStatus = "finished";
        ReactDOM.flushSync(() => {
          messageHandler.updateMessage(currentQARef.current.response!);
        });
      }

      await sessionHandler.syncSessionMessagesForSession(
        owner?.sessionId,
        messageHandler.getMessages(),
      );
    } finally {
      currentQARef.current.stopPending = false;
      markStopping(false, owner?.sessionId);
      if (
        !owner?.sessionId ||
        owner.sessionId === sessionHandler.getCurrentSessionId()
      ) {
        setLoading(false);
      }
    }
  }, [
    cancelActiveRequest,
    markStopping,
    messageHandler,
    sessionHandler,
    setLoading,
  ]);

  if (!followUpCoordinatorRef.current) {
    followUpCoordinatorRef.current = new FollowUpSubmitCoordinator({
      stop: async () => {
        if (
          followUpSessionIdRef.current !== sessionHandler.getCurrentSessionId()
        ) {
          return;
        }

        await stopActiveRunInBackground();
      },
      submit: async (data) => {
        if (
          followUpSessionIdRef.current !== sessionHandler.getCurrentSessionId()
        ) {
          return;
        }

        await submitTurn(data);
      },
      isGenerating: async () => {
        if (
          followUpSessionIdRef.current !== sessionHandler.getCurrentSessionId()
        ) {
          return false;
        }

        return isSessionGenerating();
      },
      restoreInput: (query) => {
        if (
          followUpSessionIdRef.current !== sessionHandler.getCurrentSessionId()
        ) {
          return;
        }

        restorePendingInput(query);
      },
      notifyFailure: () => {
        if (
          followUpSessionIdRef.current !== sessionHandler.getCurrentSessionId()
        ) {
          return;
        }

        notifyFollowUpFailure();
      },
    });
  }

  /**
   * 处理用户提交
   */
  const handleSubmit = useCallback<InputProps["onSubmit"]>(
    async (data) => {
      const generating = shouldEnqueueFollowUpSubmission(
        Boolean(getLoading?.()),
        await isSessionGenerating(),
      );

      if (generating) {
        followUpSessionIdRef.current = sessionHandler.getCurrentSessionId();
        await followUpCoordinatorRef.current?.enqueue(data);
        return;
      }

      await submitTurn(data);
    },
    [getLoading, isSessionGenerating, submitTurn],
  );

  const handleApproval = useCallback(
    async ({ input }) => {
      messageHandler.createApprovalMessage(input);
      const activeSessionId = sessionHandler.getCurrentSessionId();
      if (!activeSessionId) {
        return;
      }

      setLoading(true);
      await sessionHandler.syncSessionMessagesForSession(
        activeSessionId,
        messageHandler.getMessages(),
        true,
      );
      await sleep(100);

      currentQARef.current.abortController = new AbortController();
      messageHandler.createResponseMessage();
      const owner = createRequestOwner("approval", activeSessionId);
      currentQARef.current.activeRequestOwner = owner;
      const historyMessages = messageHandler.getHistoryMessages();
      await sessionHandler.syncSessionMessagesForSession(
        activeSessionId,
        messageHandler.getMessages(),
        true,
      );

      await request(historyMessages, undefined, owner);
    },
    [createRequestOwner, messageHandler, request, sessionHandler, setLoading],
  );

  /**
   * 处理取消
   */
  const handleCancel = useCallback(async () => {
    const owner = currentQARef.current.activeRequestOwner;
    currentQARef.current.stopPending = true;
    markStopping(true, owner?.sessionId);
    setLoading(true);
    try {
      await cancelActiveRequest();
    } finally {
      currentQARef.current.stopPending = false;
      markStopping(false, owner?.sessionId);
      if (
        !owner?.sessionId ||
        owner.sessionId === sessionHandler.getCurrentSessionId()
      ) {
        finishResponse("interrupted", owner);
      }
    }
  }, [
    cancelActiveRequest,
    finishResponse,
    markStopping,
    sessionHandler,
    setLoading,
  ]);

  const handleSuggestionSubmit = useCallback(
    async (data: FollowUpSubmitData) => {
      if (!data?.query || getLoading?.()) {
        return;
      }

      await submitTurn({
        query: data.query,
        fileList: data.fileList || [],
        biz_params: data.biz_params,
      });
    },
    [getLoading, submitTurn],
  );

  /**
   * 处理重新生成
   */
  const handleRegenerate = useCallback(
    async (messageId: string) => {
      const activeSessionId = sessionHandler.getCurrentSessionId();
      if (!activeSessionId) {
        return;
      }

      setLoading(true);

      // 1. 移除旧消息
      messageHandler.removeMessageById(messageId);

      // 2. 创建新的响应消息
      currentQARef.current.abortController = new AbortController();
      messageHandler.createResponseMessage();
      const owner = createRequestOwner("regenerate", activeSessionId);
      currentQARef.current.activeRequestOwner = owner;

      // 3. 发起请求
      const historyMessages = messageHandler.getHistoryMessages();
      await request(historyMessages, undefined, owner);
    },
    [createRequestOwner, messageHandler, request, sessionHandler, setLoading],
  );

  /**
   * 处理 SSE 重连（切回未完成的对话时）
   */
  const handleReconnect = useCallback(
    async (sessionId: string) => {
      currentQARef.current.abortController = new AbortController();
      setLoading(true);

      messageHandler.createResponseMessage();
      const owner = createRequestOwner("reconnect", sessionId);
      currentQARef.current.activeRequestOwner = owner;

      await reconnect(sessionId, owner);
    },
    [createRequestOwner, messageHandler, reconnect, setLoading],
  );

  // 监听会话切换：解除当前 UI 归属，保留旧流读取标题等元信息帧。
  useEffect(() => {
    const previousSessionId = previousSessionIdRef.current;
    previousSessionIdRef.current = currentSessionId;
    if (
      previousSessionId !== undefined &&
      previousSessionId !== currentSessionId
    ) {
      emitTaskProgressUpdate(null);
    }

    followUpSessionIdRef.current = undefined;
    currentQARef.current = {
      request: undefined,
      response: undefined,
      abortController: undefined,
      activeRequestOwner: undefined,
    };
  }, [currentSessionId]);

  // 监听重连事件
  useChatAnywhereEventEmitter(
    {
      type: "handleReconnect",
      callback: async (data) => {
        await handleReconnect(data.detail.session_id);
      },
    },
    [handleReconnect],
  );

  // 监听重新生成事件
  useChatAnywhereEventEmitter({
    type: "handleReplace",
    callback: async (data) => {
      await handleRegenerate(data.detail.id);
    },
  });

  useChatAnywhereEventEmitter(
    {
      type: "handleSubmit",
      callback: async (data) => {
        await handleSubmit(data.detail);
      },
    },
    [handleSubmit],
  );

  useChatAnywhereEventEmitter(
    {
      type: "handleSuggestionSubmit",
      callback: async (data) => {
        await handleSuggestionSubmit(data.detail);
      },
    },
    [handleSuggestionSubmit],
  );

  useChatAnywhereEventEmitter(
    {
      type: "handleApproval",
      callback: async (data) => {
        await handleApproval(data.detail);
      },
    },
    [handleApproval],
  );

  return { handleSubmit, handleCancel };
}
