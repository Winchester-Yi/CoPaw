import {
  Bubble,
  useProviderContext,
  IAgentScopeRuntimeWebUIInputData,
  IAgentScopeRuntimeWebUIMessage,
} from "@/components/agentscope-chat";
import { ChatAnywhereMessagesContext } from "../../Context/ChatAnywhereMessagesContext";
import { useContextSelector } from "use-context-selector";
import { ChatAnywhereSessionsContext } from "../../Context/ChatAnywhereSessionsContext";
import cls from "classnames";
import Welcome from "../Welcome";
import React from "react";
import { Result, Spin } from "antd";
import { useChatContentOnly } from "@/components/agentscope-chat/ChatContentOnlyContext";
import { chatApi } from "@/api/modules/chat";
import sessionApi, { convertArchivedPage } from "@/pages/Chat/sessionApi";
import useChatAnywhereEventEmitter from "../../Context/useChatAnywhereEventEmitter";
import useHistoryPreload from "@/components/agentscope-chat/Bubble/hooks/useHistoryPreload";
import { getScrollTopAfterAnchorOffset } from "@/components/agentscope-chat/Bubble/hooks/scrollAnchor";
import HistoryLoadStatus, { type HistoryLoadState } from "./HistoryLoadStatus";

const CONVERSATION_COMPACTION_EVENT = "conversation_compacted";
const CURRENT_HISTORY_BATCH_SIZE = 10;
const ARCHIVE_HISTORY_PAGE_LIMIT = 20;
const COMPACTION_REFRESH_DELAYS_MS = [50, 100, 250, 500, 1_000, 2_000] as const;
const COMPACTION_BOUNDARY_CARD = "ConversationCompactionBoundary";

interface HistoryAnchorTransaction {
  generation: number;
  messageId: string;
  offset: number;
  sessionId: string | undefined;
}

interface PendingCompactionRefresh {
  attempts: number;
  boundaryId?: string;
  chatId: string;
  sessionId: string;
}

function hasGeneratingMessage(
  messages: IAgentScopeRuntimeWebUIMessage[] | undefined,
): boolean {
  return Boolean(
    messages?.some((message) => message.msgStatus === "generating"),
  );
}

function isHistoricalMessage(message: IAgentScopeRuntimeWebUIMessage): boolean {
  return (
    (message as IAgentScopeRuntimeWebUIMessage & { history?: boolean })
      .history === true
  );
}

function normalizeTailValue(value: unknown): unknown {
  if (Array.isArray(value)) return value.map(normalizeTailValue);
  if (!value || typeof value !== "object") return value;
  return Object.fromEntries(
    Object.entries(value as Record<string, unknown>)
      .filter(
        ([key]) =>
          ![
            "id",
            "created_at",
            "completed_at",
            "sequence_number",
            "timestamp",
            "headerMeta",
          ].includes(key),
      )
      .map(([key, nested]) => [key, normalizeTailValue(nested)]),
  );
}

function messageTailFingerprint(
  message: IAgentScopeRuntimeWebUIMessage | undefined,
): string | null {
  const cards = message?.cards
    ?.filter((card) => card.code !== COMPACTION_BOUNDARY_CARD)
    .map((card) => ({
      code: card.code,
      data: normalizeTailValue(card.data),
    }));
  if (!message || !cards?.length) return null;
  return JSON.stringify({ cards, role: message.role });
}

function latestMessageTailFingerprints(
  messages: IAgentScopeRuntimeWebUIMessage[] | undefined,
): string[] {
  const fingerprints: string[] = [];
  for (let index = (messages?.length || 0) - 1; index >= 0; index -= 1) {
    const fingerprint = messageTailFingerprint(messages?.[index]);
    if (!fingerprint) continue;
    fingerprints.unshift(fingerprint);
    if (fingerprints.length === 2) break;
  }
  return fingerprints;
}

function snapshotContainsTail(
  messages: IAgentScopeRuntimeWebUIMessage[] | undefined,
  tailFingerprints: string[],
): boolean {
  const snapshotTailFingerprints = latestMessageTailFingerprints(messages);
  return (
    tailFingerprints.length > 0 &&
    snapshotTailFingerprints.length === tailFingerprints.length &&
    tailFingerprints.every(
      (fingerprint, index) => snapshotTailFingerprints[index] === fingerprint,
    )
  );
}

