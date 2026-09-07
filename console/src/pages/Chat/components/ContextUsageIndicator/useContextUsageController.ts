import { useCallback, useEffect, useRef, useState } from "react";
import { chatApi } from "@/api/modules/chat";
import type { ContextUsageSnapshot } from "@/api/types/contextUsage";

const COMPACTION_EVENT = "conversation_compacted";
const MODEL_SWITCHED_EVENT = "model-switched";
const POST_COMPLETION_RETRY_DELAY_MS = 150;
const POST_COMPLETION_MAX_RETRIES = 3;

interface ContextUsageController {
  snapshot?: ContextUsageSnapshot;
  error: boolean;
  refresh: () => void;
}

export function useContextUsageController(
  chatId: string | null,
  loading: boolean,
): ContextUsageController {
  const [snapshots, setSnapshots] = useState<
    Record<string, ContextUsageSnapshot>
  >({});
  const [errorChatId, setErrorChatId] = useState<string | null>(null);
  const activeChatIdRef = useRef(chatId);
  const requestVersionRef = useRef(0);
  const previousLoadingRef = useRef(loading);
  const retryTimerRef = useRef<number | null>(null);
  activeChatIdRef.current = chatId;

  const clearRetryTimer = useCallback(() => {
    if (retryTimerRef.current !== null) {
      window.clearTimeout(retryTimerRef.current);
      retryTimerRef.current = null;
    }
  }, []);

  const fetchSnapshot = useCallback(
    (retryStale: boolean) => {
      const requestedChatId = activeChatIdRef.current;
      if (!requestedChatId) return;

      clearRetryTimer();
      const requestVersion = ++requestVersionRef.current;
      const request = async (retries: number): Promise<void> => {
        try {
          const snapshot = await chatApi.getContextUsage(requestedChatId);
          if (
            requestVersion !== requestVersionRef.current ||
            activeChatIdRef.current !== requestedChatId
          ) {
            return;
          }
          setSnapshots((current) => ({
            ...current,
            [requestedChatId]: snapshot,
          }));
          setErrorChatId((current) =>
            current === requestedChatId ? null : current,
          );
          if (
            !retryStale ||
            !snapshot.available ||
            !snapshot.stale ||
            retries >= POST_COMPLETION_MAX_RETRIES
          ) {
            return;
          }
        } catch {
          if (
            requestVersion === requestVersionRef.current &&
            activeChatIdRef.current === requestedChatId
          ) {
            setErrorChatId(requestedChatId);
          }
          return;
        }

        retryTimerRef.current = window.setTimeout(() => {
          retryTimerRef.current = null;
          void request(retries + 1);
        }, POST_COMPLETION_RETRY_DELAY_MS);
      };

      void request(0);
    },
    [clearRetryTimer],
  );

  const refresh = useCallback(() => {
    fetchSnapshot(false);
  }, [fetchSnapshot]);

  useEffect(() => {
    clearRetryTimer();
    requestVersionRef.current += 1;
    if (chatId) {
      fetchSnapshot(false);
    }
  }, [chatId, clearRetryTimer, fetchSnapshot]);

  useEffect(() => {
    if (previousLoadingRef.current && !loading) {
      fetchSnapshot(true);
    }
    previousLoadingRef.current = loading;
  }, [loading, fetchSnapshot]);

  useEffect(() => {
    const handleCompaction = (event: Event) => {
      const detail = (event as CustomEvent<{ chat_id?: unknown }>).detail;
      if (detail?.chat_id === activeChatIdRef.current) {
        fetchSnapshot(false);
      }
    };
    const handleModelSwitch = () => {
      fetchSnapshot(false);
    };

    document.addEventListener(COMPACTION_EVENT, handleCompaction);
    window.addEventListener(MODEL_SWITCHED_EVENT, handleModelSwitch);
    return () => {
      document.removeEventListener(COMPACTION_EVENT, handleCompaction);
      window.removeEventListener(MODEL_SWITCHED_EVENT, handleModelSwitch);
    };
  }, [fetchSnapshot]);

  useEffect(
    () => () => {
      clearRetryTimer();
      requestVersionRef.current += 1;
      activeChatIdRef.current = null;
    },
    [clearRetryTimer],
  );

  return {
    snapshot: chatId ? snapshots[chatId] : undefined,
    error: errorChatId === chatId,
    refresh,
  };
}
