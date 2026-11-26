import { useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import { Alert, Button, Input, Spin, Tabs, Tag } from "antd";
import { useLocation, useNavigate } from "react-router-dom";
import { request } from "../../api/request";
import SpanEvidence from "./SpanEvidence";
import { useRunRefresh, type PageState } from "./useRunRefresh";
import SpanRecords from "./SpanRecords";
import styles from "./attempt.module.less";

type Attempt = {
  attempt_id: string;
  attempt_index: number;
  status: string | null;
  started_at: string | null;
  ended_at: string | null;
  duration_us: number | null;
  quality: string | null;
};
const EMPTY_ATTEMPTS: Attempt[] = [];
const spanColors: Record<string, string> = {
  Agent: "#425cc8",
  Loop: "#647796",
  LLM: "#7855cd",
  Tool: "#078477",
  SubAgent: "#2681ba",
  Context: "#aa6715",
  Checkpoint: "#617493",
};
type Span = {
  span_id: string;
  attempt_id: string | null;
  parent_span_id: string | null;
  name: string;
  span_type: string;
  operation: string;
  started_at: string | null;
  ended_at: string | null;
  status: string | null;
  quality: string | null;
  duration_us: number | null;
  [key: string]: unknown;
};
type Page = {
  spans: Span[];
  spans_next_cursor: string | null;
  generated_at?: string;
};
type Ancestors = { items: Span[]; quality: string; next_cursor: string | null };
type SearchPage = { items: Span[]; next_cursor: string | null };
const shown = (v: unknown) => (v == null ? "—" : String(v));
const time = (v: string | null) => (v ? new Date(v).toLocaleString() : "—");
const ms = (v: unknown) =>
  typeof v === "number" ? `${(v / 1000).toLocaleString()} ms` : "—";
const stamp = (v: string | null) => (v ? new Date(v).getTime() : NaN);
const microseconds = (v: string | null) => {
  if (!v) return Number.NEGATIVE_INFINITY;
  const milliseconds = Date.parse(v);
  if (!Number.isFinite(milliseconds)) return Number.NEGATIVE_INFINITY;
  const fraction = v.match(/\.(\d+)(?:Z|[+-]\d{2}:?\d{2})?$/i)?.[1] || "";
  return milliseconds * 1000 + Number(fraction.padEnd(6, "0").slice(3, 6));
};
const fields: [string, string, boolean?][] = [
  ["span_id", "Span ID"],
  ["parent_span_id", "父 Span"],
  ["span_type", "类型"],
  ["operation", "操作"],
  ["started_at", "开始时间"],
  ["ended_at", "结束时间"],
  ["model_key", "模型"],
  ["provider_key", "Provider"],
  ["status", "状态"],
  ["technical_status", "技术状态"],
  ["quality", "质量"],
  ["error_type", "异常类型"],
  ["tool_key", "工具"],
  ["business_subagent_key", "SubAgent"],
  ["request_model_key", "请求模型"],
  ["response_model_key", "实际模型"],
  ["finish_reason", "结束原因"],
  ["duration_us", "耗时", true],
  ["time_to_first_chunk_us", "首有效 chunk", true],
  ["time_to_first_visible_text_us", "首可见文本", true],
  ["input_tokens", "输入 Tokens"],
  ["output_tokens", "输出 Tokens"],
];
const requestFields: [string, string][] = [
  ["request_attempt_index", "请求序号"],
  ["streaming", "流式模式"],
  ["model_key_source", "模型来源"],
];
const modelRequestFields = [
  ...requestFields,
  ...fields.filter(
    ([key]) => key !== "tool_key" && key !== "business_subagent_key",
  ),
];
const memorySpanFields: [string, string, boolean?][] = fields.filter(
  ([key]) =>
    ![
      "model_key",
      "provider_key",
      "tool_key",
      "business_subagent_key",
      "request_model_key",
      "response_model_key",
      "finish_reason",
      "time_to_first_chunk_us",
      "time_to_first_visible_text_us",
      "input_tokens",
      "output_tokens",
    ].includes(key),
);
const compactionStrategies: Record<string, string> = {
  summary: "摘要压缩",
  checkpoint_ready: "应用已就绪的 Checkpoint",
  checkpoint_degraded: "应用降级 Checkpoint",
};

function requestValue(key: string, value: unknown, isTime?: boolean) {
  if (value == null) return "未获取";
  if (key === "streaming" && typeof value === "boolean") {
    return value ? "流式" : "非流式";
  }
  if (key === "model_key_source") {
    if (value === "response") return "Provider 响应";
    if (value === "request_fallback") return "请求模型";
  }
  return isTime ? ms(value) : shown(value);
}

export default function AttemptView({
  runId,
  attempts = EMPTY_ATTEMPTS,
  sourceKey = "",
  exceptionRequest = 0,
  runStatus,
  projectedAt,
  onRunRefresh,
}: {
  runId: string;
  attempts?: Attempt[];
  sourceKey?: string;
  exceptionRequest?: number;
  runStatus: string | null;
  projectedAt: string | null;
  onRunRefresh: (run: unknown, attempts: Attempt[]) => void;
}) {
  const ordered = useMemo(
    () => [...attempts].sort((a, b) => a.attempt_index - b.attempt_index),
    [attempts],
  );
  const attemptIds = JSON.stringify(ordered.map((a) => a.attempt_id).sort());
  const location = useLocation();
  const navigate = useNavigate();
  const params = new URLSearchParams(location.search);
  const linkedAttempt = params.get("attempt");
  const linkedSpan = params.get("span");
  const [selectedAttempt, setSelectedAttempt] = useState(
    () => linkedAttempt || ordered[ordered.length - 1]?.attempt_id,
  );
  const [target, setTarget] = useState<string | null>(linkedSpan);
  const [invalid, setInvalid] = useState(false);
  const [locationError, setLocationError] = useState(false);
  const [locationRetry, setLocationRetry] = useState(0);
  const [copyStatus, setCopyStatus] = useState("");
  const [copyLink, setCopyLink] = useState("");
  const [query, setQuery] = useState("");
  const [searchMode, setSearchMode] = useState<"text" | "errors" | null>(null);
  const [results, setResults] = useState<Span[]>([]);
  const [searchCursor, setSearchCursor] = useState<string | null>(null);
  const [searchError, setSearchError] = useState(false);
  const [searchLoading, setSearchLoading] = useState(false);
  const [pageState, setPageState] = useState<PageState<Span>>({
    spans: [],
    cursor: null,
    boundaryId: null,
  });
  const { spans, cursor, boundaryId } = pageState;
  const setSpans = (update: Span[] | ((old: Span[]) => Span[])) =>
    setPageState((old) => ({
      ...old,
      spans: typeof update === "function" ? update(old.spans) : update,
    }));
  const [observedAt, setObservedAt] = useState<number>(0);
  const [parentStates, setParentStates] = useState<Record<string, string>>({});
  const [refreshedAt, setRefreshedAt] = useState<number | null>(null);
  const [locationVersion, setLocationVersion] = useState(0);
  const [refreshIncomplete, setRefreshIncomplete] = useState(false);
  const [loading, setLoading] = useState(Boolean(selectedAttempt));
  const [initialLoading, setInitialLoading] = useState(
    Boolean(selectedAttempt),
  );
  const [retry, setRetry] = useState(0);
  const [error, setError] = useState(false);
  const [selected, setSelected] = useState<string | null>(null);
  const [collapsed, setCollapsed] = useState<Set<string>>(new Set());
  const [tab, setTab] = useState("details");
  const [narrow, setNarrow] = useState(false);
  const root = useRef<HTMLDivElement>(null);
  const scrollAnchor = useRef<{ id: string; top: number } | null>(null);
  const resolvedLocation = useRef<string | null>(null);
  const generation = useRef(0);
  const pageRequest = useRef<symbol | null>(null);
  const acquirePageRequest = () => {
    if (pageRequest.current) return null;
    const token = Symbol();
    pageRequest.current = token;
    return () => {
      if (pageRequest.current === token) pageRequest.current = null;
    };
  };
  const searchVersion = useRef(0);
  const searchSpec = useRef<{
    mode: "text" | "errors";
    query: string;
    attempt?: string;
  } | null>(null);
  const attempt = ordered.find((a) => a.attempt_id === selectedAttempt);
  const updateLink = (attemptId: string | null, spanId: string | null) => {
    const next = new URLSearchParams(location.search);
    if (attemptId) next.set("attempt", attemptId);
    else next.delete("attempt");
    if (spanId) next.set("span", spanId);
    else next.delete("span");
    navigate(
      { pathname: location.pathname, search: next.toString() },
      { replace: true },
    );
  };
  const search = async (mode: "text" | "errors", nextCursor?: string) => {
    const spec = nextCursor
      ? searchSpec.current
      : {
          mode,
          query: query.trim(),
          attempt: mode === "text" ? selectedAttempt : undefined,
        };
    if (!spec || (spec.mode === "text" && (!spec.query || !spec.attempt)))
      return;
    if (!nextCursor) {
      searchSpec.current = spec;
      setResults([]);
      setSearchCursor(null);
    }
    const version = ++searchVersion.current;
    const searchParams = new URLSearchParams({ limit: "100" });
    if (spec.mode === "text") {
      searchParams.set("q", spec.query);
      searchParams.set("attempt_id", spec.attempt!);
    } else searchParams.set("errors_only", "true");
    if (nextCursor) searchParams.set("cursor", nextCursor);
    setSearchMode(spec.mode);
    setSearchLoading(true);
    setSearchError(false);
    try {
      const page = await request<SearchPage>(
        `/agent-observability/runs/${encodeURIComponent(
          runId,
        )}/spans/search?${searchParams}`,
      );
      if (version !== searchVersion.current) return;
      setResults((old) => (nextCursor ? [...old, ...page.items] : page.items));
      setSearchCursor(page.next_cursor);
    } catch {
      if (version === searchVersion.current) setSearchError(true);
    } finally {
      if (version === searchVersion.current) setSearchLoading(false);
    }
  };
  useEffect(() => {
    ++searchVersion.current;
    searchSpec.current = null;
    setSearchMode(null);
    setResults([]);
    setSearchCursor(null);
    setSearchLoading(false);
    setSearchError(false);
  }, [runId, selectedAttempt]);
  useEffect(() => {
    setInvalid(false);
    setLocationError(false);
    setTarget(linkedSpan);
    setSelected((old) => (old === linkedSpan ? old : null));
  }, [linkedAttempt, linkedSpan]);
  useEffect(() => {
    if (
      linkedAttempt &&
      (JSON.parse(attemptIds) as string[]).includes(linkedAttempt)
    )
      setSelectedAttempt(linkedAttempt);
    else if (!linkedAttempt && !linkedSpan)
      setSelectedAttempt(
        (old) => old || ordered[ordered.length - 1]?.attempt_id,
      );
    // Metadata changes do not alter the linked location.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [linkedAttempt, linkedSpan, attemptIds]);
  useEffect(() => {
    if (exceptionRequest) void search("errors");
    // The counter represents an explicit request; changing the search form must not repeat it.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [exceptionRequest]);
  useEffect(() => {
    if (
      linkedAttempt &&
      !(JSON.parse(attemptIds) as string[]).includes(linkedAttempt)
    )
      setInvalid(true);
  }, [linkedAttempt, attemptIds]);
  useEffect(() => {
    resolvedLocation.current = null;
  }, [runId, target, locationRetry]);
  useEffect(() => {
    const locationKey = JSON.stringify([
      runId,
      linkedAttempt,
      target,
      locationRetry,
    ]);
    if (
      !target ||
      loading ||
      resolvedLocation.current === locationKey ||
      (linkedAttempt && selectedAttempt !== linkedAttempt)
    )
      return;
    let active = true;
    request<Ancestors>(
      `/agent-observability/runs/${encodeURIComponent(
        runId,
      )}/spans/${encodeURIComponent(target)}/ancestors`,
    )
      .then((path) => {
        if (!active) return;
        const item = path.items.find((s) => s.span_id === target);
        if (
          !item?.attempt_id ||
          !(JSON.parse(attemptIds) as string[]).includes(item.attempt_id) ||
          (linkedAttempt && linkedAttempt !== item.attempt_id)
        ) {
          setInvalid(true);
          return;
        }
        setInvalid(false);
        setLocationError(false);
        if (selectedAttempt !== item.attempt_id) {
          setSelectedAttempt(item.attempt_id);
          return;
        }
        resolvedLocation.current = locationKey;
        setSpans((old) => {
          const existing = new Set(old.map((s) => s.span_id));
          const missing = path.items.filter(
            (s) => s.attempt_id === item.attempt_id && !existing.has(s.span_id),
          );
          return missing.length ? [...old, ...missing] : old;
        });
        setCollapsed((old) => {
          const next = new Set(old);
          path.items.forEach((span) => next.delete(span.span_id));
          return next;
        });
        setSelected(target);
        setLocationVersion((version) => version + 1);
        if (path.quality !== "complete")
          setParentStates((old) => ({
            ...old,
            [path.items[path.items.length - 1].span_id]:
              path.quality === "parent_missing"
                ? "祖先节点缺失"
                : path.quality === "depth_limited"
                ? "祖先路径深度受限"
                : "祖先路径存在循环",
          }));
      })
      .catch((err: Error & { status?: number }) => {
        if (!active) return;
        if (err.status === 400 || err.status === 404) setInvalid(true);
        else setLocationError(true);
      });
    return () => {
      active = false;
    };
  }, [
    target,
    runId,
    attemptIds,
    linkedAttempt,
    selectedAttempt,
    loading,
    locationRetry,
  ]);
  useEffect(() => {
    if (!selected || selected !== target) return;
    const node = Array.from(
      root.current?.querySelectorAll<HTMLElement>("[data-span-id]") || [],
    ).find((el) => el.dataset.spanId === selected);
    node?.scrollIntoView?.({ block: "nearest" });
    // Preserve the reader's scroll position on later refreshes.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [locationVersion]);
  useEffect(() => {
    const el = root.current;
    if (!el || typeof ResizeObserver === "undefined") return;
    const observer = new ResizeObserver(([entry]) =>
      setNarrow(entry.contentRect.width < 900),
    );
    observer.observe(el);
    return () => observer.disconnect();
  }, []);
  useEffect(() => {
    const version = ++generation.current;
    pageRequest.current = null;
    setPageState({ spans: [], cursor: null, boundaryId: null });
    setParentStates({});
    setObservedAt(0);
    setSelected(null);
    setCollapsed(new Set());
    setError(false);
    if (!selectedAttempt) {
      setLoading(false);
      setInitialLoading(false);
      return;
    }
    setInitialLoading(true);
    setLoading(true);
    request<Page>(
      `/agent-observability/runs/${encodeURIComponent(
        runId,
      )}?attempt_id=${encodeURIComponent(selectedAttempt)}&span_limit=200`,
    )
      .then((page) => {
        if (generation.current === version) {
          const items = (page.spans || []).filter(
            (s) => s.attempt_id === selectedAttempt,
          );
          setPageState({
            spans: items,
            cursor: page.spans_next_cursor,
            boundaryId: items[items.length - 1]?.span_id || null,
          });
          setObservedAt(stamp(page.generated_at || null) || Date.now());
        }
      })
      .catch(() => {
        if (generation.current === version) setError(true);
      })
      .finally(() => {
        if (generation.current === version) {
          setLoading(false);
          setInitialLoading(false);
        }
      });
    return () => {
      if (generation.current === version) generation.current = version + 1;
    };
  }, [runId, selectedAttempt, retry]);
  const more = () => {
    if (!cursor || !selectedAttempt || loading) return;
    const release = acquirePageRequest();
    if (!release) return;
    const version = generation.current;
    setLoading(true);
    setError(false);
    request<Page>(
      `/agent-observability/runs/${encodeURIComponent(
        runId,
      )}?attempt_id=${encodeURIComponent(
        selectedAttempt,
      )}&span_limit=200&span_cursor=${encodeURIComponent(cursor)}`,
    )
      .then((page) => {
        if (generation.current === version) {
          const items = (page.spans || []).filter(
            (s) => s.attempt_id === selectedAttempt,
          );
          setPageState((old) => ({
            spans: Array.from(
              new Map(
                [...old.spans, ...items].map((s) => [s.span_id, s]),
              ).values(),
            ),
            cursor: page.spans_next_cursor,
            boundaryId: items[items.length - 1]?.span_id || old.boundaryId,
          }));
          setObservedAt(stamp(page.generated_at || null) || Date.now());
        }
      })
      .catch(() => {
        if (generation.current === version) setError(true);
      })
      .finally(() => {
        release();
        if (generation.current === version) setLoading(false);
      });
  };
  const refresh = useRunRefresh<Span, Attempt, unknown>({
    scopeKey: sourceKey,
    runId,
    attemptId: selectedAttempt,
    runStatus,
    enabled: !initialLoading,
    acquire: acquirePageRequest,
    spans,
    cursor,
    boundaryId,
    attempts,
    onRefresh: (result) => {
      const container =
        root.current?.querySelector<HTMLElement>('[role="tree"]');
      const top = container?.getBoundingClientRect().top ?? 0;
      const first = Array.from(
        container?.querySelectorAll<HTMLElement>("[data-span-id]") || [],
      ).find((row) => row.getBoundingClientRect().bottom > top);
      if (first?.dataset.spanId)
        scrollAnchor.current = {
          id: first.dataset.spanId,
          top: first.getBoundingClientRect().top,
        };
      setPageState(result.mergePage);
      setRefreshedAt(result.refreshedAt);
      setRefreshIncomplete(result.incomplete);
      setObservedAt(stamp(result.generatedAt) || result.refreshedAt);
      onRunRefresh(result.run, result.attempts);
    },
  });
  useLayoutEffect(() => {
    const anchor = scrollAnchor.current;
    if (!anchor) return;
    scrollAnchor.current = null;
    const container = root.current?.querySelector<HTMLElement>('[role="tree"]');
    const row = Array.from(
      container?.querySelectorAll<HTMLElement>("[data-span-id]") || [],
    ).find((item) => item.dataset.spanId === anchor.id);
    if (container && row)
      container.scrollTop += row.getBoundingClientRect().top - anchor.top;
  }, [spans]);
  const spanIds = new Set(spans.map((span) => span.span_id));
  const orphanKey = JSON.stringify(
    spans
      .filter(
        (span) => span.parent_span_id && !spanIds.has(span.parent_span_id),
      )
      .map(({ span_id, parent_span_id }) => ({ span_id, parent_span_id })),
  );
  useEffect(() => {
    let active = true;
    const orphans = JSON.parse(orphanKey) as Pick<
      Span,
      "span_id" | "parent_span_id"
    >[];
    setParentStates({});
    orphans.forEach((span) => {
      request<Ancestors>(
        `/agent-observability/runs/${encodeURIComponent(
          runId,
        )}/spans/${encodeURIComponent(span.span_id)}/ancestors`,
      )
        .then((result) => {
          if (!active) return;
          const parentFound = result.items.some(
            (item) => item.span_id === span.parent_span_id,
          );
          const parentState = parentFound
            ? "父节点未加载"
            : result.quality === "parent_missing"
            ? "父节点缺失"
            : "父节点状态未知";
          const pathState =
            result.quality === "parent_missing" && parentFound
              ? "祖先节点缺失"
              : result.quality === "depth_limited"
              ? "祖先路径深度受限"
              : result.quality === "cycle"
              ? "祖先路径存在循环"
              : "";
          const state = [parentState, pathState].filter(Boolean).join(" · ");
          setParentStates((old) => ({ ...old, [span.span_id]: state }));
        })
        .catch(() => {
          if (active)
            setParentStates((old) => ({
              ...old,
              [span.span_id]: "父节点状态未知",
            }));
        });
    });
    return () => {
      active = false;
    };
  }, [runId, orphanKey]);
  const rows = useMemo(() => {
    const byId = new Map(spans.map((s) => [s.span_id, s]));
    const children = new Map<string | null, Span[]>();
    spans.forEach((s) => {
      const parent =
        s.parent_span_id && byId.has(s.parent_span_id)
          ? s.parent_span_id
          : null;
      children.set(parent, [...(children.get(parent) || []), s]);
    });
    children.forEach((items) =>
      items.sort(
        (a, b) =>
          microseconds(a.started_at) - microseconds(b.started_at) ||
          a.span_id.localeCompare(b.span_id),
      ),
    );
    const result: {
      span: Span;
      depth: number;
      hasChildren: boolean;
      orphan: boolean;
      cycle: boolean;
    }[] = [];
    const seen = new Set<string>();
    const cycleRoots = new Set<string>();
    const inspected = new Set<string>();
    spans.forEach((span) => {
      const path: string[] = [];
      const positions = new Map<string, number>();
      let id: string | null = span.span_id;
      while (id && byId.has(id) && !inspected.has(id)) {
        const position = positions.get(id);
        if (position !== undefined) {
          cycleRoots.add(path.slice(position).sort()[0]);
          break;
        }
        positions.set(id, path.length);
        path.push(id);
        id = byId.get(id)?.parent_span_id || null;
      }
      path.forEach((item) => inspected.add(item));
    });
    const visit = (s: Span, depth: number) => {
      if (seen.has(s.span_id)) return;
      seen.add(s.span_id);
      const kids = (children.get(s.span_id) || []).filter(
        (child) => !seen.has(child.span_id),
      );
      result.push({
        span: s,
        depth,
        hasChildren: kids.length > 0,
        orphan: !!s.parent_span_id && !byId.has(s.parent_span_id),
        cycle: cycleRoots.has(s.span_id),
      });
      if (!collapsed.has(s.span_id)) kids.forEach((c) => visit(c, depth + 1));
    };
    (children.get(null) || []).forEach((s) => visit(s, 0));
    cycleRoots.forEach((id) => visit(byId.get(id)!, 0));
    return result;
  }, [spans, collapsed]);
  const origin = stamp(attempt?.started_at || null);
  const end = Math.max(
    Number.isFinite(origin) ? origin + 1 : 1,
    ...spans.map((s) => stamp(s.ended_at) || stamp(s.started_at) || 0),
    ...(spans.some(
      (s) =>
        !s.ended_at &&
        s.status === "running" &&
        s.quality != null &&
        s.quality !== "unknown",
    )
      ? [observedAt]
      : []),
  );
  const selectedSpan = spans.find((s) => s.span_id === selected);
  const select = (id: string) => {
    setSelected(id);
    setTarget(null);
    updateLink(selectedAttempt || null, id);
  };
  const isModelRequest =
    selectedSpan?.span_type === "LLM" &&
    selectedSpan.operation === "request_attempt";
  const isCompaction =
    selectedSpan?.span_type === "Context" &&
    selectedSpan.operation === "compact";
  const isCheckpoint = selectedSpan?.span_type === "Checkpoint";
  const isMemorySpan = isCompaction || isCheckpoint;
  const detailFields = isModelRequest
    ? modelRequestFields
    : isMemorySpan
    ? memorySpanFields
    : fields;
  const inspector = selectedSpan ? (
    <>
      <h3>{selectedSpan.name}</h3>
      <Tabs
        activeKey={tab}
        onChange={setTab}
        items={[
          { key: "details", label: "详情" },
          { key: "evidence", label: "正文证据" },
          { key: "events", label: "事件与关联" },
          { key: "assets", label: "资产" },
        ]}
      />
      {tab === "details" ? (
        <dl>
          {detailFields.map(([key, label, isTime]) => (
            <div key={key}>
              <dt>{label}</dt>
              <dd>
                {isModelRequest
                  ? requestValue(key, selectedSpan[key], isTime)
                  : isTime
                  ? ms(selectedSpan[key])
                  : shown(selectedSpan[key])}
              </dd>
            </div>
          ))}
          {isCompaction && (
            <>
              <div>
                <dt>压缩策略</dt>
                <dd>
                  {typeof selectedSpan.compaction_strategy === "string" &&
                  Object.prototype.hasOwnProperty.call(
                    compactionStrategies,
                    selectedSpan.compaction_strategy,
                  )
                    ? compactionStrategies[selectedSpan.compaction_strategy]
                    : shown(selectedSpan.compaction_strategy)}
                </dd>
              </div>
              <div>
                <dt>压缩前 Tokens（估算）</dt>
                <dd>{shown(selectedSpan.context_before_tokens)}</dd>
              </div>
              <div>
                <dt>压缩后 Tokens（估算）</dt>
                <dd>{shown(selectedSpan.context_after_tokens)}</dd>
              </div>
            </>
          )}
          {isCheckpoint && (
            <>
              <div>
                <dt>Checkpoint 动作</dt>
                <dd>
                  {selectedSpan.operation === "save"
                    ? "保存"
                    : selectedSpan.operation === "restore"
                    ? "恢复"
                    : shown(selectedSpan.operation)}
                </dd>
              </div>
              <div>
                <dt>Checkpoint ID</dt>
                <dd>{shown(selectedSpan.checkpoint_id)}</dd>
              </div>
            </>
          )}
          {selectedSpan.subagent &&
            typeof selectedSpan.subagent === "object" &&
            Object.entries(
              selectedSpan.subagent as Record<string, unknown>,
            ).map(([key, value]) => (
              <div key={key}>
                <dt>
                  {(
                    {
                      business_subagent_key: "SubAgent",
                      mode: "模式",
                      execution_agent_key: "执行 Agent",
                      result_status: "结果状态",
                    } as Record<string, string>
                  )[key] || key}
                </dt>
                <dd>{shown(value)}</dd>
              </div>
            ))}
          {!isMemorySpan && (
            <div>
              <dt>Tokens 合计</dt>
              <dd>
                {typeof selectedSpan.input_tokens === "number" &&
                typeof selectedSpan.output_tokens === "number"
                  ? selectedSpan.input_tokens + selectedSpan.output_tokens
                  : isModelRequest
                  ? "未获取"
                  : "—"}
              </dd>
            </div>
          )}
        </dl>
      ) : tab === "evidence" ? (
        <SpanEvidence
          runId={runId}
          spanId={selectedSpan.span_id}
          sourceKey={sourceKey}
          active
        />
      ) : (
        <SpanRecords
          runId={runId}
          spanId={selectedSpan.span_id}
          sourceKey={sourceKey}
          tab={tab === "assets" ? "assets" : "events"}
          active
        />
      )}
    </>
  ) : (
    <>
      <h3>Attempt 概览</h3>
      <dl>
        <div>
          <dt>状态</dt>
          <dd>{shown(attempt?.status)}</dd>
        </div>
        <div>
          <dt>开始</dt>
          <dd>{time(attempt?.started_at || null)}</dd>
        </div>
        <div>
          <dt>结束</dt>
          <dd>{time(attempt?.ended_at || null)}</dd>
        </div>
        <div>
          <dt>耗时</dt>
          <dd>{ms(attempt?.duration_us)}</dd>
        </div>
        <div>
          <dt>质量</dt>
          <dd>{shown(attempt?.quality)}</dd>
        </div>
      </dl>
    </>
  );
  return (
    <div className={styles.root} ref={root}>
      <div className={styles.toolbar}>
        <Button
          onClick={() => void refresh.refresh()}
          loading={refresh.refreshing}
          disabled={loading}
        >
          刷新
        </Button>
        <span>
          刷新时间：{refreshedAt ? new Date(refreshedAt).toLocaleString() : "—"}
        </span>
        <span>投影时间：{time(projectedAt)}</span>
      </div>
      {refresh.error && <Alert type="error" message="刷新失败，请重试" />}
      {refreshIncomplete && (
        <Alert type="warning" message="数据较多，部分已加载页面未刷新" />
      )}
      {refresh.newAttempt && (
        <Alert
          type="info"
          message={`发现新的 Attempt ${refresh.newAttempt.attempt_index}`}
          action={
            <Button
              onClick={() => {
                const id = refresh.newAttempt!.attempt_id;
                setTarget(null);
                setInvalid(false);
                setSelectedAttempt(id);
                updateLink(id, null);
                refresh.dismissNewAttempt();
              }}
            >
              切换查看
            </Button>
          }
        />
      )}
      {invalid && (
        <Alert
          type="warning"
          message="目标 Attempt 或 Span 已失效"
          action={
            <Button
              onClick={() => {
                setInvalid(false);
                setTarget(null);
                const latest = ordered[ordered.length - 1]?.attempt_id;
                setSelectedAttempt(latest);
                updateLink(latest || null, null);
              }}
            >
              {ordered.length ? "回到最近 Attempt" : "清除失效定位"}
            </Button>
          }
        />
      )}
      {locationError && (
        <Alert
          type="error"
          message="定位暂不可用"
          action={
            <Button
              onClick={() => {
                setLocationError(false);
                setLocationRetry((n) => n + 1);
              }}
            >
              重试定位
            </Button>
          }
        />
      )}
      {ordered.length === 0 ? (
        <p>尚无已观测的执行片段</p>
      ) : (
        <>
          <nav className={styles.attempts} aria-label="执行片段">
            {ordered.map((a) => (
              <Button
                key={a.attempt_id}
                type={a.attempt_id === selectedAttempt ? "primary" : "default"}
                onClick={() => {
                  setTarget(null);
                  setInvalid(false);
                  setSelectedAttempt(a.attempt_id);
                  updateLink(a.attempt_id, null);
                }}
              >
                Attempt {a.attempt_index} · {shown(a.status)} ·{" "}
                {time(a.started_at)} — {time(a.ended_at)}
              </Button>
            ))}
          </nav>
          <div className={styles.toolbar}>
            <Input
              aria-label="搜索当前 Attempt"
              value={query}
              onChange={(e) => setQuery(e.target.value)}
              onPressEnter={() => void search("text")}
              placeholder="搜索名称、操作、工具或模型"
            />
            <Button onClick={() => void search("text")} loading={searchLoading}>
              搜索
            </Button>
            <Button
              onClick={() => void search("errors")}
              loading={searchLoading}
            >
              定位异常
            </Button>
            <Button
              onClick={async () => {
                const url = new URL(window.location.href);
                const share = new URLSearchParams(location.search);
                if (selectedAttempt) share.set("attempt", selectedAttempt);
                if (selected) share.set("span", selected);
                else share.delete("span");
                url.search = share.toString();
                setCopyLink(url.toString());
                try {
                  if (!navigator.clipboard?.writeText) throw new Error();
                  await navigator.clipboard.writeText(url.toString());
                  setCopyStatus("链接已复制");
                } catch {
                  const input = document.createElement("textarea");
                  input.value = url.toString();
                  input.style.position = "fixed";
                  input.style.left = "-9999px";
                  document.body.appendChild(input);
                  input.select();
                  let copied = false;
                  try {
                    copied = document.execCommand("copy");
                  } catch {
                    /* report below */
                  }
                  input.remove();
                  setCopyStatus(
                    copied ? "链接已复制" : "复制失败，请手动复制下方链接",
                  );
                }
              }}
            >
              复制分享链接
            </Button>
          </div>
          {copyStatus && <div role="status">{copyStatus}</div>}
          {copyStatus.startsWith("复制失败") && (
            <Input
              aria-label="分享链接"
              readOnly
              value={copyLink}
              onFocus={(e) => e.currentTarget.select()}
            />
          )}
          {searchError && <Alert type="error" message="搜索暂不可用，请重试" />}
          {searchMode && !searchError && (
            <div className={styles.results} aria-label="搜索结果">
              {!results.length && !searchLoading && (
                <span>没有匹配的 Span</span>
              )}
              {results.map((item) => (
                <Button
                  key={item.span_id}
                  onClick={() => {
                    setInvalid(false);
                    setTarget(item.span_id);
                    if (item.attempt_id) setSelectedAttempt(item.attempt_id);
                    updateLink(item.attempt_id, item.span_id);
                  }}
                >
                  {item.name} · {item.attempt_id}
                </Button>
              ))}
              {searchCursor && (
                <Button
                  loading={searchLoading}
                  onClick={() => void search(searchMode, searchCursor)}
                >
                  更多结果
                </Button>
              )}
            </div>
          )}
          <div className={`${styles.layout} ${narrow ? styles.narrow : ""}`}>
            <div className={styles.timeline}>
              <div className={styles.scroll} role="tree" aria-label="执行树">
                <div className={styles.header} role="presentation">
                  <strong>Span 树</strong>
                  <div
                    className={styles.axis}
                    aria-label="时间轴（相对 Attempt 起点）"
                  >
                    {[0, 0.25, 0.5, 0.75, 1].map((fraction) => (
                      <span key={fraction}>
                        {Number.isFinite(origin)
                          ? ms((end - origin) * fraction * 1000)
                          : "—"}
                      </span>
                    ))}
                  </div>
                </div>
                {rows.map(
                  ({ span, depth, hasChildren, orphan, cycle }, index) => {
                    const start = stamp(span.started_at);
                    const finish = stamp(span.ended_at);
                    const left =
                      Number.isFinite(origin) && Number.isFinite(start)
                        ? Math.max(0, ((start - origin) / (end - origin)) * 100)
                        : 0;
                    const running =
                      !span.ended_at &&
                      span.status === "running" &&
                      span.quality != null &&
                      span.quality !== "unknown";
                    const barEnd = Number.isFinite(finish)
                      ? finish
                      : running
                      ? observedAt
                      : NaN;
                    const width =
                      Number.isFinite(barEnd) &&
                      Number.isFinite(start) &&
                      Number.isFinite(origin)
                        ? Math.max(1, ((barEnd - start) / (end - origin)) * 100)
                        : 1;
                    return (
                      <div
                        className={`${styles.row} ${
                          selected === span.span_id ? styles.selected : ""
                        }`}
                        key={span.span_id}
                        data-span-id={span.span_id}
                        role="treeitem"
                        aria-level={depth + 1}
                        aria-expanded={
                          hasChildren ? !collapsed.has(span.span_id) : undefined
                        }
                        aria-selected={selected === span.span_id}
                        tabIndex={0}
                        onClick={() => select(span.span_id)}
                        onKeyDown={(e) => {
                          if (e.key === "Enter" || e.key === " ") {
                            e.preventDefault();
                            select(span.span_id);
                          } else if (
                            e.key === "ArrowDown" ||
                            e.key === "ArrowUp"
                          ) {
                            e.preventDefault();
                            const nodes =
                              e.currentTarget.parentElement?.querySelectorAll<HTMLElement>(
                                '[role="treeitem"]',
                              );
                            nodes?.[
                              Math.max(
                                0,
                                Math.min(
                                  rows.length - 1,
                                  index + (e.key === "ArrowDown" ? 1 : -1),
                                ),
                              )
                            ]?.focus();
                          } else if (
                            hasChildren &&
                            (e.key === "ArrowLeft" || e.key === "ArrowRight")
                          ) {
                            e.preventDefault();
                            setCollapsed((old) => {
                              const next = new Set(old);
                              if (e.key === "ArrowLeft") next.add(span.span_id);
                              else next.delete(span.span_id);
                              return next;
                            });
                          }
                        }}
                      >
                        <div
                          className={styles.name}
                          style={{ paddingLeft: 8 + depth * 18 }}
                        >
                          <button
                            type="button"
                            className={styles.toggle}
                            aria-label={`${
                              collapsed.has(span.span_id) ? "展开" : "收起"
                            } ${span.name}`}
                            disabled={!hasChildren}
                            onKeyDown={(e) => e.stopPropagation()}
                            onClick={(e) => {
                              e.stopPropagation();
                              setCollapsed((old) => {
                                const next = new Set(old);
                                if (next.has(span.span_id))
                                  next.delete(span.span_id);
                                else next.add(span.span_id);
                                return next;
                              });
                            }}
                          >
                            {hasChildren
                              ? collapsed.has(span.span_id)
                                ? "▸"
                                : "▾"
                              : "·"}
                          </button>
                          <span title={span.name}>{span.name}</span>{" "}
                          {cycle && <Tag>父链存在循环</Tag>}
                          {orphan && (
                            <Tag>
                              {parentStates[span.span_id] || "父节点状态查询中"}
                            </Tag>
                          )}
                        </div>
                        <div className={styles.track}>
                          <span
                            className={styles.bar}
                            style={{
                              left: `${left}%`,
                              width: `${Math.min(
                                width,
                                Math.max(0, 100 - left),
                              )}%`,
                              backgroundColor: ["error", "failed"].includes(
                                (span.status || "").toLowerCase(),
                              )
                                ? "#e04d6b"
                                : spanColors[
                                    Object.keys(spanColors).find(
                                      (key) =>
                                        key.toLowerCase() ===
                                        span.span_type.toLowerCase(),
                                    ) || ""
                                  ] || "#647796",
                            }}
                          />
                          <span className={styles.barLabel}>
                            {span.ended_at
                              ? ms(span.duration_us)
                              : running
                              ? "进行中"
                              : "结束时间未知"}
                          </span>
                        </div>
                      </div>
                    );
                  },
                )}
              </div>
              <div className={styles.legend} aria-label="Span 类型图例">
                {Object.entries(spanColors).map(([label, color]) => (
                  <span key={label}>
                    <i style={{ backgroundColor: color }} />
                    {label}
                  </span>
                ))}
                <span>
                  <i style={{ backgroundColor: "#e04d6b" }} />
                  异常
                </span>
              </div>
              {!loading && !spans.length && <p>此片段暂无 Span</p>}
              {error && (
                <Alert
                  type="error"
                  message="加载 Span 失败"
                  action={
                    <Button
                      onClick={cursor ? more : () => setRetry((n) => n + 1)}
                    >
                      重试
                    </Button>
                  }
                />
              )}
              {loading && <Spin tip="正在加载 Span" />}
              {cursor && (
                <Button
                  onClick={more}
                  loading={loading}
                  disabled={refresh.refreshing}
                >
                  加载更多
                </Button>
              )}
            </div>
            <aside className={styles.inspector} aria-label="节点详情">
              {inspector}
            </aside>
          </div>
        </>
      )}
    </div>
  );
}
