import {
  CalendarDays,
  CheckCircle2,
  ChevronRight,
  Clock3,
  Info,
  PlaySquare,
  ShieldCheck,
  TrendingDown,
  TrendingUp,
  UserRound,
  Workflow,
  type LucideIcon,
} from "lucide-react";
import { useEffect, useState } from "react";
import { DatePicker, Input, Modal, Select, Spin, Tooltip } from "antd";
import { WarningOutlined } from "@ant-design/icons";
import type { Dayjs } from "dayjs";
import dayjs from "dayjs";
import { monitorApi, CronOverviewResponse, ExecutionItem } from "../../../api/modules/monitor";
import styles from "./index.module.less";

type TimeRange = "day" | "week" | "month" | "custom";

type MetricCard = {
  key: string;
  title: string;
  value: string;
  compare: string;
  trend: "up" | "down";
  accent: string;
  icon: LucideIcon;
};

type DistributionItem = {
  name: string;
  value: number;
  percent?: number;
  color?: string;
};

const formatNumber = (value: number) => value.toLocaleString("en-US");
const truncateAxisLabel = (value: string, maxLength = 6) =>
  value.length > maxLength ? `${value.slice(0, maxLength)}...` : value;

const hashString = (value: string) => {
  let hash = 0;
  for (let index = 0; index < value.length; index += 1) {
    hash = (hash * 31 + value.charCodeAt(index)) >>> 0;
  }
  return hash.toString(36);
};

const metricDefinitions = [
  {
    key: "total",
    title: "定时任务数",
    accent: "#2563eb",
    icon: Workflow,
  },
  {
    key: "subscribed",
    title: "订阅任务数",
    accent: "#7c3aed",
    icon: CalendarDays,
  },
  {
    key: "created",
    title: "自主创建任务数",
    accent: "#0891b2",
    icon: UserRound,
  },
  {
    key: "runs",
    title: "今日执行次数",
    accent: "#f97316",
    icon: PlaySquare,
  },
  {
    key: "success_rate",
    title: "执行成功率",
    accent: "#16a34a",
    icon: ShieldCheck,
  },
  {
    key: "avg_cost",
    title: "平均耗时",
    accent: "#2563eb",
    icon: Clock3,
  },
] as const;

const formatMetricValue = (key: string, value: number | undefined) => {
  if (value === undefined || value === null) {
    return "-";
  }
  if (key === "success_rate") {
    return `${value.toFixed(2)}%`;
  }
  if (key === "avg_cost") {
    return `${value.toFixed(2)}s`;
  }
  return formatNumber(value);
};

function SectionTitle({ children }: { children: React.ReactNode }) {
  return (
    <div className={styles.sectionTitle}>
      <span />
      <h2>{children}</h2>
    </div>
  );
}

function MetricCard({ metric }: { metric: MetricCard }) {
  const Icon = metric.icon;
  const trendClassName =
    metric.trend === "up" ? styles.goodTrend : styles.hotTrend;

  return (
    <article
      className={styles.metricCard}
      style={{ borderTopColor: metric.accent }}
    >
      <div className={styles.metricHeader}>
        <i
          style={{
            color: "#ffffff",
            backgroundColor: metric.accent,
          }}
        >
          <Icon size={22} />
        </i>
        <div className={styles.metricText}>
          <span className={styles.metricTitle}>{metric.title}</span>
          <strong>{metric.value}</strong>
          {metric.compare ? (
            <div className={`${styles.metricCompare} ${trendClassName}`}>
              <span>环比</span>
              {metric.trend === "up" ? (
                <TrendingUp size={14} />
              ) : (
                <TrendingDown size={14} />
              )}
              <em>{metric.compare}</em>
            </div>
          ) : null}
        </div>
      </div>
    </article>
  );
}

const polarPoint = (cx: number, cy: number, radius: number, angle: number) => ({
  x: cx + radius * Math.cos(angle),
  y: cy + radius * Math.sin(angle),
});

function seamCurve(
  cx: number,
  cy: number,
  outerRadius: number,
  innerRadius: number,
  angle: number,
  fromOuter: boolean,
) {
  const outer = polarPoint(cx, cy, outerRadius, angle);
  const inner = polarPoint(cx, cy, innerRadius, angle);
  const control = polarPoint(cx, cy, (outerRadius + innerRadius) / 2, angle + 0.22);
  const end = fromOuter ? inner : outer;

  return `Q ${control.x.toFixed(3)} ${control.y.toFixed(3)} ${end.x.toFixed(3)} ${end.y.toFixed(3)}`;
}

