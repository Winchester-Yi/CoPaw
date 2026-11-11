import { createContext, useContextSelector } from "use-context-selector";
import { IAgentScopeRuntimeWebUISessionsContext } from "../types/ISessions";
import {
  IAgentScopeRuntimeWebUISessionAPI,
  IAgentScopeRuntimeWebUIMessage,
  IAgentScopeRuntimeWebUISessionUpdateOptions,
} from "../types";
import { useGetState, useMount } from "ahooks";
import { IAgentScopeRuntimeWebUISession } from "../types/ISessions";
import React from "react";
import { ChatAnywhereMessagesContext } from "./ChatAnywhereMessagesContext";
import { useChatAnywhereOptions } from "./ChatAnywhereOptionsContext";
import ReactDOM from "react-dom";
import { useAsyncEffect } from "ahooks";
import useChatAnywhereEventEmitter, {
  emit,
} from "./useChatAnywhereEventEmitter";
import { getInitialSessionId } from "@/pages/Chat/sessionApi/initialSessionSelection";
import { shouldApplySessionLoadResult } from "@/pages/Chat/sessionApi/sessionRaceGuard";

export const SESSION_TITLE_PATCH_EVENT = "patchSessionTitle";
export const SESSION_TITLE_GENERATED_META_KEY = "session_title_generated";

interface SessionTitlePatchPayload {
  chat_id?: string;
  session_id?: string;
  session_title?: string;
}

interface NormalizedSessionTitlePatchPayload {
  chat_id?: string;
  session_id?: string;
  session_title: string;
}

type SessionWithTitleIdentity = IAgentScopeRuntimeWebUISession & {
  meta?: Record<string, unknown>;
  realId?: string;
  sessionId?: string;
};

interface SessionApiWithIntent {
  setSelectedSessionIntent?: (sessionId: string | undefined | null) => void;
}

interface SessionApiWithTitlePatch extends SessionApiWithIntent {
  patchSessionTitle?: (
    payload: unknown,
  ) => IAgentScopeRuntimeWebUISession[] | void;
}

interface SessionOptions {
  api?: IAgentScopeRuntimeWebUISessionAPI & SessionApiWithTitlePatch;
}

interface LoadSessionMessagesOptions {
  requestedSessionId: string | undefined;
  clearBeforeLoad: boolean;
  finishLoadingWithoutSession?: boolean;
  options: SessionOptions;
  setMessages: (messages: IAgentScopeRuntimeWebUIMessage[]) => void;
  getCurrentSessionId: () => string | undefined;
  setSessionLoading?: (loading: boolean) => void;
  setSessionNotFound?: (notFound: boolean) => void;
  reconnectIfGenerating?: boolean;
}

// Exported for focused loader regression coverage.
// eslint-disable-next-line react-refresh/only-export-components
export async function loadSessionMessages({
  requestedSessionId,
  clearBeforeLoad,
  finishLoadingWithoutSession = false,
  options,
  setMessages,
  getCurrentSessionId,
  setSessionLoading,
  setSessionNotFound,
  reconnectIfGenerating = true,
}: LoadSessionMessagesOptions): Promise<boolean> {
  if (!requestedSessionId || !options.api) {
    if (clearBeforeLoad) {
      ReactDOM.flushSync(() => {
        if (finishLoadingWithoutSession) {
          setSessionLoading?.(false);
        }
        setMessages([]);
      });
    } else if (finishLoadingWithoutSession) {
      setSessionLoading?.(false);
    }
    return false;
  }

  const sessionApi = options.api as SessionApiWithIntent | undefined;
  sessionApi?.setSelectedSessionIntent?.(requestedSessionId);

  if (clearBeforeLoad) {
    // 使用 flushSync 确保 loading 状态和消息清空同步更新
    // 避免 React 状态更新异步导致先显示欢迎页再显示 loading
    ReactDOM.flushSync(() => {
      setSessionLoading?.(true);
      setMessages([]);
    });
  } else {
    setSessionLoading?.(true);
  }

  try {
    const session = await options.api.getSession(requestedSessionId);
    if (
      !shouldApplySessionLoadResult({
        requestedSessionId,
        currentSessionId: getCurrentSessionId(),
      })
    ) {
      // 竞态条件：当前会话已变更，此请求结果不应用
      // 不清除 loading，因为另一个请求正在加载当前会话
      return false;
    }

    const messages = session?.messages || [];
    setMessages(
      messages.map((item) => {
        return {
          ...item,
          history: true,
        };
      }),
    );

    if (session?.generating && reconnectIfGenerating) {
      emit({
        type: "handleReconnect",
        data: { session_id: requestedSessionId },
      });
    }

    return true;
  } catch (error) {
    const status =
      error && typeof error === "object"
        ? (error as { status?: unknown }).status
        : undefined;
    if (status !== 404) {
      throw error;
    }

    if (
      !shouldApplySessionLoadResult({
        requestedSessionId,
        currentSessionId: getCurrentSessionId(),
      })
    ) {
      return false;
    }

    setSessionNotFound?.(true);
    return false;
  } finally {
    // 只有当请求成功应用时才清除 loading
    // 竞态失败的请求不应清除 loading，让获胜的请求来清除
    const currentId = getCurrentSessionId();
    if (requestedSessionId === currentId) {
      setSessionLoading?.(false);
    }
  }
}

