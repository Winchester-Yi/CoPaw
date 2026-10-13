import { Alert, Button, Empty, Skeleton, Table, Tag } from "antd";
import type { ColumnsType } from "antd/es/table";
import { useEffect, useMemo, useRef } from "react";
import { Link, useLocation, useSearchParams } from "react-router-dom";
import {
  tokenDiagnosisV2Api,
  type AgentAttributionDTO,
  type DashboardDTO,
  type DiagnosisFilters,
  type DiagnosisWindow,
  type RuleCoverageDTO,
  type TaskListItemDTO,
  type UsageDTO,
} from "../../../api/modules/tokenDiagnosisV2";
import { FilterBar } from "../TokenDiagnosisShared/FilterBar";
import { useDiagnosisResource } from "../TokenDiagnosisShared/useDiagnosisResource";
import {
  formatCount,
  formatRatio,
  parseDiagnosisFilters,
  reasonLabel,
  stateLabel,
  updateDiagnosisSearch,
  RULE_LABELS,
} from "../TokenDiagnosisShared/presentation";
import styles from "./index.module.less";

function projectionFilters(filters: DiagnosisFilters): DiagnosisFilters {
  return {
    start: filters.start,
    end: filters.end,
    agent_id: filters.agent_id,
    root_agent_id: filters.root_agent_id,
    model_name: filters.model_name,
  };
}
function taskHref(
  task: TaskListItemDTO,
  search: URLSearchParams,
  window: DiagnosisWindow,
) {
  const next = updateDiagnosisSearch(search, {
    start: window.start,
    end: window.end,
    cursor: undefined,
    branch_run_id: undefined,
    rule_id: undefined,
    state: undefined,
  });
  for (const key of [
    "relation_cursor",
    "call_id",
    "context_id",
    "evaluation_id",
    "operation_id",
    "step_id",
    "group_id",
    "finding_id",
    "returnTo",
  ])
    next.delete(key);
  const returnSearch = new URLSearchParams(search);
  returnSearch.set("start", window.start);
  returnSearch.set("end", window.end);
  next.set("returnTo", `/analytics/token-overview?${returnSearch}#task-list`);
  return `/analytics/token-executions/${encodeURIComponent(
    task.task_run_id,
  )}?${next}`;
}
function populationLabel(population: string) {
  const labels: Record<string, string> = {
    "window attempts grouped by agent": "窗口物理 attempt 按 Agent 归属分组",
    "window attempts with a current saved comparison":
      "窗口内存在当前已保存比较的物理 attempt",
  };
  return labels[population] || population;
}
function Metric({ label, value }: { label: string; value: string }) {
  return (
    <div className={styles.metric}>
      <span>{label}</span>
      <strong>{value}</strong>
    </div>
  );
}
function usageCoverage(usage: UsageDTO) {
  return `${formatCount(usage.reported_call_count)} / ${formatCount(
    usage.observed_call_count,
  )}（${formatRatio(
    usage.observed_call_count
      ? usage.reported_call_count / usage.observed_call_count
      : null,
  )}）`;
}
function UsagePanel({ usage }: { usage: UsageDTO }) {
  return (
    <section className={styles.section} aria-label="窗口已知用量">
      <h2>窗口已知用量</h2>
      <p className={styles.caption}>
        物理 attempt started_at 纳入 UTC 半开时间窗；执行 Agent、根 Agent
        和模型筛选选择窗口内的对应尝试。
      </p>
      <div className={styles.metrics}>
        <Metric
          label={`已知 Token 总量（${usage.usage_complete ? "完整" : "部分"}）`}
          value={formatCount(usage.known_total_tokens)}
        />
        <Metric
          label="已知输入 Token"
          value={formatCount(usage.known_input_tokens)}
        />
        <Metric
          label="已知输出 Token"
          value={formatCount(usage.known_output_tokens)}
        />
        <Metric label="Usage 完整率" value={usageCoverage(usage)} />
      </div>
      <p className={styles.caption}>
        真实总量：{formatCount(usage.actual_total_tokens)}
        {" · "}完整报告 / 已记录物理 attempt（包括未知与待完成）；部分报告：
        {formatCount(usage.partial_call_count)}，未知：
        {formatCount(usage.unknown_call_count)}
      </p>
    </section>
  );
}
const attributionColumns: ColumnsType<AgentAttributionDTO["items"][number]> = [
  {
    title: "Agent",
    dataIndex: "agent_id",
    width: 180,
    render: (value: string | null) => (
      <span className={styles.id}>{value || "未知"}</span>
    ),
  },
  {
    title: "已知输入",
    render: (_, item) => formatCount(item.usage.known_input_tokens),
  },
  {
    title: "已知输出",
    render: (_, item) => formatCount(item.usage.known_output_tokens),
  },
  {
    title: "已知总量",
    render: (_, item) => formatCount(item.usage.known_total_tokens),
  },
  {
    title: "完整报告 / 已记录",
    render: (_, item) =>
      `${formatCount(item.usage.reported_call_count)} / ${formatCount(
        item.usage.observed_call_count,
      )}`,
  },
];
function Attribution({
  title,
  data,
}: {
  title: string;
  data: AgentAttributionDTO;
}) {
  return (
    <section className={styles.section} aria-label={title}>
      <h2>{title}</h2>
      <p className={styles.caption}>
        窗口内物理 attempt 的已知用量投影 · {populationLabel(data.population)}
      </p>
      {data.limited && (
        <Alert
          type="warning"
          message="归属列表查询受限；未列出的 Agent 仍可能有用量"
        />
      )}
      <Table
        size="small"
        rowKey={(row) => row.agent_id ?? "unknown"}
        dataSource={data.items}
        columns={attributionColumns}
        pagination={false}
        scroll={{ x: 560 }}
        locale={{ emptyText: "窗口内没有已记录的 Agent 用量" }}
      />
    </section>
  );
}
function Distribution({
  data,
}: {
  data: DashboardDTO["lifecycle_distribution"];
}) {
  const quantiles = data.quantiles;
  return (
    <section className={styles.section} aria-label="Task 生命周期用量分布">
      <h2>Task 生命周期用量分布</h2>
      <p className={styles.caption}>
        根 Task started_at 纳入窗口；仅已终结、采集及 Usage 均完整的 Task
        全模型生命周期样本。筛选只选择样本，执行规模分布不是效率或质量排名。
      </p>
      <div className={styles.metrics}>
        <Metric
          label="完整生命周期样本数"
          value={`${formatCount(data.sample_count)} / ${formatCount(
            data.task_count,
          )} Task`}
        />
        <Metric label="p50 Token" value={formatCount(quantiles.p50)} />
        <Metric label="p90 Token" value={formatCount(quantiles.p90)} />
        <Metric label="p95 Token" value={formatCount(quantiles.p95)} />
      </div>
      <p className={styles.caption}>
        分位样本数：{formatCount(quantiles.sample_count)} · 方法：
        {quantiles.method}
      </p>
      {!quantiles.available && (
        <p className={styles.unavailable}>
          分布不可用：{reasonLabel(quantiles.reason_code) || "缺少可信样本"}
        </p>
      )}
    </section>
  );
}
function Prefix({ data }: { data: DashboardDTO["prefix"] }) {
  return (
    <section className={styles.section} aria-label="保守文本前缀比例">
      <h2>保守文本前缀比例</h2>
      <p className={styles.caption}>
        按文本 bytes 加权（保守匹配 bytes / 参与文本
        bytes），仅使用当前已保存比较；不代表缓存命中率或节省 Token。
      </p>
      <strong className={styles.prefixValue}>{formatRatio(data.ratio)}</strong>
      <div className={styles.inlineMetrics}>
        <span>
          {formatCount(data.matched_text_bytes)} /{" "}
          {formatCount(data.eligible_text_bytes)} bytes
        </span>
        <span>参与请求：{formatCount(data.request_count)}</span>
      </div>
      {data.limited && <p className={styles.unavailable}>前缀查询受限</p>}
      <h3>排除原因与请求数</h3>
      <div className={styles.exclusions}>
        {Object.entries(data.excluded).map(([reason, count]) => (
          <Tag key={reason}>
            {reasonLabel(
              reason === "pending"
                ? "analysis_pending"
                : reason === "failed"
                ? "analysis_failed"
                : reason,
            )}
            ：{formatCount(count)}
          </Tag>
        ))}
      </div>
      <p className={styles.caption}>{populationLabel(data.population)}</p>
    </section>
  );
}
const coverageColumns: ColumnsType<RuleCoverageDTO> = [
  {
    title: "规则",
    dataIndex: "rule_id",
    width: 220,
    render: (rule: RuleCoverageDTO["rule_id"]) => RULE_LABELS[rule],
  },
  { title: "候选问题", dataIndex: "candidate", render: formatCount },
  { title: "未命中", dataIndex: "not_hit", render: formatCount },
  {
    title: "证据不足",
    dataIndex: "insufficient_evidence",
    render: formatCount,
  },
  { title: "不适用", dataIndex: "not_applicable", render: formatCount },
  { title: "待评估分支", dataIndex: "pending", render: formatCount },
  { title: "分析失败分支", dataIndex: "failed", render: formatCount },
];
function Coverage({
  data,
  search,
  window,
}: {
  data: RuleCoverageDTO[];
  search: URLSearchParams;
  window: DiagnosisWindow;
}) {
  const columns = coverageColumns.map((column, index) =>
    index === 0
      ? {
          ...column,
          render: (rule: RuleCoverageDTO["rule_id"]) => {
            const next = updateDiagnosisSearch(search, {
              start: window.start,
              end: window.end,
              rule_id: rule,
              state: "candidate",
              cursor: undefined,
              branch_run_id: undefined,
            });
            next.delete("relation_cursor");
            return (
              <Link to={`/analytics/token-issues?${next}`}>
                {RULE_LABELS[rule]}
              </Link>
            );
          },
        }
      : column,
  );
  return (
    <section className={styles.section} aria-label="规则评估覆盖">
      <h2>规则评估覆盖</h2>
      <p className={styles.caption}>
        前四类按当前评估对象计数；待评估与分析失败按缺少当前评估头的分支计数。待评估、失败、证据不足均不等于无问题。
      </p>
      <Table
        size="small"
        rowKey="rule_id"
        dataSource={data}
        columns={columns}
        pagination={false}
        scroll={{ x: 850 }}
        locale={{ emptyText: "规则覆盖数据不可用" }}
      />
    </section>
  );
}
function TaskList({
  items,
  search,
  window,
}: {
  items: TaskListItemDTO[];
  search: URLSearchParams;
  window: DiagnosisWindow;
}) {
  const columns: ColumnsType<TaskListItemDTO> = [
    {
      title: "Task",
      dataIndex: "task_run_id",
      width: 240,
      render: (id: string, task) => (
        <Link className={styles.id} to={taskHref(task, search, window)}>
          {id}
        </Link>
      ),
    },
    {
      title: "根 Agent",
      dataIndex: "agent_id",
      width: 160,
      render: (value: string | null) => (
        <span className={styles.id}>{value || "未知"}</span>
      ),
    },
    { title: "执行状态", dataIndex: "execution_state", render: stateLabel },
    { title: "分析状态", dataIndex: "analysis_state", render: stateLabel },
    {
      title: "生命周期已知 Token",
      render: (_, task) => (
        <>
          {formatCount(task.lifecycle_usage.known_total_tokens)}
          <span className={styles.cellHint}>
            真实总量：{formatCount(task.lifecycle_usage.actual_total_tokens)}
          </span>
        </>
      ),
    },
    {
      title: "选中投影已知贡献",
      render: (_, task) =>
        formatCount(task.selected_contribution.known_total_tokens),
    },
    {
      title: "完整性",
      render: (_, task) => (
        <>
          {stateLabel(task.completeness.capture_state)}
          <span className={styles.cellHint}>
            Usage {task.completeness.usage_complete ? "完整" : "不完整"} ·
            缺失子分支 {formatCount(task.completeness.missing_child_count)} ·
            缺口 {formatCount(task.completeness.gap_count)}
            {task.completeness.gap_tracking_limited ? " · 缺口跟踪受限" : ""}
          </span>
        </>
      ),
    },
  ];
  return (
    <Table
      size="small"
      rowKey="task_run_id"
      dataSource={items}
      columns={columns}
      pagination={false}
      scroll={{ x: 1160 }}
      locale={{
        emptyText: (
          <Empty
            image={Empty.PRESENTED_IMAGE_SIMPLE}
            description="当前筛选下没有已记录的 Task"
          />
        ),
      }}
    />
  );
}
export default function TokenOverview() {
  const location = useLocation();
  const [search, setSearch] = useSearchParams();
  const parsed = useMemo(() => parseDiagnosisFilters(search), [search]);
  const filters = useMemo(() => projectionFilters(parsed), [parsed]);
  const taskFilters = {
    ...filters,
    limit: parsed.limit,
    cursor: parsed.cursor,
  };
  const dashboard = useDiagnosisResource(
    `dashboard:${JSON.stringify(filters)}`,
    (signal) => tokenDiagnosisV2Api.dashboard(signal, filters),
  );
  const tasks = useDiagnosisResource(
    `tasks:${JSON.stringify(taskFilters)}`,
    (signal) => tokenDiagnosisV2Api.tasks(signal, taskFilters),
  );
  const data = dashboard.data;
  const taskListRef = useRef<HTMLElement>(null);
  const taskLandingComplete = useRef(false);
  useEffect(() => {
    if (location.hash !== "#task-list") {
      taskLandingComplete.current = false;
      return;
    }
    if (
      taskLandingComplete.current ||
      dashboard.loading ||
      tasks.state !== "ready"
    )
      return;
    const target = taskListRef.current;
    if (!target) return;
    target.scrollIntoView({ block: "start" });
    target.focus({ preventScroll: true });
    taskLandingComplete.current = true;
  }, [location.hash, dashboard.loading, tasks.state]);
  const changeFilters = (next: DiagnosisFilters) =>
    setSearch(
      updateDiagnosisSearch(search, {
        start: next.start,
        end: next.end,
        agent_id: next.agent_id,
        root_agent_id: next.root_agent_id,
        model_name: next.model_name,
        limit: next.limit,
        rule_id: undefined,
        state: undefined,
        branch_run_id: undefined,
        cursor: undefined,
      }),
    );
  const nextPage = () => {
    if (!tasks.data?.next_cursor) return;
    const locked = updateDiagnosisSearch(search, {
      start: tasks.data.window.start,
      end: tasks.data.window.end,
    });
    setSearch(
      updateDiagnosisSearch(locked, { cursor: tasks.data.next_cursor }),
    );
  };
  return (
    <main className={styles.page}>
      <header className={styles.header}>
        <h1>Token 总览</h1>
        <Button
          onClick={() => {
            dashboard.reload();
            tasks.reload();
          }}
          disabled={dashboard.loading || tasks.loading}
        >
          刷新
        </Button>
      </header>
      <FilterBar
        filters={filters}
        window={data?.window || tasks.data?.window}
        onChange={changeFilters}
      />
      {dashboard.loading && (
        <Skeleton active title={{ width: "25%" }} paragraph={{ rows: 5 }} />
      )}
      {(dashboard.state === "error" ||
        dashboard.state === "unavailable" ||
        dashboard.state === "not_found") && (
        <Alert
          showIcon
          type={dashboard.state === "error" ? "error" : "warning"}
          message={dashboard.error}
          action={<Button onClick={dashboard.reload}>重试总览</Button>}
        />
      )}
      {data && (
        <>
          <p className={styles.metadata}>
            来源 {data.scope.source_id} · 租户 {data.scope.tenant_id}
            <br />
            窗口 [{data.window.start}, {data.window.end}) ·{" "}
            {data.window.semantics}
            <br />
            as_of：{data.as_of}
          </p>
          <p className={styles.taskCount}>
            已记录 Task：{formatCount(data.lifecycle_distribution.task_count)}
          </p>
          <UsagePanel usage={data.window_attempt_usage} />
          <div className={styles.split}>
            <Distribution data={data.lifecycle_distribution} />
            <Prefix data={data.prefix} />
          </div>
          <div className={styles.split}>
            <Attribution
              title="执行 Agent 用量"
              data={data.agent_attribution.executing_agents}
            />
            <Attribution
              title="根 Agent 归属用量"
              data={data.agent_attribution.root_agents}
            />
          </div>
          <Coverage
            data={data.rule_coverage}
            search={search}
            window={data.window}
          />
          {!data.limits.available && (
            <p className={styles.caption}>
              部署预算压力：不可用 ·{" "}
              {reasonLabel(data.limits.reason_code) || "缺少实际部署预算"}
            </p>
          )}
        </>
      )}
      <section
        id="task-list"
        ref={taskListRef}
        tabIndex={-1}
        className={styles.section}
        aria-label="已记录 Task 列表"
      >
        <div className={styles.header}>
          <h2>已记录 Task</h2>
          <Button
            disabled={!tasks.data?.next_cursor || tasks.loading}
            onClick={nextPage}
          >
            下一页 Task
          </Button>
        </div>
        <p className={styles.caption}>
          根 Task started_at 纳入窗口；执行 Agent
          与模型按生命周期内对应尝试选择样本，根 Agent
          按根任务归属选择。生命周期总量包含全部分支和模型，选中贡献单独列示。
        </p>
        {tasks.loading && <Skeleton active paragraph={{ rows: 3 }} />}
        {(tasks.state === "error" ||
          tasks.state === "unavailable" ||
          tasks.state === "not_found") && (
          <Alert
            type="warning"
            message={tasks.error}
            action={<Button onClick={tasks.reload}>重试 Task 列表</Button>}
          />
        )}
        {tasks.data && (
          <>
            <p className={styles.metadata}>
              Task 窗口 [{tasks.data.window.start}, {tasks.data.window.end}) ·
              as_of：{tasks.data.as_of}
            </p>
            <TaskList
              items={tasks.data.items}
              search={search}
              window={tasks.data.window}
            />
          </>
        )}
      </section>
    </main>
  );
}