function snapshotContainsCompactionBoundary(
  messages: IAgentScopeRuntimeWebUIMessage[] | undefined,
  boundaryId: string | undefined,
): boolean {
  if (!boundaryId) return true;
  return Boolean(
    messages?.some(
      (message) =>
        message.cards?.some((card) => {
          if (card.code !== COMPACTION_BOUNDARY_CARD) return false;
          const data = card.data;
          return (
            Boolean(data) &&
            typeof data === "object" &&
            (data as { id?: unknown }).id === boundaryId
          );
        }),
    ),
  );
}

function getVisibleMessageAnchor(scrollElement: HTMLElement) {
  const containerRect = scrollElement.getBoundingClientRect();
  const anchorElement = Array.from(
    scrollElement.querySelectorAll<HTMLElement>("[data-role][id]"),
  )
    .map((element) => ({ element, rect: element.getBoundingClientRect() }))
    .filter(
      ({ rect }) =>
        rect.bottom > containerRect.top && rect.top < containerRect.bottom,
    )
    .sort((left, right) => left.rect.top - right.rect.top)[0];

  if (!anchorElement) return null;
  return {
    messageId: anchorElement.element.id,
    offset: anchorElement.rect.top - containerRect.top,
  };
}

function getHistoryAnchorElement(
  scrollElement: HTMLElement,
  messageId: string,
) {
  return Array.from(
    scrollElement.querySelectorAll<HTMLElement>("[data-role][id]"),
  ).find((element) => element.id === messageId);
}

