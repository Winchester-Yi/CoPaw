import { useEffect, useState } from "react";
import { Button } from "antd";
import { request } from "../../api/request";
import styles from "./runOverview.module.less";

type Business = {
  population_source: { business: string; runs: string; subagents: string };
  business_as_of: string;
  observability_as_of: string | null;
  subagents_as_of: string | null;
  business: { turn_count: number };
  runs: { status: string; settled_count: number | null };
  subagents: { status: string; count: number | null };
};
type Percentiles = {
  known_count: number;
  p50: number | null;
  p95: number | null;
  p99: number | null;
};
type Performance = {
  population_source: string;
  generated_at: string;
  sample_count: number;
  run_count: number;
  duration_us: Percentiles;
  tool_failures: { count: number; sample_count: number; rate: number | null };
  llm?: {
    call_count: number;
    request_attempt_count: number;
    usage_known_count: number;
    input_tokens_known_count: number;
    output_tokens_known_count: number;
    first_chunk_us: Percentiles;
    first_visible_text_us: Percentiles;
  };
};
type Load<T> =
  | { state: "loading" }
  | { state: "error" }
  | { state: "ready"; data: T };
const displayTime = (time: string | null) =>
  time ? new Date(time).toLocaleString() : "—";
const displayNumber = (value: number | null) =>
  value == null ? "—" : value.toLocaleString();
const millis = (value: number | null) =>
  value == null ? "—" : `${(value / 1000).toLocaleString()} ms`;

function ModelPerformance({ llm }: { llm: NonNullable<Performance["llm"]> }) {
  const requests = llm.request_attempt_count;
  const coverage = (known: number) =>
    `${displayNumber(known)}/${displayNumber(requests)}`;
  return (
    <div>
      <h3>模型请求性能</h3>
      <p>
        已观测逻辑调用 {displayNumber(llm.call_count)} · 已结束请求尝试{" "}
        {displayNumber(requests)}
      </p>
      {requests > 0 && (
        <>
          <p>
            请求用量覆盖 · 输入 {coverage(llm.input_tokens_known_count)} · 输出{" "}
            {coverage(llm.output_tokens_known_count)} · 两项均获取{" "}
            {coverage(llm.usage_known_count)}
          </p>
          {(
            [
              ["首有效 chunk", llm.first_chunk_us],
              ["首可见文本", llm.first_visible_text_us],
            ] as const
          ).map(([label, stats]) => (
            <p key={label}>
              {label}（已知样本 {displayNumber(stats.known_count)}）· P50{" "}
              {millis(stats.p50)} / P95 {millis(stats.p95)} / P99{" "}
              {millis(stats.p99)}
            </p>
          ))}
        </>
      )}
    </div>
  );
}