function getTitlePatchPayload(
  payload: unknown,
): NormalizedSessionTitlePatchPayload | undefined {
  if (!payload || typeof payload !== "object") {
    return undefined;
  }

  const titlePatch = payload as SessionTitlePatchPayload;
  const sessionTitle =
    typeof titlePatch.session_title === "string"
      ? titlePatch.session_title.trim()
      : "";
  if (!sessionTitle) {
    return undefined;
  }

  const sessionId =
    typeof titlePatch.session_id === "string"
      ? titlePatch.session_id.trim()
      : "";
  const chatId =
    typeof titlePatch.chat_id === "string" ? titlePatch.chat_id.trim() : "";
  if (!sessionId && !chatId) {
    return undefined;
  }

  return {
    chat_id: chatId || undefined,
    session_id: sessionId || undefined,
    session_title: sessionTitle,
  };
}

function sessionMatchesTitlePatch(
  session: SessionWithTitleIdentity,
  payload: NormalizedSessionTitlePatchPayload,
) {
  const candidateIds = new Set<string>(
    [payload.session_id, payload.chat_id].filter((id): id is string =>
      Boolean(id),
    ),
  );

  return [session.id, session.realId, session.sessionId].some(
    (id) => typeof id === "string" && candidateIds.has(id),
  );
}

export function patchSessionTitleInList(
  sessions: IAgentScopeRuntimeWebUISession[],
  payload: unknown,
) {
  const titlePatch = getTitlePatchPayload(payload);
  if (!titlePatch) {
    return sessions;
  }

  let changed = false;
  const patchedSessions = sessions.map((session) => {
    const sessionWithIdentity = session as SessionWithTitleIdentity;
    if (!sessionMatchesTitlePatch(sessionWithIdentity, titlePatch)) {
      return session;
    }
    if (sessionWithIdentity.meta?.session_kind === "task") {
      return session;
    }

    const generatedMeta =
      sessionWithIdentity.meta?.[SESSION_TITLE_GENERATED_META_KEY] === true;
    if (
      sessionWithIdentity.name === titlePatch.session_title &&
      generatedMeta
    ) {
      return session;
    }

    changed = true;
    return {
      ...sessionWithIdentity,
      name: titlePatch.session_title,
      meta: {
        ...sessionWithIdentity.meta,
        [SESSION_TITLE_GENERATED_META_KEY]: true,
      },
    };
  });

  return changed ? patchedSessions : sessions;
}

export const ChatAnywhereSessionsContext =
  createContext<IAgentScopeRuntimeWebUISessionsContext>({
    sessions: [],
    setSessions: () => {},
    getSessions: () => [],
    currentSessionId: undefined,
    setCurrentSessionId: () => {},
    getCurrentSessionId: () => "",
    isSessionLoading: false,
    setSessionLoading: () => {},
    sessionNotFound: false,
    setSessionNotFound: () => {},
    isSessionsListLoading: true,
    setSessionsListLoading: () => {},
  });

export function ChatAnywhereSessionsContextProvider(props: {
  children: React.ReactNode | React.ReactNode[];
}) {
  const options = useChatAnywhereOptions((v) => v.session);
  const [sessions, setSessions, getSessions] = useGetState<
    IAgentScopeRuntimeWebUISession[]
  >([]);
  const [currentSessionId, setCurrentSessionId, getCurrentSessionId] =
    useGetState<string | undefined>(undefined);
  const [isSessionLoading, setSessionLoading] = useGetState<boolean>(false);
  const [sessionNotFound, setSessionNotFound] = useGetState<boolean>(false);
  const [isSessionsListLoading, setSessionsListLoading] =
    useGetState<boolean>(true);
  const sessionApi = options.api;

  useMount(async () => {
    setSessionsListLoading(true);
    try {
      const sessionList = await options.api.getSessionList();
      setSessions(sessionList);
      setCurrentSessionId(
        getInitialSessionId({
          pathname: window.location.pathname,
          sessionList,
        }),
      );
    } finally {
      setSessionsListLoading(false);
    }
  });

  useChatAnywhereEventEmitter(
    {
      type: SESSION_TITLE_PATCH_EVENT,
      callback: (event) => {
        sessionApi?.patchSessionTitle?.(event.detail);
        setSessions(patchSessionTitleInList(getSessions(), event.detail));
      },
    },
    [getSessions, sessionApi, setSessions],
  );

  return (
    <ChatAnywhereSessionsContext.Provider
      value={{
        sessions,
        setSessions,
        getSessions,
        currentSessionId,
        setCurrentSessionId,
        getCurrentSessionId,
        isSessionLoading,
        setSessionLoading,
        sessionNotFound,
        setSessionNotFound,
        isSessionsListLoading,
        setSessionsListLoading,
      }}
    >
      {props.children}
    </ChatAnywhereSessionsContext.Provider>
  );
}

