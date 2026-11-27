import { useCallback, useEffect, useRef, useState } from "react";
import { request } from "../../api/request";

export type RefreshSpan = {
  span_id: string;
  attempt_id: string | null;
  status: string | null;
  subagent?: unknown;
};
export type RefreshAttempt = { attempt_id: string; attempt_index: number };
type RefreshPage<S extends RefreshSpan, A extends RefreshAttempt, R> = {
  run: R;
  attempts: A[];
  spans: S[];
  spans_next_cursor: string | null;
  generated_at: string;
};
type Options<S extends RefreshSpan, A extends RefreshAttempt, R> = {
  scopeKey: string;
  runId: string;
  attemptId: string | undefined;
  runStatus: string | null;
  enabled?: boolean;
  acquire?: () => (() => void) | null;
  spans: S[];
  cursor: string | null;
  boundaryId: string | null;
  attempts: A[];
  onRefresh: (result: {
    run: R;
    attempts: A[];
    mergePage: (current: PageState<S>) => PageState<S>;
    generatedAt: string;
    refreshedAt: number;
    incomplete: boolean;
  }) => void;
};
export type PageState<S extends RefreshSpan> = {
  spans: S[];
  cursor: string | null;
  boundaryId: string | null;
};
const ACTIVE = new Set([
  "accepted",
  "pending",
  "queued",
  "running",
  "waiting_approval",
]);
const MAX_PAGES = 100;
const MAX_BACKGROUND_QUERIES = 20;
const TERMINAL = new Set([
  "succeeded",
  "failed",
  "cancelled",
  "timed_out",
  "rejected",
  "completed",
  "timeout",
]);

export function hasActiveExecution(
  status: string | null,
  spans: RefreshSpan[],
) {
  return (
    ACTIVE.has(status || "") ||
    spans.some((span) => {
      const subagent = span.subagent as {
        mode?: string;
        result_status?: string;
      } | null;
      return (
        subagent?.mode === "background" &&
        (ACTIVE.has(span.status || "") ||
          ACTIVE.has(subagent.result_status || ""))
      );
    })
  );
}

/** Refreshes from the first page while retaining the previously loaded boundary. */
export function useRunRefresh<
  S extends RefreshSpan,
  A extends RefreshAttempt,
  R,