export default function RunOverview({
  source,
  start,
  end,
}: {
  source: string;
  start: string;
  end: string;
}) {
  const [open, setOpen] = useState(false);
  const [businessResult, setBusiness] = useState<{
    key: string;
    value: Load<Business>;
  }>();
  const [performanceResult, setPerformance] = useState<{
    key: string;
    value: Load<Performance>;
  }>();
  const [retry, setRetry] = useState(0);
  const key = JSON.stringify([source, start, end, retry]);
  const business: Load<Business> =
    businessResult?.key === key ? businessResult.value : { state: "loading" };
  const performance: Load<Performance> =
    performanceResult?.key === key
      ? performanceResult.value
      : { state: "loading" };
  useEffect(() => {
    if (!open || !start || !end) return;
    let active = true;
    setBusiness({ key, value: { state: "loading" } });
    setPerformance({ key, value: { state: "loading" } });
    const query = new URLSearchParams({ start, end });
    const options = { headers: { "X-Source-Id": source } };
    request<Business>(
      `/agent-observability/business-population?${query}`,
      options,
    )
      .then((data) => {
        if (active) setBusiness({ key, value: { state: "ready", data } });
      })
      .catch(() => {
        if (active) setBusiness({ key, value: { state: "error" } });
      });
    request<Performance>(`/agent-observability/performance?${query}`, options)
      .then((data) => {
        if (active) setPerformance({ key, value: { state: "ready", data } });
      })
      .catch(() => {
        if (active) setPerformance({ key, value: { state: "error" } });
      });
    return () => {
      active = false;
    };
  }, [open, source, start, end, key]);
  return (
    <section className={styles.overview} aria-label="业务与性能总览">
      <Button aria-expanded={open} onClick={() => setOpen(!open)}>
        {open ? "收起总览" : "展开总览"}
      </Button>
      {open && (
        <div className={styles.content}>
          <p>
            总览统计窗口：{displayTime(start)} — {displayTime(end)}（精确 ID
            查询可能包含窗口外记录）
          </p>
          {!start || !end ? (
            <p>等待执行列表的统计范围</p>
          ) : (
            <>
              <h2>业务总体</h2>
              {business.state === "loading" ? (
                <p>正在加载业务总体…</p>
              ) : business.state === "error" ? (
                <p role="alert">业务总体暂不可用</p>
              ) : (
                <div className={styles.groups}>
                  <div>
                    <h3>业务 Turn</h3>
                    <p>
                      样本量：{displayNumber(business.data.business.turn_count)}
                    </p>
                    <p>来源：{business.data.population_source.business}</p>
                    <p>统计时间：{displayTime(business.data.business_as_of)}</p>
                  </div>
                  <div>
                    <h3>已观测 Run</h3>
                    <p>
                      已关联结算样本：
                      {displayNumber(business.data.runs.settled_count)}
                    </p>
                    <p>
                      状态：
                      {business.data.runs.status === "available"
                        ? "可用"
                        : "不可用"}
                    </p>
                    <p>
                      来源：
                      {business.data.runs.status === "available"
                        ? "business_settled_runs_linked_by_exact_identity"
                        : business.data.population_source.runs}
                    </p>
                    <p>
                      统计时间：{displayTime(business.data.observability_as_of)}
                    </p>
                  </div>
                  <div>
                    <h3>后台 SubAgent</h3>
                    <p>
                      样本量：{displayNumber(business.data.subagents.count)}
                    </p>
                    <p>
                      状态：
                      {business.data.subagents.status === "available"
                        ? "可用"
                        : business.data.subagents.status === "partial"
                        ? "部分数据"
                        : "不可用"}
                    </p>
                    <p>来源：{business.data.population_source.subagents}</p>
                    <p>
                      统计时间：{displayTime(business.data.subagents_as_of)}
                    </p>
                  </div>
                </div>
              )}
              <h2>观测性能</h2>
              {performance.state === "loading" ? (
                <p>正在加载观测性能…</p>
              ) : performance.state === "error" ? (
                <p role="alert">观测性能暂不可用</p>
              ) : (
                <>
                  <p>
                    来源：{performance.data.population_source} · 样本量：
                    {displayNumber(performance.data.sample_count)} · Run 数：
                    {displayNumber(performance.data.run_count)} · 统计时间：
                    {displayTime(performance.data.generated_at)}
                  </p>
                  {performance.data.sample_count === 0 ? (
                    <p>当前时间范围内没有观测性能样本</p>
                  ) : (
                    <p>
                      Span 耗时（已知 {performance.data.duration_us.known_count}
                      ）：P50 {millis(performance.data.duration_us.p50)} / P95{" "}
                      {millis(performance.data.duration_us.p95)} / P99{" "}
                      {millis(performance.data.duration_us.p99)}；工具失败率{" "}
                      {performance.data.tool_failures.rate == null
                        ? "—"
                        : `${(
                            performance.data.tool_failures.rate * 100
                          ).toLocaleString()}%`}
                      （{performance.data.tool_failures.count}/
                      {performance.data.tool_failures.sample_count}，不含取消）
                    </p>
                  )}
                  {performance.data.llm?.first_chunk_us &&
                    (performance.data.llm.call_count > 0 ||
                      performance.data.llm.request_attempt_count > 0) && (
                      <ModelPerformance llm={performance.data.llm} />
                    )}
                </>
              )}
              {(business.state === "error" ||
                performance.state === "error") && (
                <Button onClick={() => setRetry((n) => n + 1)}>重试总览</Button>
              )}
            </>
          )}
        </div>
      )}
    </section>
  );
}
