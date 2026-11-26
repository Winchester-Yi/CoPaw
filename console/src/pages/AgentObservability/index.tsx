import { useEffect, useRef, useState } from "react";
import { Alert, Button, Input, Select, Spin, Table, Tag } from "antd";
import { Link, useLocation, useNavigate, useParams } from "react-router-dom";
import { request } from "../../api/request";
import { useIframeStore } from "../../stores/iframeStore";
import { useAgentStore } from "../../stores/agentStore";
import { DEFAULT_SOURCE_ID } from "../../constants/identity";
import styles from "./index.module.less";
import AttemptView from "./AttemptView";
import RunOverview from "./RunOverview";

type Run = {
  run_id: string;
  agent_key: string | null;
  started_at: string | null;
  status: string | null;
  quality: string | null;
  duration_us: number | null;
  llm_calls: number | null;
  request_attempt_count?: number | null;
  tool_calls: number | null;
  input_tokens: number | null;
  output_tokens: number | null;
  projected_at?: string | null;
};
type RunList = {
  items: Run[];
  next_cursor: string | null;
  window_start?: string;
  window_end?: string;
};
type Detail = {
  run: Run;
  attempts?: React.ComponentProps<typeof AttemptView>["attempts"];
};
type Capabilities = { capture_enabled: boolean };
const value = (v: string | number | null | undefined) => v ?? "—";
const duration = (v: number | null | undefined) =>
  v == null ? "—" : `${(v / 1000).toLocaleString()} ms`;
const tokens = (run: Run) =>
  run.input_tokens == null || run.output_tokens == null
    ? "—"
    : (run.input_tokens + run.output_tokens).toLocaleString();
const calls = (run: Run) =>
  `模型 ${value(run.llm_calls)} / 工具 ${value(run.tool_calls)}`;
const observedTokens = (count: number | null) =>
  count == null ? "" : `已观测 ${count.toLocaleString()}`;
const date = (v: string | null) => (v ? new Date(v).toLocaleString() : "—");
const statuses = [
  "accepted",
  "running",
  "waiting_approval",
  "succeeded",
  "failed",
  "cancelled",
  "timed_out",
  "rejected",
];
let returnPosition: {
  scope: string;
  search: string;
  runId: string;
  scrollY: number;
} | null = null;

export default function AgentObservability() {
  const source = useIframeStore((s) => s.source);
  const userId = useIframeStore((s) => s.userId);
  const manager = useIframeStore((s) => s.manager);
  const admin = useIframeStore((s) => s.isSuperManager);
  const selectedAgent = useAgentStore((s) => s.selectedAgent);
  const location = useLocation();
  const navigate = useNavigate();
  const scope = JSON.stringify([source, userId, manager, admin, selectedAgent]);
  const previousScope = useRef(scope);
  useEffect(() => {
    if (previousScope.current !== scope) {
      previousScope.current = scope;
      returnPosition = null;
      const params = new URLSearchParams(location.search);
      params.delete("cursor");
      params.delete("previous");
      params.delete("window_start");
      params.delete("window_end");
      navigate(
        { pathname: location.pathname, search: params.toString() },
        { replace: true },
      );
    }
  }, [scope, location.pathname, location.search, navigate]);
  if (previousScope.current !== scope) return null;
  return (
    <ObservabilityView
      key={scope}
      scope={scope}
      allowed={manager || admin}
      source={source || DEFAULT_SOURCE_ID}
    />
  );
}

