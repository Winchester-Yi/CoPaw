import { fetchSuggestions } from "@/api/modules/suggestions";
import { useCallback, useEffect, useRef } from "react";
import { useContextSelector } from "use-context-selector";
import { ChatAnywhereSessionsContext } from "../../Context/ChatAnywhereSessionsContext";
import { useChatAnywhereOptions } from "../../Context/ChatAnywhereOptionsContext";

const SUGGESTIONS_POLL_INTERVAL_MS = 800;
const SUGGESTIONS_MAX_POLL_ATTEMPTS = 5;

function delay(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

/**
 * 猜你想问建议获取 Hook
 *
 * 在响应完成后轮询后端 suggestions API，并更新到当前响应中。
 */
export default function useSuggestionsPolling(options: {
  currentQARef: React.MutableRefObject<{
    request?: any;
    response?: any;
    abortController?: AbortController;
  }>;
  updateMessage: (message: any) => void;
}) {
  const { currentQARef, updateMessage } = options;

  const currentSessionId = useContextSelector(
    ChatAnywhereSessionsContext,
    (v) => v.currentSessionId,
  );
  const sessionApi = useChatAnywhereOptions((v) => v.session?.api);

  const sessionIdRef = useRef(currentSessionId);
  const activePollResponseIdRef = useRef<string | null>(null);
  const mountedRef = useRef(true);

  useEffect(() => {
    return () => {
      mountedRef.current = false;
      activePollResponseIdRef.current = null;
    };
  }, []);

  useEffect(() => {
    sessionIdRef.current = currentSessionId;
  }, [currentSessionId]);

  const pollSuggestions = useCallback(async () => {
    const currentResponse = currentQARef.current.response;
    const turnId = currentResponse?.id;
    if (!turnId) {
      console.debug("[Suggestions] No response ID available");
      return;
    }

    activePollResponseIdRef.current = turnId;

    for (let attempt = 1; attempt <= SUGGESTIONS_MAX_POLL_ATTEMPTS; attempt += 1) {
      if (!mountedRef.current || activePollResponseIdRef.current !== turnId) {
        return;
      }

      const latestResponse = currentQARef.current.response;
      if (latestResponse?.id !== turnId) {
        console.debug(
          "[Suggestions] Response ID mismatch, skipping update. Expected:",
          turnId,
          "Current:",
          latestResponse?.id,
        );
        return;
      }

      const latestSessionId = sessionIdRef.current;
      if (!latestSessionId) {
        console.debug("[Suggestions] No session ID available");
        if (attempt < SUGGESTIONS_MAX_POLL_ATTEMPTS) {
          await delay(SUGGESTIONS_POLL_INTERVAL_MS);
        }
        continue;
      }
      const pollingSessionId =
        sessionApi?.getLogicalSessionId?.(latestSessionId) ?? latestSessionId;

      console.debug(
        "[Suggestions] Fetching suggestions for sessionId:",
        pollingSessionId,
        "attempt:",
        attempt,
      );

      try {
        const suggestions = await fetchSuggestions({
          sessionId: pollingSessionId,
          turnId,
        });

        if (!mountedRef.current || activePollResponseIdRef.current !== turnId) {
          console.debug(
            "[Suggestions] Request cancelled, responseId mismatch. Expected:",
            turnId,
            "Active:",
            activePollResponseIdRef.current,
          );
          return;
        }

        if (!suggestions.length) {
          if (attempt < SUGGESTIONS_MAX_POLL_ATTEMPTS) {
            await delay(SUGGESTIONS_POLL_INTERVAL_MS);
          }
          continue;
        }

        const responseToUpdate = currentQARef.current.response;
        if (responseToUpdate?.id !== turnId) {
          console.debug(
            "[Suggestions] Response ID mismatch, skipping update. Expected:",
            turnId,
            "Current:",
            responseToUpdate?.id,
          );
          return;
        }

        if (responseToUpdate?.cards?.[0]?.data) {
          const updatedCards = [
            {
              ...responseToUpdate.cards[0],
              data: {
                ...responseToUpdate.cards[0].data,
                suggestions,
              },
            },
            ...responseToUpdate.cards.slice(1),
          ];

          currentQARef.current.response = {
            ...responseToUpdate,
            cards: updatedCards,
          };

          updateMessage(currentQARef.current.response);
        }
        return;
      } catch (error) {
        console.debug(
          "[Suggestions] Fetch failed:",
          error,
        );
        return;
      }
    }
  }, [currentQARef, updateMessage, sessionApi]);

  return { pollSuggestions };
}