export default function MessageList(props: {
  onSubmit: (data: IAgentScopeRuntimeWebUIInputData) => void;
}) {
  const isContentOnly = useChatContentOnly();
  const messages = useContextSelector(
    ChatAnywhereMessagesContext,
    (v) => v.messages,
  );
  const setMessages = useContextSelector(
    ChatAnywhereMessagesContext,
    (v) => v.setMessages,
  );
  const safeMessages = React.useMemo(
    () => [...(messages || [])].reverse(),
    [messages],
  );
  const prefixCls = useProviderContext().getPrefixCls(
    "chat-anywhere-message-list",
  );
  const currentSessionId = useContextSelector(
    ChatAnywhereSessionsContext,
    (v) => v.currentSessionId,
  );
  const isSessionLoading = useContextSelector(
    ChatAnywhereSessionsContext,
    (v) => v.isSessionLoading,
  );
  const sessionNotFound = useContextSelector(
    ChatAnywhereSessionsContext,
    (v) => v.sessionNotFound,
  );
  const listRef = React.useRef<{
    scrollToBottom: () => void;
    getScrollElement: () => HTMLDivElement | null;
  } | null>(null);
  const [historyScrollElement, setHistoryScrollElement] =
    React.useState<HTMLDivElement | null>(null);
  const prevMessagesLengthRef = React.useRef(safeMessages.length);
  const historyCursorRef = React.useRef<string | null>(null);
  const historyLoadingRef = React.useRef(false);
  const historyDoneRef = React.useRef(false);
  const loadedArchiveMessageIdsRef = React.useRef(new Set<string>());
  const loadedBoundaryIdsRef = React.useRef(new Set<string>());
  const historyGenerationRef = React.useRef(0);
  const latestMessagesRef = React.useRef(messages);
  const messagesRevisionRef = React.useRef(0);
  const pendingCompactionRefreshRef =
    React.useRef<PendingCompactionRefresh | null>(null);
  const compactionRetryTimerRef = React.useRef<number | null>(null);
  const scheduleCompactionRefreshRef = React.useRef<() => void>(() => {});
  const isPrependingHistoryRef = React.useRef(false);
  const isAtLatestRef = React.useRef(true);
  const pendingHistoryAnchorRef = React.useRef<HistoryAnchorTransaction | null>(
    null,
  );
  const [visibleHistoryCount, setVisibleHistoryCount] = React.useState(
    CURRENT_HISTORY_BATCH_SIZE,
  );
  const [archivePagingStarted, setArchivePagingStarted] = React.useState(false);
  const [historyExhausted, setHistoryExhausted] = React.useState(false);
  const [historyLoadState, setHistoryLoadState] =
    React.useState<HistoryLoadState>("idle");
  const [historyRetrying, setHistoryRetrying] = React.useState(false);
  const backendChatId = sessionApi.getChatIdForSession(currentSessionId || "");
  const historicalMessageCount = React.useMemo(
    () => safeMessages.filter(isHistoricalMessage).length,
    [safeMessages],
  );
  const hasHiddenLocalHistory =
    !archivePagingStarted && historicalMessageCount > visibleHistoryCount;
  const visibleMessages = React.useMemo(() => {
    if (archivePagingStarted) return safeMessages;
    let includedHistoryCount = 0;
    return safeMessages.filter((message) => {
      if (!isHistoricalMessage(message)) return true;
      includedHistoryCount += 1;
      return includedHistoryCount <= visibleHistoryCount;
    });
  }, [archivePagingStarted, safeMessages, visibleHistoryCount]);

  React.useLayoutEffect(() => {
    if (latestMessagesRef.current !== messages) {
      latestMessagesRef.current = messages;
      messagesRevisionRef.current += 1;
    }
  }, [messages]);

  const clearPendingCompactionRefresh = React.useCallback(() => {
    pendingCompactionRefreshRef.current = null;
    if (compactionRetryTimerRef.current !== null) {
      window.clearTimeout(compactionRetryTimerRef.current);
      compactionRetryTimerRef.current = null;
    }
  }, []);

  const scheduleCompactionRefresh = React.useCallback(() => {
    const pending = pendingCompactionRefreshRef.current;
    if (
      !pending ||
      pending.sessionId !== currentSessionId ||
      pending.chatId !== backendChatId ||
      compactionRetryTimerRef.current !== null ||
      hasGeneratingMessage(latestMessagesRef.current) ||
      pending.attempts >= COMPACTION_REFRESH_DELAYS_MS.length
    ) {
      return;
    }
    const tailFingerprints = latestMessageTailFingerprints(
      latestMessagesRef.current,
    );
    if (!tailFingerprints.length) return;

    const messagesRevision = messagesRevisionRef.current;
    const delay = COMPACTION_REFRESH_DELAYS_MS[pending.attempts];
    pending.attempts += 1;
    compactionRetryTimerRef.current = window.setTimeout(() => {
      compactionRetryTimerRef.current = null;
      void sessionApi
        .getSession(pending.sessionId)
        .then((session) => {
          const latestPending = pendingCompactionRefreshRef.current;
          const isConfirmed =
            latestPending === pending &&
            session.generating === false &&
            messagesRevision === messagesRevisionRef.current &&
            !hasGeneratingMessage(latestMessagesRef.current) &&
            latestPending.sessionId === currentSessionId &&
            latestPending.chatId === backendChatId &&
            snapshotContainsCompactionBoundary(
              session.messages,
              pending.boundaryId,
            ) &&
            snapshotContainsTail(session.messages, tailFingerprints);
          if (isConfirmed) {
            pendingCompactionRefreshRef.current = null;
            setMessages(
              (session.messages || []).map((message) => ({
                ...message,
                history: true,
              })),
            );
            return;
          }
          scheduleCompactionRefreshRef.current();
        })
        .catch(() => {
          scheduleCompactionRefreshRef.current();
        });
    }, delay);
  }, [backendChatId, currentSessionId, setMessages]);

  scheduleCompactionRefreshRef.current = scheduleCompactionRefresh;

  React.useEffect(() => {
    scheduleCompactionRefresh();
  }, [messages, scheduleCompactionRefresh]);

  React.useEffect(() => {
    const pending = pendingCompactionRefreshRef.current;
    if (
      pending &&
      (pending.sessionId !== currentSessionId ||
        pending.chatId !== backendChatId)
    ) {
      clearPendingCompactionRefresh();
    }
    return clearPendingCompactionRefresh;
  }, [backendChatId, clearPendingCompactionRefresh, currentSessionId]);

  React.useEffect(() => {
    historyCursorRef.current = null;
    historyLoadingRef.current = false;
    historyDoneRef.current = false;
    loadedArchiveMessageIdsRef.current = new Set();
    loadedBoundaryIdsRef.current = new Set();
    historyGenerationRef.current += 1;
    setVisibleHistoryCount(CURRENT_HISTORY_BATCH_SIZE);
    setArchivePagingStarted(false);
    setHistoryExhausted(false);
    setHistoryLoadState("idle");
    setHistoryRetrying(false);
  }, [backendChatId]);

  const loadOlderHistory = React.useCallback(
    async (retrying = false) => {
      if (
        !backendChatId ||
        historyLoadingRef.current ||
        historyDoneRef.current
      ) {
        return;
      }
      historyLoadingRef.current = true;
      setHistoryRetrying(retrying);
      setHistoryLoadState("loading");
      const generation = historyGenerationRef.current;
      try {
        const page = await chatApi.getChatHistory(
          backendChatId,
          historyCursorRef.current,
          ARCHIVE_HISTORY_PAGE_LIMIT,
        );
        if (generation !== historyGenerationRef.current) return;
        historyDoneRef.current = !page.has_more;
        setHistoryExhausted(!page.has_more);
        historyCursorRef.current = page.next_cursor || null;
        const unseenMessages = (page.messages || []).filter((message) => {
          if (typeof message.id !== "string") return true;
          if (loadedArchiveMessageIdsRef.current.has(message.id)) return false;
          loadedArchiveMessageIdsRef.current.add(message.id);
          return true;
        });
        const unseenBoundaries = (page.boundaries || []).filter((boundary) => {
          if (loadedBoundaryIdsRef.current.has(boundary.id)) return false;
          loadedBoundaryIdsRef.current.add(boundary.id);
          return true;
        });
        const older = convertArchivedPage(unseenMessages, unseenBoundaries).map(
          (message) => ({ ...message, history: true }),
        );
        const knownMessageIds = new Set(
          (messages || []).map((message) => message.id),
        );
        const uniqueOlder = older.filter(
          (message) => !knownMessageIds.has(message.id),
        );
        const scrollElement = listRef.current?.getScrollElement();
        if (uniqueOlder.length > 0 && scrollElement) {
          const anchor = getVisibleMessageAnchor(scrollElement);
          pendingHistoryAnchorRef.current = anchor
            ? {
                ...anchor,
                generation,
                sessionId: currentSessionId,
              }
            : null;
        }
        isPrependingHistoryRef.current = uniqueOlder.length > 0;
        // @ts-expect-error Context exposes a React-style updater at runtime but omits it from its public type.
        setMessages((current) => {
          const known = new Set(current.map((message) => message.id));
          return [
            ...uniqueOlder.filter((message) => !known.has(message.id)),
            ...current,
          ];
        });
        setHistoryLoadState(page.has_more ? "idle" : "exhausted");
      } catch {
        if (generation === historyGenerationRef.current) {
          setHistoryLoadState("error");
        }
      } finally {
        if (generation === historyGenerationRef.current) {
          historyLoadingRef.current = false;
          setHistoryRetrying(false);
        }
      }
    },
    [backendChatId, currentSessionId, messages, setMessages],
  );

  const captureVisibleHistoryAnchor = React.useCallback(() => {
    const scrollElement = listRef.current?.getScrollElement();
    if (!scrollElement) return;
    const anchor = getVisibleMessageAnchor(scrollElement);
    pendingHistoryAnchorRef.current = anchor
      ? {
          ...anchor,
          generation: historyGenerationRef.current,
          sessionId: currentSessionId,
        }
      : null;
  }, [currentSessionId]);

  const advanceHistory = React.useCallback(async () => {
    if (hasHiddenLocalHistory) {
      captureVisibleHistoryAnchor();
      isPrependingHistoryRef.current = true;
      setVisibleHistoryCount((current) =>
        Math.min(current + CURRENT_HISTORY_BATCH_SIZE, historicalMessageCount),
      );
      return;
    }

    setArchivePagingStarted(true);
    await loadOlderHistory();
  }, [
    captureVisibleHistoryAnchor,
    hasHiddenLocalHistory,
    historicalMessageCount,
    loadOlderHistory,
  ]);

  React.useLayoutEffect(() => {
    const nextScrollElement = listRef.current?.getScrollElement() ?? null;
    setHistoryScrollElement((current) =>
      current === nextScrollElement ? current : nextScrollElement,
    );
  }, [currentSessionId, safeMessages.length]);

  React.useLayoutEffect(() => {
    const transaction = pendingHistoryAnchorRef.current;
    const scrollElement = listRef.current?.getScrollElement();
    if (
      !transaction ||
      !scrollElement ||
      transaction.generation !== historyGenerationRef.current ||
      transaction.sessionId !== currentSessionId
    ) {
      pendingHistoryAnchorRef.current = null;
      return;
    }

    const restoreAnchor = (anchor: HistoryAnchorTransaction) => {
      const anchorElement = getHistoryAnchorElement(
        scrollElement,
        anchor.messageId,
      );
      if (!anchorElement) return;
      const nextOffset =
        anchorElement.getBoundingClientRect().top -
        scrollElement.getBoundingClientRect().top;
      scrollElement.scrollTop = getScrollTopAfterAnchorOffset({
        oldScrollTop: scrollElement.scrollTop,
        previousOffset: anchor.offset,
        nextOffset,
      });
    };

    restoreAnchor(transaction);
    const frameId = requestAnimationFrame(() => {
      const pending = pendingHistoryAnchorRef.current;
      if (
        !pending ||
        pending !== transaction ||
        pending.generation !== historyGenerationRef.current ||
        pending.sessionId !== currentSessionId
      ) {
        return;
      }
      restoreAnchor(pending);
      pendingHistoryAnchorRef.current = null;
    });
    return () => cancelAnimationFrame(frameId);
  }, [currentSessionId, visibleMessages]);

  useHistoryPreload({
    scrollElement: historyScrollElement,
    onNearStart: advanceHistory,
    disabled:
      historyLoadState !== "idle" ||
      (!hasHiddenLocalHistory && (!backendChatId || historyExhausted)),
    resetKey: backendChatId,
  });

  useChatAnywhereEventEmitter(
    {
      type: CONVERSATION_COMPACTION_EVENT,
      callback: (event) => {
        const detail = event.detail as
          | { boundary?: { id?: unknown }; chat_id?: unknown }
          | undefined;
        if (
          typeof detail?.chat_id !== "string" ||
          detail.chat_id !== backendChatId ||
          !currentSessionId
        ) {
          return;
        }
        historyCursorRef.current = null;
        historyLoadingRef.current = false;
        historyDoneRef.current = false;
        loadedArchiveMessageIdsRef.current = new Set();
        loadedBoundaryIdsRef.current = new Set();
        historyGenerationRef.current += 1;
        setHistoryExhausted(false);
        setHistoryLoadState("idle");
        setHistoryRetrying(false);
        clearPendingCompactionRefresh();
        pendingCompactionRefreshRef.current = {
          attempts: 0,
          boundaryId:
            typeof detail.boundary?.id === "string"
              ? detail.boundary.id
              : undefined,
          chatId: detail.chat_id,
          sessionId: currentSessionId,
        };
        scheduleCompactionRefreshRef.current();
      },
    },
    [backendChatId, clearPendingCompactionRefresh, currentSessionId],
  );

  React.useEffect(() => {
    if (
      safeMessages.length > prevMessagesLengthRef.current &&
      !isPrependingHistoryRef.current &&
      isAtLatestRef.current
    ) {
      listRef.current?.scrollToBottom();
    }
    isPrependingHistoryRef.current = false;
    prevMessagesLengthRef.current = safeMessages.length;
  }, [safeMessages.length]);

  // 当正在加载会话时，显示加载指示器而不是欢迎页
  // 避免在切换会话时闪现"新建会话"页面
  if (isSessionLoading) {
    return (
      <div className={cls(prefixCls, `${prefixCls}-loading`)}>
        <Spin size="large" />
      </div>
    );
  }

  if (isContentOnly && sessionNotFound) {
    return (
      <div className={cls(prefixCls, `${prefixCls}-welcome`)}>
        <Result
          status="404"
          title="会话不存在"
          subTitle="该会话不存在或已被删除"
        />
      </div>
    );
  }

  if (safeMessages.length === 0) {
    return (
      <div className={cls(prefixCls, `${prefixCls}-welcome`)}>
        {!isContentOnly && <Welcome onSubmit={props.onSubmit} />}
      </div>
    );
  }

  return (
    <Bubble.List
      ref={listRef}
      pagination={false}
      disableBrowserScrollAnchoring
      order="desc"
      key={currentSessionId}
      classNames={{
        wrapper: prefixCls,
      }}
      items={visibleMessages}
      preserveScrollPosition={isPrependingHistoryRef.current}
      autoScrollToBottom="initial"
      onBottomStateChange={(isAtBottom) => {
        isAtLatestRef.current = isAtBottom;
      }}
      topContent={
        <HistoryLoadStatus
          state={historyLoadState}
          retrying={historyRetrying}
          onRetry={() => void loadOlderHistory(true)}
        />
      }
    />
  );
}