function ObservabilityView({
  allowed,
  source,
  scope,
}: {
  allowed: boolean;
  source: string;
  scope: string;
}) {
  const [exceptionRequest, setExceptionRequest] = useState(0);
  const { runId } = useParams();
  const location = useLocation();
  const navigate = useNavigate();
  const pageRef = useRef<HTMLElement>(null);
  const params = new URLSearchParams(location.search);
  const [draft, setDraft] = useState(() => ({
    idType: params.get("idType") || "run_id",
    id: params.get("id") || "",
    start: params.get("start") || "",
    end: params.get("end") || "",
    agent_id: params.get("agent_id") || "",
    status: params.get("status") || "",
    min_duration_ms: params.get("min_duration_ms") || "",
    sort_by: params.get("sort_by") || "started_at",
  }));
  useEffect(() => {
    const p = new URLSearchParams(location.search);
    setDraft({
      idType: p.get("idType") || "run_id",
      id: p.get("id") || "",
      start: p.get("start") || "",
      end: p.get("end") || "",
      agent_id: p.get("agent_id") || "",
      status: p.get("status") || "",
      min_duration_ms: p.get("min_duration_ms") || "",
      sort_by: p.get("sort_by") || "started_at",
    });
  }, [location.search]);
  const embedded =
    new URLSearchParams(location.search).get("hideMenu") === "true"
      ? "?hideMenu=true"
      : "";
  const [list, setList] = useState<RunList | null>(null);
  const [detail, setDetail] = useState<Detail | null>(null);
  const [capture, setCapture] = useState<boolean | null>(null);
  const [error, setError] = useState<
    "missing" | "invalid" | "forbidden" | "unavailable" | "range" | null
  >(null);
  const [retry, setRetry] = useState(0);
  const fetchSearch = runId ? "" : location.search;
  useEffect(() => {
    if (!allowed) return;
    let active = true;
    setList(null);
    setDetail(null);
    setError(null);
    setCapture(null);
    request<Capabilities>("/agent-observability/capabilities")
      .then((v) => {
        if (active) setCapture(v.capture_enabled);
      })
      .catch(() => {});
    const query = new URLSearchParams(fetchSearch);
    const apiQuery = new URLSearchParams({
      limit: "50",
      sort_by: query.get("sort_by") || "started_at",
    });
    for (const key of [
      "start",
      "end",
      "agent_id",
      "status",
      "min_duration_ms",
      "cursor",
    ]) {
      const v =
        query.get(key) ||
        (key === "start"
          ? query.get("window_start")
          : key === "end"
          ? query.get("window_end")
          : null);
      if (
        v &&
        (key === "start" || key === "end") &&
        Number.isNaN(new Date(v).getTime())
      ) {
        setError("invalid");
        return () => {
          active = false;
        };
      }
      if (v)
        apiQuery.set(
          key,
          (key === "start" || key === "end") &&
            !/(?:Z|[+-]\d{2}:\d{2})$/i.test(v)
            ? new Date(v).toISOString()
            : v,
        );
    }
    const id = query.get("id");
    const idType = query.get("idType");
    if (id && idType && ["run_id", "attempt_id", "session_id"].includes(idType))
      apiQuery.set(idType, id);
    const path = runId
      ? `/agent-observability/runs/${encodeURIComponent(runId)}`
      : `/agent-observability/runs?${apiQuery}`;
    request<Detail | RunList>(path)
      .then((v) => {
        if (!active) return;
        if (runId) setDetail(v as Detail);
        else setList(v as RunList);
      })
      .catch((e: Error & { status?: number; data?: { detail?: string } }) => {
        if (active)
          setError(
            e.data?.detail === "range_too_large" ||
              e.message === "range_too_large"
              ? "range"
              : runId && e.status === 404
              ? "missing"
              : e.status === 400
              ? "invalid"
              : e.status === 403
              ? "forbidden"
              : "unavailable",
          );
      });
    return () => {
      active = false;
    };
  }, [allowed, runId, retry, fetchSearch]);
  useEffect(() => {
    if (
      !runId &&
      list &&
      returnPosition?.scope === scope &&
      returnPosition.search === location.search
    ) {
      const target = document.getElementById(`run-${returnPosition.runId}`);
      const container = pageRef.current?.closest(".page-content");
      if (container) container.scrollTop = returnPosition.scrollY;
      else window.scrollTo(0, returnPosition.scrollY);
      target?.focus({ preventScroll: true });
      returnPosition = null;
    }
  }, [runId, list, scope, location.search]);
  const listSearch =
    returnPosition?.scope === scope ? returnPosition.search : embedded;
  const openRun = (id: string) => {
    returnPosition = {
      scope,
      search: location.search,
      runId: id,
      scrollY:
        pageRef.current?.closest(".page-content")?.scrollTop ?? window.scrollY,
    };
    navigate(`/agent-observability/runs/${encodeURIComponent(id)}${embedded}`);
  };
  const updateQuery = (next: URLSearchParams) =>
    navigate({
      pathname: "/agent-observability/runs",
      search: next.toString(),
    });
  const reset = () => updateQuery(new URLSearchParams(embedded));
  const submit = (event: React.FormEvent) => {
    event.preventDefault();
    const next = new URLSearchParams(embedded);
    Object.entries(draft).forEach(([key, v]) => {
      if (v && !(key === "idType" && !draft.id)) next.set(key, v);
    });
    updateQuery(next);
  };
  const back = (
    <Link to={`/agent-observability/runs${listSearch}`}>返回执行记录</Link>
  );
  if (!allowed)
    return (
      <main className={styles.page}>
        <Alert type="warning" message="无权访问 Agent 可观测" />
      </main>
    );
  return (
    <main ref={pageRef} className={styles.page}>
      {runId ? (
        <>
          {back}
          <h1>Run 摘要</h1>
        </>
      ) : (
        <>
          <h1>Agent 可观测</h1>
          <p>执行记录 · 默认最近 24 小时，精确 ID 可查询历史</p>
        </>
      )}
      <p>当前 Source：{source}</p>
      {capture === false && (
        <Alert
          type="info"
          showIcon
          message="当前采集已关闭，仍可查询历史记录。"
        />
      )}
      {!runId && (
        <form className={styles.filters} onSubmit={submit}>
          <label>
            标识类型
            <Select
              aria-label="标识类型"
              value={draft.idType}
              onChange={(v) => setDraft({ ...draft, idType: v })}
              options={["run_id", "attempt_id", "session_id"].map((v) => ({
                value: v,
                label: v
                  .replace("_id", " ID")
                  .replace(/^./, (c) => c.toUpperCase()),
              }))}
            />
          </label>
          <label>
            完整 ID
            <Input
              value={draft.id}
              onChange={(e) => setDraft({ ...draft, id: e.target.value })}
            />
          </label>
          <label>
            开始时间
            <Input
              type="datetime-local"
              value={draft.start}
              onChange={(e) => setDraft({ ...draft, start: e.target.value })}
            />
          </label>
          <label>
            结束时间
            <Input
              type="datetime-local"
              value={draft.end}
              onChange={(e) => setDraft({ ...draft, end: e.target.value })}
            />
          </label>
          <label>
            Agent ID
            <Input
              value={draft.agent_id}
              onChange={(e) => setDraft({ ...draft, agent_id: e.target.value })}
            />
          </label>
          <label>
            Run 状态
            <Select
              aria-label="Run 状态"
              value={draft.status}
              onChange={(v) => setDraft({ ...draft, status: v })}
              options={[
                { value: "", label: "全部" },
                ...statuses.map((v) => ({ value: v, label: v })),
              ]}
            />
          </label>
          <label>
            最短已知耗时（ms）
            <Input
              type="number"
              min={0}
              step={1}
              value={draft.min_duration_ms}
              onChange={(e) =>
                setDraft({ ...draft, min_duration_ms: e.target.value })
              }
            />
          </label>
          <label>
            排序
            <Select
              aria-label="排序"
              value={draft.sort_by}
              onChange={(v) => setDraft({ ...draft, sort_by: v })}
              options={[
                { value: "started_at", label: "开始时间倒序" },
                { value: "duration_us", label: "已知耗时倒序" },
              ]}
            />
          </label>
          <Button type="primary" htmlType="submit">
            查询
          </Button>
          <Button onClick={reset}>重置</Button>
        </form>
      )}
      {error === "missing" ? (
        <div className={styles.state}>
          <p>未找到此 Run</p>
          {back}
        </div>
      ) : error === "range" ? (
        <div className={styles.state}>
          <p>候选记录过多，请缩小时间范围或使用完整 ID 查询。</p>
          <Button onClick={reset}>清除筛选</Button>
        </div>
      ) : error === "invalid" || error === "forbidden" ? (
        <div className={styles.state}>
          <p>{error === "invalid" ? "请求参数无效" : "无权访问可观测数据"}</p>
          {!runId && error === "invalid" && (
            <Button onClick={reset}>清除筛选</Button>
          )}
          {runId && back}
        </div>
      ) : error === "unavailable" ? (
        <div className={styles.state}>
          <p>可观测存储或查询暂不可用</p>
          <Button onClick={() => setRetry((n) => n + 1)}>重试</Button>
        </div>
      ) : !list && !detail ? (
        <div className={styles.state}>
          <Spin tip="正在加载" />
        </div>
      ) : detail ? (
        <>
          <section className={styles.summary} aria-label="Run 摘要">
            <div>
              <span>Run ID</span>
              <strong>{detail.run.run_id}</strong>
            </div>
            <div>
              <span>状态</span>
              <Tag>{value(detail.run.status)}</Tag>
            </div>
            <div>
              <span>Agent</span>
              <strong>{value(detail.run.agent_key)}</strong>
            </div>
            <div>
              <span>开始时间</span>
              <strong>{date(detail.run.started_at)}</strong>
            </div>
            <div>
              <span>耗时</span>
              <strong>{duration(detail.run.duration_us)}</strong>
            </div>
            <div>
              <span>模型逻辑调用</span>
              <strong>{value(detail.run.llm_calls)}</strong>
            </div>
            <div>
              <span>模型请求次数</span>
              <strong>{value(detail.run.request_attempt_count)}</strong>
            </div>
            <div>
              <span>工具调用</span>
              <strong>{value(detail.run.tool_calls)}</strong>
            </div>
            <div>
              <span>输入 Tokens</span>
              <strong>{observedTokens(detail.run.input_tokens)}</strong>
            </div>
            <div>
              <span>输出 Tokens</span>
              <strong>{observedTokens(detail.run.output_tokens)}</strong>
            </div>
            <Button onClick={() => setExceptionRequest((n) => n + 1)}>
              定位异常
            </Button>
          </section>
          <AttemptView
            runId={detail.run.run_id}
            attempts={detail.attempts}
            sourceKey={scope}
            exceptionRequest={exceptionRequest}
            runStatus={detail.run.status}
            projectedAt={detail.run.projected_at || null}
            onRunRefresh={(run, attempts) =>
              setDetail((old) =>
                old ? { ...old, run: run as Run, attempts } : old,
              )
            }
          />
        </>
      ) : (
        <>
          <RunOverview
            source={source}
            start={list?.window_start || ""}
            end={list?.window_end || ""}
          />
          <Table<Run>
            rowKey="run_id"
            dataSource={list?.items}
            onRow={(run) => ({
              tabIndex: 0,
              "aria-label": `打开 Run ${run.run_id}`,
              className: styles.runRow,
              onClick: () => openRun(run.run_id),
              onKeyDown: (event) => {
                if (event.target !== event.currentTarget) return;
                if (event.key === "Enter" || event.key === " ") {
                  event.preventDefault();
                  openRun(run.run_id);
                }
              },
            })}
            pagination={false}
            scroll={{ x: 960 }}
            locale={{
              emptyText: (
                <span>
                  当前时间范围内暂无执行记录{" "}
                  <Button onClick={reset}>清除筛选</Button>
                </span>
              ),
            }}
            columns={[
              { title: "开始时间", render: (_, r) => date(r.started_at) },
              { title: "Agent", render: (_, r) => value(r.agent_key) },
              { title: "状态", render: (_, r) => <Tag>{value(r.status)}</Tag> },
              {
                title: "数据质量",
                render: (_, r) => <Tag>{value(r.quality)}</Tag>,
              },
              { title: "耗时", render: (_, r) => duration(r.duration_us) },
              { title: "调用", render: (_, r) => calls(r) },
              { title: "Tokens", render: (_, r) => tokens(r) },
              {
                title: "Run ID",
                render: (_, r) => (
                  <Link
                    id={`run-${r.run_id}`}
                    onClick={(event) => {
                      event.preventDefault();
                      event.stopPropagation();
                      openRun(r.run_id);
                    }}
                    to={`/agent-observability/runs/${encodeURIComponent(
                      r.run_id,
                    )}${embedded}`}
                  >
                    {r.run_id}
                  </Link>
                ),
              },
            ]}
          />
          <div className={styles.pagination}>
            <span>本页 {list?.items.length || 0} 条</span>
            <Button
              disabled={!params.has("previous")}
              onClick={() => {
                const next = new URLSearchParams(location.search);
                const previous = next.getAll("previous");
                const cursor = previous.pop();
                next.delete("previous");
                previous.forEach((v) => next.append("previous", v));
                if (cursor) next.set("cursor", cursor);
                else next.delete("cursor");
                updateQuery(next);
              }}
            >
              上一页
            </Button>
            <Button
              disabled={!list?.next_cursor}
              onClick={() => {
                const next = new URLSearchParams(location.search);
                next.append("previous", next.get("cursor") || "");
                if (!next.get("id")) {
                  if (!next.get("start") && list?.window_start)
                    next.set("window_start", list.window_start);
                  if (!next.get("end") && list?.window_end)
                    next.set("window_end", list.window_end);
                }
                next.set("cursor", list!.next_cursor!);
                updateQuery(next);
              }}
            >
              下一页
            </Button>
          </div>
        </>
      )}
    </main>
  );
}