function donutSegmentPath(
  startAngle: number,
  endAngle: number,
  cx = 74,
  cy = 74,
  outerRadius = 56,
  innerRadius = 39,
) {
  const outerStart = polarPoint(cx, cy, outerRadius, startAngle);
  const outerEnd = polarPoint(cx, cy, outerRadius, endAngle);
  const innerStart = polarPoint(cx, cy, innerRadius, startAngle);
  const largeArcFlag = endAngle - startAngle > Math.PI ? 1 : 0;

  return [
    `M ${outerStart.x.toFixed(3)} ${outerStart.y.toFixed(3)}`,
    `A ${outerRadius} ${outerRadius} 0 ${largeArcFlag} 1 ${outerEnd.x.toFixed(3)} ${outerEnd.y.toFixed(3)}`,
    seamCurve(cx, cy, outerRadius, innerRadius, endAngle, true),
    `A ${innerRadius} ${innerRadius} 0 ${largeArcFlag} 0 ${innerStart.x.toFixed(3)} ${innerStart.y.toFixed(3)}`,
    seamCurve(cx, cy, outerRadius, innerRadius, startAngle, false),
    "Z",
  ].join(" ");
}

function CurvedDonutChart({
  items,
  centerValue,
  centerLabel,
}: {
  items: DistributionItem[];
  centerValue: string;
  centerLabel: string;
}) {
  const total = items.reduce((sum, item) => sum + item.value, 0);
  const gradientPrefix = `donut-${hashString(
    `${centerLabel}-${items.map((item) => item.name).join("-")}`,
  )}`;
  let currentAngle = -Math.PI / 2;

  return (
    <svg
      className={styles.curvedDonut}
      viewBox="0 0 148 148"
      role="img"
      aria-label={`${centerLabel} ${centerValue}`}
    >
      <defs>
        {items.map((item, index) => {
          const color = item.color || "#94a3b8";
          return (
            <radialGradient
              key={item.name}
              id={`${gradientPrefix}-${index}`}
              cx="50%"
              cy="50%"
              r="62%"
            >
              <stop offset="58%" stopColor={color} stopOpacity="0.72" />
              <stop offset="100%" stopColor={color} />
            </radialGradient>
          );
        })}
      </defs>
      {items.map((item, index) => {
        const angle = total ? (item.value / total) * Math.PI * 2 : 0;
        const startAngle = currentAngle;
        const endAngle = currentAngle + angle;
        currentAngle = endAngle;

        return (
          <path
            key={item.name}
            d={donutSegmentPath(startAngle, endAngle)}
            fill={`url(#${gradientPrefix}-${index})`}
          >
            <title>
              {item.name}: {formatNumber(item.value)}
              {item.percent !== undefined ? ` (${item.percent.toFixed(2)}%)` : ""}
            </title>
          </path>
        );
      })}
      <text
        x="74"
        y="68"
        textAnchor="middle"
        className={styles.curvedDonutValue}
      >
        {centerValue}
      </text>
      <text
        x="74"
        y="86"
        textAnchor="middle"
        className={styles.curvedDonutLabel}
      >
        {centerLabel}
      </text>
    </svg>
  );
}

function Panel({
  title,
  children,
  action,
}: {
  title: string;
  children: React.ReactNode;
  action?: React.ReactNode;
}) {
  return (
    <article className={styles.panel}>
      <div className={styles.panelHeader}>
        <h3>{title}</h3>
        {action}
      </div>
      {children}
    </article>
  );
}

function DonutPanel({
  title,
  items,
  centerValue,
  centerLabel,
}: {
  title: string;
  items: DistributionItem[];
  centerValue: string;
  centerLabel: string;
}) {
  return (
    <Panel title={title}>
      <div className={styles.donutLayout}>
        <CurvedDonutChart
          items={items}
          centerValue={centerValue}
          centerLabel={centerLabel}
        />
        <div className={styles.legendList}>
          {items.map((item) => (
            <div key={item.name} className={styles.legendRow}>
              <span>
                <i style={{ backgroundColor: item.color }} />
                {item.name}
              </span>
              <strong>
                {formatNumber(item.value)} ({item.percent.toFixed(2)}%)
              </strong>
            </div>
          ))}
        </div>
      </div>
    </Panel>
  );
}

