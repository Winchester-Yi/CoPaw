import { useEffect, useRef, useState } from "react";
import { Button, Spin } from "antd";
import { Link, useLocation } from "react-router-dom";
import { request } from "../../api/request";
import styles from "./spanRecords.module.less";

type Relation = {
  relation_type: string;
  obs_run_id?: string;
  obs_attempt_id?: string;
  obs_span_id?: string;
  target_unavailable?: boolean;
};
type RecordItem = {
  event_id?: string;
  event_name?: string;
  occurred_at?: string;
  metadata?: { event_type?: string; links?: Relation[] };
  asset_ref?: string;
  asset_type?: string;
  asset_id?: string;
  asset_source?: string;
  asset_version?: string;
  asset_digest?: string;
};
type Page = { items: RecordItem[]; next_cursor: string | null };
type Result = Page & { identity: string };

type Props = {
  runId: string;
  spanId: string;
  sourceKey: string;
  tab: "events" | "assets";
  active: boolean;
};

/** Activate only the selected tab; sourceKey identifies the current authorization context. */
export default function SpanRecords(props: Props) {
  if (props.tab === "assets") return <RecordList {...props} view="events" />;
  return (
    <EventViews
      key={JSON.stringify([props.runId, props.spanId, props.sourceKey])}
      {...props}
    />
  );
}

function EventViews(props: Props) {
  const [view, setView] = useState<"events" | "links">("events");
  const [visitedLinks, setVisitedLinks] = useState(false);
  if (!props.active) return null;
  return (
    <section aria-label="事件与关联">
      <div role="group" aria-label="事件与关联视图">
        <Button
          size="small"
          aria-label="事件"
          aria-pressed={view === "events"}
          type={view === "events" ? "primary" : "default"}
          onClick={() => setView("events")}
        >
          事件
        </Button>{" "}
        <Button
          size="small"
          aria-label="关联"
          aria-pressed={view === "links"}
          type={view === "links" ? "primary" : "default"}
          onClick={() => {
            setVisitedLinks(true);
            setView("links");
          }}
        >
          关联
        </Button>
      </div>
      <div hidden={view !== "events"}>
        <RecordList {...props} view="events" />
      </div>
      {visitedLinks && (
        <div hidden={view !== "links"}>
          <RecordList {...props} view="links" />
        </div>
      )}
    </section>
  );
}

function RecordList({
  runId,
  spanId,
  sourceKey,
  tab,
  active,
  view,
}: Props & { view: "events" | "links" }) {
  const location = useLocation();
  const identity = JSON.stringify([runId, spanId, sourceKey, tab, view]);
  const [result, setResult] = useState<Result | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState(false);
  const [retry, setRetry] = useState(0);
  const generation = useRef(0);
  const base = `/agent-observability/runs/${encodeURIComponent(
    runId,
  )}/${tab}?span_id=${encodeURIComponent(spanId)}${
    tab === "events" && view === "links" ? "&view=links" : ""
  }`;

  useEffect(() => {
    const currentGeneration = generation;
    const version = ++currentGeneration.current;
    setResult(null);
    setError(false);
    setLoading(false);
    if (!active) return;
    setLoading(true);
    request<Page>(base)
      .then((page) => {
        if (generation.current === version) setResult({ ...page, identity });
      })
      .catch(() => {
        if (generation.current === version) setError(true);
      })
      .finally(() => {
        if (generation.current === version) setLoading(false);
      });
    return () => {
      currentGeneration.current++;
    };
  }, [active, base, identity, retry]);

  const more = async () => {
    if (!result?.next_cursor || loading) return;
    const version = generation.current;
    setLoading(true);
    setError(false);
    try {
      const page = await request<Page>(
        `${base}&cursor=${encodeURIComponent(result.next_cursor)}`,
      );
      if (generation.current === version)
        setResult((old) =>
          old?.identity === identity
            ? { ...page, identity, items: [...old.items, ...page.items] }
            : old,
        );
    } catch {
      if (generation.current === version) setError(true);
    } finally {
      if (generation.current === version) setLoading(false);
    }
  };

  if (!active) return null;
  const page = result?.identity === identity ? result : null;
  const label =
    tab === "events" ? (view === "links" ? "关联" : "事件") : "资产";
  const relationUrl = (relation: Relation) => {
    const params = new URLSearchParams();
    if (relation.obs_attempt_id) params.set("attempt", relation.obs_attempt_id);
    if (relation.obs_span_id) params.set("span", relation.obs_span_id);
    if (new URLSearchParams(location.search).get("hideMenu") === "true")
      params.set("hideMenu", "true");
    const query = params.toString();
    return `/agent-observability/runs/${encodeURIComponent(
      relation.obs_run_id || runId,
    )}${query ? `?${query}` : ""}`;
  };
  return (
    <section aria-label={label} className={styles.records}>
      {loading && (
        <p>
          <Spin size="small" /> 正在加载{label}
        </p>
      )}
      {error && (
        <p role="alert">
          {label}加载失败，请重试{" "}
          {page ? (
            <Button size="small" onClick={() => void more()}>
              重试
            </Button>
          ) : (
            <Button size="small" onClick={() => setRetry((value) => value + 1)}>
              重试
            </Button>
          )}
        </p>
      )}
      {page?.items.length === 0 && <p>暂无{label}</p>}
      {page?.items.map((item) =>
        tab === "events" ? (
          <div className={styles.row} key={item.event_id}>
            <div>{item.metadata?.event_type || item.event_name || "事件"}</div>
            {item.occurred_at && <small>{item.occurred_at}</small>}
            {item.metadata?.links?.map((relation, index) => (
              <div key={`${relation.relation_type}-${index}`}>
                {relation.target_unavailable ||
                (!relation.obs_span_id &&
                  !relation.obs_attempt_id &&
                  !relation.obs_run_id) ? (
                  <span>{relation.relation_type} · 目标不可用</span>
                ) : (
                  <Link to={relationUrl(relation)}>
                    {relation.relation_type} · 打开关联 Run
                  </Link>
                )}
              </div>
            ))}
          </div>
        ) : (
          <div className={styles.row} key={item.asset_ref}>
            <div>
              {item.asset_type || "资产"} · {item.asset_id || "未知"}
            </div>
            <small>
              来源：{item.asset_source || "未知"}
              {item.asset_version ? ` · 版本：${item.asset_version}` : ""}
              {item.asset_digest ? ` · 摘要：${item.asset_digest}` : ""}
            </small>
          </div>
        ),
      )}
      {page?.next_cursor && (
        <Button size="small" disabled={loading} onClick={() => void more()}>
          继续加载
        </Button>
      )}
    </section>
  );
}