>(options: Options<S, A, R>) {
  const latest = useRef(options);
  latest.current = options;
  const generation = useRef(0);
  const busy = useRef(false);
  const [refreshing, setRefreshing] = useState(false);
  const [error, setError] = useState(false);
  const [newAttempt, setNewAttempt] = useState<A | null>(null);
  const [background, setBackground] = useState<Map<string, string | null>>(
    () => new Map(),
  );
  const backgroundRef = useRef(background);
  backgroundRef.current = background;
  const backgroundOffset = useRef(0);
  const refresh = useCallback(async () => {
    if (busy.current || latest.current.enabled === false) return;
    const release = latest.current.acquire?.();
    if (latest.current.acquire && !release) return;
    busy.current = true;
    const version = generation.current;
    const { runId, attemptId, cursor, boundaryId, attempts } = latest.current;
    const refreshed = new Map<string, S>();
    let next: string | null = null;
    let found = !boundaryId;
    let page: RefreshPage<S, A, R> | undefined;
    let count = 0;
    let pageTail = boundaryId;
    setRefreshing(true);
    setError(false);
    try {
      do {
        const params = new URLSearchParams({ span_limit: "200" });
        if (attemptId) params.set("attempt_id", attemptId);
        if (next) params.set("span_cursor", next);
        page = await request<RefreshPage<S, A, R>>(
          `/agent-observability/runs/${encodeURIComponent(runId)}?${params}`,
        );
        if (version !== generation.current) return;
        for (const span of page.spans) {
          if (!attemptId || span.attempt_id === attemptId) {
            refreshed.set(span.span_id, span);
            pageTail = span.span_id;
            if (span.span_id === boundaryId) found = true;
          }
        }
        next = page.spans_next_cursor;
        count += 1;
      } while (next && !found && count < MAX_PAGES);
      const otherBackground = Array.from(backgroundRef.current).filter(
        ([, owner]) => owner !== attemptId,
      );
      if (otherBackground.length) {
        const start = backgroundOffset.current % otherBackground.length;
        const selected = [
          ...otherBackground.slice(start),
          ...otherBackground.slice(0, start),
        ].slice(0, MAX_BACKGROUND_QUERIES);
        backgroundOffset.current =
          (start + selected.length) % otherBackground.length;
        const observations = await Promise.allSettled(
          selected.map(async ([id]) => {
            const path = await request<{ items: RefreshSpan[] }>(
              `/agent-observability/runs/${encodeURIComponent(
                runId,
              )}/spans/${encodeURIComponent(id)}/ancestors`,
            );
            return path.items.find((span) => span.span_id === id);
          }),
        );
        if (version !== generation.current) return;
        setBackground((old) => {
          const updated = new Map(old);
          observations.forEach((result) => {
            if (result.status !== "fulfilled" || !result.value) return;
            const span = result.value;
            const subagent = span.subagent as { result_status?: string } | null;
            if (
              !ACTIVE.has(span.status || "") &&
              !ACTIVE.has(subagent?.result_status || "") &&
              TERMINAL.has(subagent?.result_status || span.status || "")
            )
              updated.delete(span.span_id);
          });
          return updated;
        });
      }
      const latestAttempt = [...page!.attempts].sort(
        (a, b) => b.attempt_index - a.attempt_index,
      )[0];
      if (
        latestAttempt &&
        !attempts.some((a) => a.attempt_id === latestAttempt.attempt_id)
      )
        setNewAttempt(latestAttempt);
      latest.current.onRefresh({
        run: page!.run,
        attempts: page!.attempts,
        mergePage: (current) => {
          const merged = new Map(
            current.spans.map((span) => [span.span_id, span]),
          );
          refreshed.forEach((span, id) => merged.set(id, span));
          const unchanged =
            current.cursor === cursor && current.boundaryId === boundaryId;
          return {
            spans: Array.from(merged.values()),
            cursor: unchanged && (found || !next) ? next : current.cursor,
            boundaryId:
              unchanged && (found || !next) ? pageTail : current.boundaryId,
          };
        },
        generatedAt: page!.generated_at,
        refreshedAt: Date.now(),
        incomplete: Boolean(next && !found),
      });
    } catch {
      if (version === generation.current) setError(true);
    } finally {
      release?.();
      if (version === generation.current) {
        busy.current = false;
        setRefreshing(false);
      }
    }
  }, []);
  useEffect(() => {
    generation.current += 1;
    busy.current = false;
    setRefreshing(false);
    setError(false);
    setNewAttempt(null);
    return () => {
      generation.current += 1;
    };
  }, [options.scopeKey, options.runId, options.attemptId]);
  useEffect(() => {
    setBackground(new Map());
  }, [options.scopeKey, options.runId]);
  useEffect(() => {
    setBackground((old) => {
      const next = new Map(old);
      for (const span of options.spans) {
        const subagent = span.subagent as {
          mode?: string;
          result_status?: string;
        } | null;
        if (subagent?.mode === "background" || next.has(span.span_id)) {
          if (hasActiveExecution(null, [span]))
            next.set(span.span_id, span.attempt_id);
          else if (TERMINAL.has(subagent?.result_status || span.status || ""))
            next.delete(span.span_id);
        }
      }
      return next;
    });
  }, [options.scopeKey, options.runId, options.spans]);
  const active =
    hasActiveExecution(options.runStatus, options.spans) || background.size > 0;
  useEffect(() => {
    if (!active || options.enabled === false) return;
    const timer = window.setInterval(() => {
      if (document.visibilityState === "visible") void refresh();
    }, 5000);
    return () => window.clearInterval(timer);
  }, [active, options.enabled, refresh]);
  return {
    refresh,
    refreshing,
    error,
    newAttempt,
    dismissNewAttempt: () => setNewAttempt(null),
  };
}