function FailureReasonChart({
  items,
}: {
  items: DistributionItem[];
}) {
  const maxValue = Math.max(...items.map((item) => item.value), 1);

  return (
    <div className={styles.failureChart}>
      {items.map((item) => (
        <div key={item.name} className={styles.failureRow}>
          <Tooltip title={item.name} placement="topLeft">
            <span className={styles.failureLabel}>
              {truncateAxisLabel(item.name)}
            </span>
          </Tooltip>
          <div className={styles.failureBarTrack}>
            <span
              className={styles.failureBar}
              style={{
                width: `${Math.max(5, (item.value / maxValue) * 100)}%`,
              }}
            />
          </div>
          <strong className={styles.failureValue}>
            {item.value}
            {item.percent !== undefined ? ` (${item.percent.toFixed(2)}%)` : ""}
          </strong>
        </div>
      ))}
    </div>
  );
}

function normalizeStack<T extends Record<string, string | number>>(
  row: T,
  keys: string[],
) {
  const total = keys.reduce((sum, key) => sum + Number(row[key]), 0);

  return keys.reduce<Record<string, number>>((result, key, index) => {
    if (!total) {
      result[key] = 0;
      return result;
    }

    if (index === keys.length - 1) {
      const previousTotal = keys
        .slice(0, -1)
        .reduce((sum, prevKey) => sum + result[prevKey], 0);
      result[key] = Number((100 - previousTotal).toFixed(2));
      return result;
    }

    result[key] = Number(((Number(row[key]) / total) * 100).toFixed(2));
    return result;
  }, {});
}

function curvedStackSegmentPath(
  start: number,
  end: number,
  isFirst: boolean,
  isLast: boolean,
) {
  const height = 12;
  const curveOffset = 2.4;
  const rightBoundary = isLast
    ? `L ${end.toFixed(3)} ${height}`
    : `Q ${(end + curveOffset).toFixed(3)} ${(height / 2).toFixed(3)} ${end.toFixed(3)} ${height}`;
  const leftBoundary = isFirst
    ? `L ${start.toFixed(3)} 0`
    : `Q ${(start + curveOffset).toFixed(3)} ${(height / 2).toFixed(3)} ${start.toFixed(3)} 0`;

  return [
    `M ${start.toFixed(3)} 0`,
    `L ${end.toFixed(3)} 0`,
    rightBoundary,
    `L ${start.toFixed(3)} ${height}`,
    leftBoundary,
    "Z",
  ].join(" ");
}

function BranchLegend({
  items,
}: {
  items: Array<{ label: string; color: string }>;
}) {
  return (
    <div className={styles.branchLegend}>
      {items.map((item) => (
        <span key={item.label}>
          <i style={{ backgroundColor: item.color }} />
          {item.label}
        </span>
      ))}
    </div>
  );
}

function BranchTaskCell({
  name,
  rows,
  maxValue,
}: {
  name: string;
  rows: DistributionItem[];
  maxValue: number;
}) {
  const task = rows.find((item) => item.name === name);
  const value = task?.value ?? 0;

  return (
    <div className={styles.branchBarRow}>
      <span className={styles.branchName}>{name}</span>
      <div className={styles.branchBarTrack}>
        <span
          className={styles.branchSingleBar}
          style={{ width: `${Math.max(5, (value / maxValue) * 100)}%` }}
        />
      </div>
      <strong>{formatNumber(value)}</strong>
    </div>
  );
}

function BranchStackCell<T extends { name: string } & Record<string, string | number>>({
  name,
  rows,
  keys,
  colors,
}: {
  name: string;
  rows: T[];
  keys: string[];
  colors: string[];
}) {
  const row = rows.find((item) => item.name === name);
  const normalized = row ? normalizeStack(row, keys) : {};
  const gradientPrefix = `branch-stack-${name.replace(
    /[^a-zA-Z0-9_-]/g,
    "-",
  )}-${keys.join("-")}`;
  let currentStart = 0;

  return (
    <div className={styles.branchBarRow}>
      <span className={styles.branchName}>{name}</span>
      <svg
        className={styles.branchStackTrack}
        viewBox="0 0 100 12"
        preserveAspectRatio="none"
      >
        <defs>
          {colors.map((color, index) => (
            <linearGradient
              key={color}
              id={`${gradientPrefix}-${index}`}
              x1="0"
              y1="0"
              x2="1"
              y2="0"
            >
              <stop offset="0%" stopColor={color} />
              <stop offset="100%" stopColor={color} stopOpacity="0.72" />
            </linearGradient>
          ))}
        </defs>
        {keys.map((key, index) => {
          const value = normalized[key] ?? 0;
          const start = currentStart;
          const end = index === keys.length - 1 ? 100 : currentStart + value;
          currentStart = end;

          return (
            <path
              key={key}
              d={curvedStackSegmentPath(
                start,
                end,
                index === 0,
                index === keys.length - 1,
              )}
              fill={`url(#${gradientPrefix}-${index})`}
            />
          );
        })}
      </svg>
      <strong>100%</strong>
    </div>
  );
}