/**
 * 会话切换时加载消息和判断重连的 hook，必须保证只挂载一次
 */
export const useChatAnywhereSessionLoader = ({
  finishLoadingWithoutSession = false,
}: {
  finishLoadingWithoutSession?: boolean;
} = {}) => {
  const currentSessionId = useContextSelector(
    ChatAnywhereSessionsContext,
    (v) => v.currentSessionId,
  );
  const options = useChatAnywhereOptions((v) => v.session);
  const setMessages = useContextSelector(
    ChatAnywhereMessagesContext,
    (v) => v.setMessages,
  );
  const getCurrentSessionId = useContextSelector(
    ChatAnywhereSessionsContext,
    (v) => v.getCurrentSessionId,
  );
  const setSessionLoading = useContextSelector(
    ChatAnywhereSessionsContext,
    (v) => v.setSessionLoading,
  );
  const setSessionNotFound = useContextSelector(
    ChatAnywhereSessionsContext,
    (v) => v.setSessionNotFound,
  );

  useAsyncEffect(async () => {
    await loadSessionMessages({
      requestedSessionId: currentSessionId,
      clearBeforeLoad: true,
      finishLoadingWithoutSession,
      options,
      setMessages,
      getCurrentSessionId,
      setSessionLoading,
      setSessionNotFound,
    });
  }, [currentSessionId, finishLoadingWithoutSession]);
};

/**
 * 获取会话列表的 reactive 状态，供外部自定义会话面板使用
 */
export const useChatAnywhereSessionsState = () => {
  return useContextSelector(ChatAnywhereSessionsContext, (v) => v);
};

export const useChatAnywhereSessions = () => {
  const { setSessions, getSessions, getCurrentSessionId, setCurrentSessionId } =
    useContextSelector(ChatAnywhereSessionsContext, (v) => v);
  const options = useChatAnywhereOptions((v) => v.session);
  const setMessages = useContextSelector(
    ChatAnywhereMessagesContext,
    (v) => v.setMessages,
  );

  const removeSession = React.useCallback(
    async (
      session: Partial<IAgentScopeRuntimeWebUISession> & { id: string },
    ) => {
      const res = await options.api.removeSession(session);
      setMessages([]);
      setCurrentSessionId(undefined);
      setSessions(res);
    },
    [],
  );

  const updateSession = React.useCallback(
    async (
      session: Partial<IAgentScopeRuntimeWebUISession> & {
        __fromSubmit?: boolean;
      },
      updateOptions?: IAgentScopeRuntimeWebUISessionUpdateOptions,
    ) => {
      const res = session.id
        ? await options.api.updateSession(session, updateOptions)
        : await options.api.createSession(session);

      setSessions(res);
      return session;
    },
    [],
  );

  const createSession = React.useCallback(
    async (data?: { name?: string; fromSubmit?: boolean }) => {
      const session = await updateSession({
        name: data?.name,
        messages: [],
        __fromSubmit: data?.fromSubmit,
      });
      setCurrentSessionId(session.id);
      setMessages(session.messages);
      return session.id;
    },
    [],
  );

  const changeCurrentSessionId = React.useCallback((sessionId: string) => {
    setCurrentSessionId(sessionId);
  }, []);

  const refreshSession = React.useCallback(
    async (sessionId?: string, reconnectIfGenerating = true) => {
      const requestedSessionId = sessionId ?? getCurrentSessionId();
      return loadSessionMessages({
        requestedSessionId,
        clearBeforeLoad: false,
        options,
        setMessages,
        getCurrentSessionId,
        reconnectIfGenerating,
      });
    },
    [getCurrentSessionId, options, setMessages],
  );

  return {
    changeCurrentSessionId,
    getCurrentSessionId,
    getSessions,
    removeSession,
    updateSession,
    createSession,
    refreshSession,
  };
};