function BranchSharedOverview({
  branchTasks,
  branchExecution,
  branchRead,
}: {
  branchTasks: DistributionItem[];
  branchExecution: Array<{
    name: string;
    success: number;
    failed: number;
    skipped: number;
  }>;
  branchRead: Array<{
    name: string;
    read: number;
    unread: number;
  }>;
}) {
  const branchNames = branchTasks.map((item) => item.name);
  const maxTaskValue = Math.max(...branchTasks.map((item) => item.value), 1);

  return (
    <section className={styles.branchSharedSection}>
      <div className={styles.branchSharedScroller}>
        <div className={styles.branchSharedGrid}>
          <article className={styles.branchHeaderCard}>
            <h3>分行定时任务数量</h3>
            <div className={styles.branchLegendSpacer} />
          </article>
          <article className={styles.branchHeaderCard}>
            <h3>分行执行结果分布</h3>
            <BranchLegend
              items={[
                { label: "成功", color: "#16a34a" },
                { label: "失败", color: "#ef4444" },
                { label: "已取消/跳过", color: "#94a3b8" },
              ]}
            />
          </article>
          <article className={styles.branchHeaderCard}>
            <h3>分行阅读状态分布</h3>
            <BranchLegend
              items={[
                { label: "已读", color: "#2563eb" },
                { label: "未读", color: "#f97316" },
              ]}
            />
          </article>

          {branchNames.map((name) => (
            <div key={name} className={styles.branchSharedRow}>
              <div className={styles.branchCell}>
                <BranchTaskCell name={name} rows={branchTasks} maxValue={maxTaskValue} />
              </div>
              <div className={styles.branchCell}>
                <BranchStackCell
                  name={name}
                  rows={branchExecution}
                  keys={["success", "failed", "skipped"]}
                  colors={["#16a34a", "#ef4444", "#94a3b8"]}
                />
              </div>
              <div className={styles.branchCell}>
                <BranchStackCell
                  name={name}
                  rows={branchRead}
                  keys={["read", "unread"]}
                  colors={["#2563eb", "#f97316"]}
                />
              </div>
            </div>
          ))}
        </div>
      </div>
    </section>
  );
}

function FailedTaskModal({
  open,
  onClose,
  tasks,
  loading,
}: {
  open: boolean;
  onClose: () => void;
  tasks: ExecutionItem[];
  loading: boolean;
}) {
  const [keyword, setKeyword] = useState("");
  const [filterField, setFilterField] = useState<"job_name" | "tenant_name">(
    "tenant_name",
  );
  const normalizedKeyword = keyword.trim().toLowerCase();
  const filteredTasks = normalizedKeyword
    ? tasks.filter((task) =>
        (task[filterField] || "").toLowerCase().includes(normalizedKeyword),
      )
    : tasks;
  const searchPlaceholder =
    filterField === "tenant_name" ? "输入用户姓名筛选" : "输入任务名称筛选";

  return (
    <Modal
      open={open}
      className={styles.failedTaskModal}
      title={
        <div
          className={styles.failedTaskModalTitle}
        >
          <span className={styles.failedTaskWarningIcon}>
            <WarningOutlined />
          </span>
          <span>执行失败任务清单</span>
        </div>
      }
      width={1080}
      footer={null}
      onCancel={onClose}
      destroyOnHidden
    >
      <div className={styles.failedTaskToolbar}>
        <Select
          value={filterField}
          onChange={setFilterField}
          className={styles.failedTaskFilterSelect}
          options={[
            { label: "用户姓名", value: "tenant_name" },
            { label: "任务名称", value: "job_name" },
          ]}
        />
        <Input.Search
          value={keyword}
          onChange={(event) => setKeyword(event.target.value)}
          onSearch={setKeyword}
          allowClear
          placeholder={searchPlaceholder}
          className={styles.failedTaskSearch}
        />
      </div>
      <Spin spinning={loading} tip="加载失败任务...">
        <div className={styles.failedTaskTable}>
          <div className={styles.failedTaskTableHeader}>
            <span>任务名称</span>
            <span>用户姓名</span>
            <span>用户id</span>
            <span>执行时间</span>
            <span>耗时</span>
            <span>报错信息</span>
          </div>
          <div className={styles.failedTaskTableBody}>
            {filteredTasks.map((task) => (
              <div key={task.id} className={styles.failedTaskTableRow}>
                <span className={styles.failedTaskName}>{task.job_name}</span>
                <span>{task.tenant_name}</span>
                <span>{task.tenant_id}</span>
                <span>{task.actual_time ? dayjs(task.actual_time).format("YYYY-MM-DD HH:mm:ss") : "-"}</span>
                <span>
                  {task.duration_ms === undefined || task.duration_ms === null
                    ? "-"
                    : task.duration_ms < 1000
                    ? `${task.duration_ms}ms`
                    : `${(task.duration_ms / 1000).toFixed(2)}s`}
                </span>
                <Tooltip title={task.error_message} placement="topLeft">
                  <span className={styles.errorMessageCell}>
                    {task.error_message || "-"}
                  </span>
                </Tooltip>
              </div>
            ))}
          </div>
        </div>
      </Spin>
    </Modal>
  );
}

export default function CronJobOverviewPage() {
  const [timeRange, setTimeRange] = useState<TimeRange>("week");
  const [dateRange, setDateRange] = useState<[Dayjs, Dayjs]>([
    dayjs().subtract(6, "day"),
    dayjs(),
  ]);
  const [overview, setOverview] = useState<CronOverviewResponse | null>(null);
  const [overviewLoading, setOverviewLoading] = useState(false);
  const [failedTaskModalOpen, setFailedTaskModalOpen] = useState(false);
  const [failedTasks, setFailedTasks] = useState<ExecutionItem[]>([]);
  const [failedTasksLoading, setFailedTasksLoading] = useState(false);

  const getDateRangeParams = (range: [Dayjs, Dayjs]) => ({
    start_time: range[0].startOf("day").format("YYYY-MM-DDTHH:mm:ss"),
    end_time: range[1].endOf("day").format("YYYY-MM-DDTHH:mm:ss"),
  });

  const fetchOverview = async () => {
    setOverviewLoading(true);
    try {
      const params = getDateRangeParams(dateRange);
      const response = await monitorApi.getCronOverview(params);
      setOverview(response);
    } catch (error) {
      console.error("Failed to fetch cron overview:", error);
    } finally {
      setOverviewLoading(false);
    }
  };

  const fetchFailedTasks = async () => {
    setFailedTasksLoading(true);
    try {
      const params = {
        ...getDateRangeParams(dateRange),
        status: "error",
      };
      const response = await monitorApi.getExecutions(1, 100, params);
      setFailedTasks(response.items);
    } catch (error) {
      console.error("Failed to fetch failed tasks:", error);
    } finally {
      setFailedTasksLoading(false);
    }
  };

  useEffect(() => {
    fetchOverview();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [timeRange, dateRange]);

  useEffect(() => {
    if (failedTaskModalOpen) {
      fetchFailedTasks();
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [failedTaskModalOpen, dateRange]);

  const handleModeChange = (nextRange: TimeRange) => {
    setTimeRange(nextRange);
    const today = dayjs();

    if (nextRange === "day") {
      setDateRange([today, today]);
    } else if (nextRange === "week") {
      setDateRange([today.subtract(6, "day"), today]);
    } else if (nextRange === "month") {
      setDateRange([today.subtract(29, "day"), today]);
    }
  };

  const handleDateRangeChange = (
    dates: null | [Dayjs | null, Dayjs | null],
  ) => {
    if (!dates?.[0] || !dates?.[1]) {
      return;
    }

    const [start, end] = dates;
    const today = dayjs();

    if (start.isSame(today, "day") && end.isSame(today, "day")) {
      setTimeRange("day");
    } else if (
      start.isSame(today.subtract(6, "day"), "day") &&
      end.isSame(today, "day")
    ) {
      setTimeRange("week");
    } else if (
      start.isSame(today.subtract(29, "day"), "day") &&
      end.isSame(today, "day")
    ) {
      setTimeRange("month");
    } else {
      setTimeRange("custom");
    }

    setDateRange([start, end]);
  };

  const disabledDate = (current: Dayjs | null): boolean =>
    !!current && current.isAfter(dayjs().startOf("day"), "day");

  const getOverviewMetricValue = (key: string) =>
    overview?.metrics.find((item) => item.key === key)?.value;

  const metricCards = metricDefinitions.map((definition) => ({
    key: definition.key,
    title: definition.title,
    value: formatMetricValue(definition.key, getOverviewMetricValue(definition.key)),
    compare: "",
    trend: "up" as const,
    accent: definition.accent,
    icon: definition.icon,
  }));

  const taskStatus = overview?.task_status ?? [];
  const executionResult = overview?.execution_result ?? [];
  const readStatus = overview?.read_status ?? [];
  const failureReasons = overview?.failure_reasons ?? [];
  const branchTasks = overview?.branch_tasks ?? [];
  const branchExecution = overview?.branch_execution ?? [];
  const branchRead = overview?.branch_read ?? [];

  return (
    <div className={styles.cronOverviewPage}>
      <header className={styles.pageHeader}>
        <div className={styles.toolbar}>
          <div className={styles.toolbarLeft}>
            <div className={styles.titleWrap}>
              <h1>定时任务概览</h1>
              <Info size={18} />
            </div>
          </div>
          <div className={styles.toolbarRight}>
            <div className={styles.segmentedControl}>
              <button
                type="button"
                className={
                  timeRange === "day"
                    ? styles.segmentActive
                    : styles.segmentButton
                }
                onClick={() => handleModeChange("day")}
              >
                今天
              </button>
              <button
                type="button"
                className={
                  timeRange === "week"
                    ? styles.segmentActive
                    : styles.segmentButton
                }
                onClick={() => handleModeChange("week")}
              >
                近7天
              </button>
              <button
                type="button"
                className={
                  timeRange === "month"
                    ? styles.segmentActive
                    : styles.segmentButton
                }
                onClick={() => handleModeChange("month")}
              >
                近30天
              </button>
            </div>

            <div className={styles.dateRangePanel}>
              <DatePicker.RangePicker
                className={styles.rangePicker}
                value={dateRange}
                onChange={handleDateRangeChange}
                format="YYYY-MM-DD"
                suffixIcon={<CalendarDays size={16} />}
                disabledDate={disabledDate}
                allowClear={false}
              />
            </div>
          </div>
        </div>
      </header>

      <SectionTitle>任务总体概览</SectionTitle>
      <Spin spinning={overviewLoading} tip="加载中...">
        <section className={styles.metricGrid}>
          {metricCards.map((metric) => (
            <MetricCard key={metric.key} metric={metric} />
          ))}
        </section>
      </Spin>

      <SectionTitle>任务状态与触达情况</SectionTitle>
      <Spin spinning={overviewLoading} tip="加载中...">
        <section className={styles.statusGrid}>
          <DonutPanel
            title="任务状态分布"
            items={taskStatus}
            centerValue={formatNumber(branchTasks.reduce((sum, item) => sum + item.value, 0))}
            centerLabel="总任务数"
          />
          <DonutPanel
            title="执行结果分布"
            items={executionResult}
            centerValue={formatNumber(executionResult.reduce((sum, item) => sum + item.value, 0))}
            centerLabel="执行次数"
          />
          <Panel
            title="任务失败原因分布"
            action={
              <button
                type="button"
                className={styles.linkButton}
                onClick={() => setFailedTaskModalOpen(true)}
              >
                查看详情
                <ChevronRight size={14} />
              </button>
            }
          >
            <FailureReasonChart items={failureReasons} />
          </Panel>
          <DonutPanel
            title="任务阅读状态分布"
            items={readStatus}
            centerValue={formatNumber(readStatus.reduce((sum, item) => sum + item.value, 0))}
            centerLabel="总任务数"
          />
        </section>
      </Spin>

      <SectionTitle>分行任务执行与触达概况</SectionTitle>
      <Spin spinning={overviewLoading} tip="加载中...">
        <BranchSharedOverview
          branchTasks={branchTasks}
          branchExecution={branchExecution}
          branchRead={branchRead}
        />
      </Spin>

      <FailedTaskModal
        open={failedTaskModalOpen}
        onClose={() => setFailedTaskModalOpen(false)}
        tasks={failedTasks}
        loading={failedTasksLoading}
      />
    </div>
  );
}
