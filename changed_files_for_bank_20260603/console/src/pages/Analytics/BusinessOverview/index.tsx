/**
 * AI平台运营概览 - 业务价值展示页面
 * 用于银行管理层查看平台使用情况和业务覆盖情况
 */
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import type { UIEvent } from "react";
import { useNavigate } from "react-router-dom";
import {
  ArrowUp,
  ArrowUpRight,
  CalendarDays,
  CheckSquare,
  ChevronRight,
  Clock3,
  Coins,
  Database,
  MessageCircleMore,
  RotateCw,
  Sparkles,
  TrendingDown,
  TrendingUp,
  UserRound,
  Users,
  Zap,
} from "lucide-react";
import { DatePicker, Select, Switch, Tooltip, message } from "antd";
import ReactECharts from "echarts-for-react";
import type { Dayjs } from "dayjs";
import dayjs from "dayjs";
import styles from "./index.module.less";
import { htmlPreviewEventsApi } from "../../../api/modules/htmlPreviewEvents";
import type {
  HtmlPreviewCustomerClickItem,
  HtmlPreviewClickSummaryItem,
  HtmlPreviewListSummaryItem,
} from "../../../api/types/htmlPreviewEvents";
import {
  tracingApi,
  type BranchMetricItem,
  type ErrorSummary,
  type OverviewStats,
  type SkillUsage,
  type TaskStatusSummary,
  type DepthSummary,
} from "../../../api/modules/tracing";
import UserDetailModal from "./components/UserDetailModal";
import SkillDetailModal from "./components/SkillDetailModal";
import { BBK_ID_MAP, BBK_ID_TO_NAME_MAP, getBbkDisplayName } from "../../../constants/bbk";
import {
  formatChange,
  formatDuration,
  formatNumber,
  formatPercent,
  formatTokens,
  truncateName,
  toChangeDirection,
  type BreakdownItem,
  type DepthStatCard,
  type OverviewMetricCard,
  type SummaryLegendItem,
  type TimeRange,
  type TrendDatum,
  type UserRow,
} from "./types";
const { Option } = Select;

const METRIC_ACCENT_COLORS = [
  "#2563eb",
  "#22c55e",
  "#06b6d4",
  "#f97316",
  "#7c3aed",
];

const DONUT_COLORS = ["#18b368", "#ef4444", "#94a3b8"];
const safeNumber = (value: unknown): number =>
  typeof value === "number" && !Number.isNaN(value) ? value : 0;

const iconMap = {
  users: UserRound,
  conversations: MessageCircleMore,
  sessions: CheckSquare,
  tokens: Coins,
  skills: Zap,
};

function mapBreakdown(
  rows: BranchMetricItem[] | undefined,
  formatter?: (value: number) => string,
): BreakdownItem[] | null {
  const mapped = (rows || []).slice(0, 5).map((item) => ({
    name: item.bbk_name || item.bbk_id || "-",
    value: Math.max(item.percent || 0, 8),
    valueText: formatter
      ? formatter(safeNumber(item.value))
      : formatPercent(item.percent || 0),
  }));

  // 无真实数据时返回 null，由渲染层显示空状态
  if (mapped.length === 0) {
    return null;
  }

  return mapped;
}

function buildMetricCards(
  overviewStats: OverviewStats | null,
  taskStatusSummary: TaskStatusSummary | null,
  growthStats: {
    callsGrowth: number | null;
    tokensGrowth: number | null;
    sessionGrowth: number | null;
    userGrowth: number | null;
    skillGrowth: number | null;
    cronGrowth: number | null;
  },
): OverviewMetricCard[] {
  return [
    {
      key: "users",
      title: "活跃用户数",
      valueText: (
        <span className={styles.userValueWrap}>
          <span className={styles.userTotal}>{formatNumber(overviewStats?.total_users ?? 0)}</span>
          <span className={styles.userAnnotation}>
            <span className={styles.annotationRow}>
              <span className={styles.annotationDot} style={{ background: "#6366f1" }} />
              IT人员 {formatNumber(overviewStats?.it_users ?? 0)}
            </span>
            <span className={styles.annotationRow}>
              <span className={styles.annotationDot} style={{ background: "#22c55e" }} />
              业务人员 {formatNumber(overviewStats?.business_users ?? 0)}
            </span>
          </span>
        </span>
      ),
      changeText: formatChange(growthStats.userGrowth),
      changeDirection: toChangeDirection(growthStats.userGrowth),
      accentColor: METRIC_ACCENT_COLORS[0],
      breakdown: mapBreakdown(overviewStats?.branch_breakdown?.users),
    },
    {
      key: "sessions",
      title: "总会话数",
      valueText: formatNumber(overviewStats?.total_sessions ?? 0),
      changeText: formatChange(growthStats.sessionGrowth),
      changeDirection: toChangeDirection(growthStats.sessionGrowth),
      accentColor: METRIC_ACCENT_COLORS[1],
      breakdown: mapBreakdown(overviewStats?.branch_breakdown?.sessions),
    },
    {
      key: "cron_tasks",
      title: "定时任务数",
      valueText: (
        <>
          {formatNumber(taskStatusSummary?.total_tasks ?? 0)}
          <span className={styles.newCronHint}>
            <ArrowUp size={10} className={styles.newCronIcon} />新增：{formatNumber(taskStatusSummary?.new_cron_tasks ?? 0)}个
          </span>
        </>
      ),
      changeText: formatChange(growthStats.cronGrowth),
      changeDirection: toChangeDirection(growthStats.cronGrowth),
      accentColor: METRIC_ACCENT_COLORS[2],
      breakdown: mapBreakdown(overviewStats?.branch_breakdown?.cron_tasks),
    },
    {
      key: "tokens",
      title: "资源消耗",
      valueText: formatTokens(overviewStats?.total_tokens ?? 0),
      changeText: formatChange(growthStats.tokensGrowth),
      changeDirection: toChangeDirection(growthStats.tokensGrowth),
      accentColor: METRIC_ACCENT_COLORS[3],
      breakdown: mapBreakdown(overviewStats?.branch_breakdown?.tokens),
    },
    {
      key: "skills",
      title: "技能调用次数",
      valueText: formatNumber(overviewStats?.total_skill_calls ?? 0),
      changeText: formatChange(growthStats.skillGrowth),
      changeDirection: toChangeDirection(growthStats.skillGrowth),
      accentColor: METRIC_ACCENT_COLORS[4],
      breakdown: mapBreakdown(overviewStats?.branch_breakdown?.skills),
    },
  ];
}

function buildDepthCards(
  summary: DepthSummary | null,
  growthStats: {
    avgRoundsGrowth: number | null;
    multiRoundRatioGrowth: number | null;
    avgDurationGrowth: number | null;
    avgSessionsPerUserGrowth: number | null;
  },
): DepthStatCard[] {
  return [
    {
      key: "avg-rounds",
      title: "单次会话平均轮数",
      valueText: safeNumber(summary?.avg_rounds).toFixed(1),
      changeText: formatChange(growthStats.avgRoundsGrowth),
      changeDirection: toChangeDirection(growthStats.avgRoundsGrowth),
    },
    {
      key: "multi-round",
      title: "多轮会话占比(>3轮)",
      valueText: formatPercent(safeNumber(summary?.multi_round_ratio)),
      changeText: formatChange(growthStats.multiRoundRatioGrowth),
      changeDirection: toChangeDirection(growthStats.multiRoundRatioGrowth),
    },
    {
      key: "avg-duration",
      title: "平均对话时长",
      valueText: formatDuration(safeNumber(summary?.avg_duration_seconds)),
      changeText: formatChange(growthStats.avgDurationGrowth),
      changeDirection: toChangeDirection(growthStats.avgDurationGrowth),
    },
    {
      key: "avg-sessions",
      title: "人均会话数",
      valueText: safeNumber(summary?.avg_sessions_per_user).toFixed(1),
      changeText: formatChange(growthStats.avgSessionsPerUserGrowth),
      changeDirection: toChangeDirection(growthStats.avgSessionsPerUserGrowth),
    },
  ];
}

function buildExecutionSummary(
  summary: TaskStatusSummary | null,
): SummaryLegendItem[] {
  return [
    {
      key: "success",
      label: "成功",
      value: safeNumber(summary?.success),
      color: DONUT_COLORS[0],
    },
    {
      key: "failed",
      label: "失败",
      value: safeNumber(summary?.failed),
      color: DONUT_COLORS[1],
    },
    {
      key: "cancelled",
      label: "已取消/跳过",
      value: safeNumber(summary?.cancelled),
      color: DONUT_COLORS[2],
    },
  ];
}

function buildErrorSummary(summary: ErrorSummary | null): SummaryLegendItem[] {
  return [
    {
      key: "model-error",
      label: "模型报错",
      value: safeNumber(summary?.model_errors),
      color: "#f59e0b",
    },
    {
      key: "tool-error",
      label: "工具报错",
      value: safeNumber(summary?.tool_errors),
      color: "#ef4444",
    },
  ];
}

function buildDonutSegments(items: SummaryLegendItem[]) {
  const total = Math.max(
    items.reduce((sum, item) => sum + item.value, 0),
    1,
  );
  let offset = 0;

  return items.map((item) => {
    const fraction = item.value / total;
    const segment = {
      ...item,
      dasharray: `${fraction * 283} 283`,
      dashoffset: -offset,
    };
    offset += fraction * 283;
    return segment;
  });
}

/** 漏斗图组件：使用 echarts 展示任务执行转化率 */
function TaskFunnel({ taskStatusSummary }: { taskStatusSummary: TaskStatusSummary | null }) {
  const totalTasks = safeNumber(taskStatusSummary?.total_tasks);
  const successCount = safeNumber(taskStatusSummary?.success);
  const readCount = safeNumber(taskStatusSummary?.read_count);

  if (totalTasks === 0) {
    return (
      <div className={styles.emptyBreakdown}>
        <Database className={styles.emptyBreakdownIcon} />
        <span className={styles.emptyBreakdownText}>暂无任务数据</span>
      </div>
    );
  }

  const successRate = ((successCount / totalTasks) * 100).toFixed(1);
  const readRate = successCount > 0 ? ((readCount / successCount) * 100).toFixed(1) : "0.0";

  // 值为 0 时保证有最小值显示
  const minBar = Math.max(totalTasks * 0.12, 1);
  const ensureVisible = (v: number) => (v <= minBar ? minBar : v);

  const funnelColors = ["#4f46e5", "#16a34a", "#0891b2"];

  const chartData = [
    { name: "总任务数", value: ensureVisible(totalTasks), rawValue: totalTasks },
    { name: "执行成功数", value: ensureVisible(successCount), rawValue: successCount },
    { name: "已读数", value: ensureVisible(readCount), rawValue: readCount },
  ];

  const option = {
    tooltip: {
      trigger: "item",
      formatter: (params: { name: string; data: { rawValue: number } }) =>
        `${params.name}: ${formatNumber(params.data.rawValue)}`,
    },
    legend: {
      data: chartData.map((item) => item.name),
      top: 0,
      itemWidth: 10,
      itemHeight: 10,
      itemGap: 16,
      textStyle: {
        color: "#475569",
        fontSize: 12,
        fontWeight: 500,
      },
    },
    grid: {
      left: "5%",
      right: "35%",
      top: "10%",
      bottom: "15%",
    },
    xAxis: { show: false, type: "value" },
    yAxis: { show: false, type: "category" },
    series: [
      {
        type: "funnel",
        left: "5%",
        right: "40%",
        top: "15%",
        bottom: "10%",
        min: 0,
        max: totalTasks,
        minSize: "35%",
        maxSize: "100%",
        sort: "descending",
        gap: 2,
        label: {
          show: true,
          position: "inside",
          formatter: (params: { name: string; data: { rawValue: number } }) =>
            `${params.name}  ${formatNumber(params.data.rawValue)}`,
          color: "#fff",
          fontSize: 12,
          fontWeight: 600,
        },
        itemStyle: {
          borderWidth: 0,
        },
        data: chartData.map((item, index) => ({
          name: item.name,
          value: item.value,
          rawValue: item.rawValue,
          itemStyle: { color: funnelColors[index] },
        })),
      },
    ],
    // 右侧转化率标注
    graphic: [
      // 第一层到第二层的转化率
      {
        type: "group",
        left: "68%",
        top: "28%",
        children: [
          {
            type: "circle",
            shape: { cx: 3, cy: 0, r: 3 },
            style: { fill: "#94a3b8" },
          },
          {
            type: "line",
            shape: { x1: 3, y1: 0, x2: 3, y2: 30 },
            style: { stroke: "#94a3b8", lineWidth: 1, lineDash: [3, 2] },
          },
          {
            type: "circle",
            shape: { cx: 3, cy: 30, r: 3 },
            style: { fill: "#94a3b8" },
          },
          {
            type: "text",
            style: {
              text: `→ ${successRate}%`,
              x: 12,
              y: 15,
              fill: "#64748b",
              fontSize: 11,
              fontWeight: 500,
            },
          },
        ],
      },
      // 第二层到第三层的转化率
      {
        type: "group",
        left: "68%",
        top: "56%",
        children: [
          {
            type: "circle",
            shape: { cx: 3, cy: 0, r: 3 },
            style: { fill: "#94a3b8" },
          },
          {
            type: "line",
            shape: { x1: 3, y1: 0, x2: 3, y2: 30 },
            style: { stroke: "#94a3b8", lineWidth: 1, lineDash: [3, 2] },
          },
          {
            type: "circle",
            shape: { cx: 3, cy: 30, r: 3 },
            style: { fill: "#94a3b8" },
          },
          {
            type: "text",
            style: {
              text: `→ ${readRate}%`,
              x: 12,
              y: 15,
              fill: "#64748b",
              fontSize: 11,
              fontWeight: 500,
            },
          },
        ],
      },
    ],
  };

  return (
    <div className={styles.funnelWrap}>
      <ReactECharts option={option} style={{ height: 160 }} />
    </div>
  );
}

function getLabelInterval(dataLength: number): number {
  if (dataLength <= 7) return 1;
  if (dataLength <= 14) return 2;
  if (dataLength <= 24) return 3;
  return 4;
}

function getBarWidth(dataLength: number, step: number): number {
  const maxWidth = Math.floor(step * 0.6);
  if (dataLength <= 7) return Math.min(20, maxWidth);
  if (dataLength <= 14) return Math.min(12, maxWidth);
  if (dataLength <= 24) return Math.min(8, maxWidth);
  return Math.max(4, Math.min(5, maxWidth));
}

interface TrendAxisTick {
  value: number;
  label: string;
}

interface TrendHoverZone {
  key: string;
  label: string;
  users: number;
  calls: number;
  x: number;
  y: number;
  width: number;
  height: number;
  pointX: number;
  pointY: number;
}

function getNiceAxisMax(value: number): number {
  if (value <= 0) {
    return 0;
  }
  const magnitude = 10 ** Math.floor(Math.log10(value));
  const normalized = value / magnitude;
  if (normalized <= 1) return magnitude;
  if (normalized <= 2) return 2 * magnitude;
  if (normalized <= 5) return 5 * magnitude;
  return 10 * magnitude;
}

function formatTrendAxisLabel(value: number, axisMax: number): string {
  if (value === 0) {
    return "0";
  }
  if (axisMax >= 10000) {
    const scaled = value / 10000;
    return `${scaled.toFixed(Number.isInteger(scaled) ? 0 : 1)}W`;
  }
  if (axisMax >= 1000) {
    const scaled = value / 1000;
    return `${scaled.toFixed(Number.isInteger(scaled) ? 0 : 1)}K`;
  }
  return formatNumber(value);
}

function buildTrendAxisTicks(axisMax: number): TrendAxisTick[] {
  if (axisMax <= 0) {
    return Array.from({ length: 6 }, () => ({
      value: 0,
      label: "0",
    }));
  }
  const step = axisMax / 5;
  return Array.from({ length: 6 }, (_, index) => {
    const value = step * (5 - index);
    return {
      value,
      label: formatTrendAxisLabel(value, axisMax),
    };
  });
}

export function buildTrendSvgData(trendData: TrendDatum[]) {
  const width = 428;
  const height = 244;
  const chartLeft = 34;
  const chartRight = 34;
  const chartTop = 18;
  const chartBottom = 34;
  const chartWidth = width - chartLeft - chartRight;
  const chartHeight = height - chartTop - chartBottom;
  const rawMaxCalls = Math.max(
    ...trendData.map((item) => safeNumber(item.calls)),
    0,
  );
  const rawMaxUsers = Math.max(
    ...trendData.map((item) => safeNumber(item.users)),
    0,
  );
  const maxCalls = Math.max(
    rawMaxCalls,
    1,
  );
  const maxUsers = Math.max(
    rawMaxUsers,
    1,
  );
  const leftAxisMax = getNiceAxisMax(rawMaxUsers);
  const rightAxisMax = getNiceAxisMax(rawMaxCalls);
  const step = trendData.length > 1 ? chartWidth / (trendData.length - 1) : 0;
  const labelInterval = getLabelInterval(trendData.length);
  const barWidth = getBarWidth(trendData.length, step);

  const bars = trendData.map((item, index) => {
    const barHeight = (safeNumber(item.users) / maxUsers) * (chartHeight - 8);
    const x = chartLeft + index * step - barWidth / 2;
    const label = item.date.includes(":")
      ? dayjs(item.date).format("HH:mm")
      : dayjs(item.date).format("MM-DD");
    return {
      key: item.date,
      x,
      y: chartTop + chartHeight - barHeight,
      height: barHeight,
      width: barWidth,
      label,
      showLabel: index % labelInterval === 0,
    };
  });

  const points = trendData.map((item, index) => {
    const x = chartLeft + index * step;
    const y =
      chartTop +
      chartHeight -
      (safeNumber(item.calls) / maxCalls) * chartHeight;
    return { x, y };
  });

  const hoverZones: TrendHoverZone[] = trendData.map((item, index) => {
    const pointX = points[index]?.x ?? chartLeft;
    const pointY = points[index]?.y ?? chartTop + chartHeight;
    const zoneStart =
      trendData.length === 1
        ? chartLeft
        : index === 0
        ? chartLeft
        : pointX - step / 2;
    const zoneEnd =
      trendData.length === 1
        ? width - chartRight
        : index === trendData.length - 1
        ? width - chartRight
        : pointX + step / 2;

    return {
      key: item.date,
      label: bars[index]?.label ?? item.date,
      users: safeNumber(item.users),
      calls: safeNumber(item.calls),
      x: zoneStart,
      y: chartTop,
      width: Math.max(zoneEnd - zoneStart, barWidth),
      height: chartHeight,
      pointX,
      pointY,
    };
  });

  return {
    width,
    height,
    chartLeft,
    chartRight,
    chartTop,
    chartBottom,
    chartHeight,
    leftAxisTicks: buildTrendAxisTicks(leftAxisMax),
    rightAxisTicks: buildTrendAxisTicks(rightAxisMax),
    bars,
    points,
    hoverZones,
    polyline: points.map((point) => `${point.x},${point.y}`).join(" "),
  };
}

export default function BusinessOverviewPage() {
  const navigate = useNavigate();

  const [timeRange, setTimeRange] = useState<TimeRange>("day");
  const [dateRange, setDateRange] = useState<[Dayjs, Dayjs]>([dayjs(), dayjs()]);
  // 管理员多选分行；非管理员使用用户所属分行
  const [bbkIds, setBbkIds] = useState<string[]>([]);

  const [overviewStats, setOverviewStats] = useState<OverviewStats | null>(
    null,
  );
  const [growthStats, setGrowthStats] = useState<{
    callsGrowth: number | null;
    tokensGrowth: number | null;
    sessionGrowth: number | null;
    userGrowth: number | null;
    skillGrowth: number | null;
    cronGrowth: number | null;
    avgRoundsGrowth: number | null;
    multiRoundRatioGrowth: number | null;
    avgDurationGrowth: number | null;
    avgSessionsPerUserGrowth: number | null;
  }>({
    callsGrowth: null,
    tokensGrowth: null,
    sessionGrowth: null,
    userGrowth: null,
    skillGrowth: null,
    cronGrowth: null,
    avgRoundsGrowth: null,
    multiRoundRatioGrowth: null,
    avgDurationGrowth: null,
    avgSessionsPerUserGrowth: null,
  });
  const [trendData, setTrendData] = useState<TrendDatum[]>([]);
  const [activeUsers, setActiveUsers] = useState<UserRow[]>([]);
  const [activePage, setActivePage] = useState(1);
  const [activeHasMore, setActiveHasMore] = useState(true);
  const [activeLoading, setActiveLoading] = useState(false);
  const activeLoadingRef = useRef(false);
  // 用户过滤类型：filtered(过滤IT人员) / all(全部用户)
  const [activeFilterType, setActiveFilterType] = useState<"filtered" | "all">("all");
  const [skills, setSkills] = useState<SkillUsage[]>([]);
  const [skillsPage, setSkillsPage] = useState(1);
  const [skillsHasMore, setSkillsHasMore] = useState(true);
  const [skillsLoading, setSkillsLoading] = useState(false);
  const skillsLoadingRef = useRef(false);
  const [errorSummaryData, setErrorSummaryData] = useState<ErrorSummary | null>(null);
  const [taskStatusSummary, setTaskStatusSummary] =
    useState<TaskStatusSummary | null>(null);
  const [depthSummary, setDepthSummary] = useState<DepthSummary | null>(null);
  const [htmlPreviewClicks, setHtmlPreviewClicks] = useState<
    HtmlPreviewClickSummaryItem[]
  >([]);
  const [htmlPreviewLists, setHtmlPreviewLists] = useState<
    HtmlPreviewListSummaryItem[]
  >([]);
  const [htmlPreviewCustomerClicks, setHtmlPreviewCustomerClicks] = useState<
    HtmlPreviewCustomerClickItem[]
  >([]);
  const [selectedHtmlPreviewListKey, setSelectedHtmlPreviewListKey] =
    useState<string>("all");
  const [includeUnclickedCustomers, setIncludeUnclickedCustomers] =
    useState(false);
  const [htmlPreviewLoading, setHtmlPreviewLoading] = useState(false);
  const [errorLoading, setErrorLoading] = useState(false);
  const errorLoadingRef = useRef(false);
  const [modalOpen, setModalOpen] = useState(false);
  const [selectedUserId, setSelectedUserId] = useState<string | null>(null);
  const [selectedUserName, setSelectedUserName] = useState<string | null>(null);
  const [skillModalOpen, setSkillModalOpen] = useState(false);
  const [selectedSkillName, setSelectedSkillName] = useState("");
  const [activeTrendIndex, setActiveTrendIndex] = useState<number | null>(null);

  const startDateText = useMemo(
    () => dateRange[0].format("YYYY-MM-DD"),
    [dateRange],
  );
  const endDateText = useMemo(
    () => dateRange[1].format("YYYY-MM-DD"),
    [dateRange],
  );
  // 分行筛选参数：直接使用 UI 选择的 bbkIds，空数组表示全部分行
  const effectiveBbkIds = useMemo(() => {
    return bbkIds.length === 0 ? undefined : bbkIds;
  }, [bbkIds]);

  const transformUserData = useCallback(
    (items: Record<string, unknown>[]): UserRow[] =>
      items.map((item) => ({
        userId: String(item.user_id || ""),
        userName: item.user_name ? String(item.user_name) : undefined,
        bbkId: item.bbk_id ? String(item.bbk_id) : undefined,
        name: String(item.user_name || item.user_id || "-"),
        calls: safeNumber(item.total_conversations),
        tokens: safeNumber(item.total_tokens),
        lastActive: item.last_active
          ? dayjs(String(item.last_active)).format("YYYY-MM-DD HH:mm")
          : "-",
      })),
    [],
  );

  const fetchDashboard = useCallback(async () => {
    const isSingleDay = dateRange[0].isSame(dateRange[1], "day");

    try {
      const [overviewRes, growthRes, trendRes] = await Promise.allSettled([
        tracingApi.getOverview(
          startDateText,
          endDateText,
          effectiveBbkIds?.join(","),
        ),
        tracingApi.getGrowthStats(
          startDateText,
          endDateText,
          timeRange,
          effectiveBbkIds?.join(","),
        ),
        isSingleDay
          ? tracingApi.getHourlyTrend(
              startDateText,
              endDateText,
              effectiveBbkIds?.join(","),
            )
          : tracingApi.getDailyTrend(
              startDateText,
              endDateText,
              effectiveBbkIds?.join(","),
            ),
      ]);

      if (overviewRes.status === "fulfilled") {
        setOverviewStats(overviewRes.value);
      }
      if (growthRes.status === "fulfilled") {
        setGrowthStats(growthRes.value);
      }
      if (trendRes.status === "fulfilled") {
        setTrendData(trendRes.value.trendData || []);
      }
    } catch (error) {
      console.error("Failed to fetch dashboard:", error);
      message.error("获取总览数据失败");
    }
  }, [
    dateRange,
    effectiveBbkIds,
    endDateText,
    startDateText,
    timeRange,
  ]);

  const fetchActiveUsers = useCallback(
    async (page: number, append = false) => {
      if (activeLoadingRef.current) {
        return;
      }
      activeLoadingRef.current = true;
      setActiveLoading(true);

      try {
        const result = await tracingApi.getUsers(page, 10, {
          start_date: startDateText,
          end_date: endDateText,
          bbk_ids: effectiveBbkIds?.join(","),
          sort_by: "conversations",
          filter_user_type: activeFilterType,
        });
        const mappedUsers = transformUserData(
          result.items as unknown as Record<string, unknown>[],
        );
        setActiveUsers((previous) =>
          append ? [...previous, ...mappedUsers] : mappedUsers,
        );
        setActiveHasMore(mappedUsers.length === 10);
      } catch (error) {
        console.error("Failed to fetch active users:", error);
      } finally {
        activeLoadingRef.current = false;
        setActiveLoading(false);
      }
    },
    [effectiveBbkIds, endDateText, startDateText, transformUserData, activeFilterType],
  );

  const fetchSkills = useCallback(
    async (page: number = 1, append: boolean = false) => {
      if (skillsLoadingRef.current) {
        return;
      }
      skillsLoadingRef.current = true;
      setSkillsLoading(true);

      try {
        const pageSize = 10;
        const result = await tracingApi.getSkills(page, pageSize, {
          start_date: startDateText,
          end_date: endDateText,
          bbk_ids: effectiveBbkIds?.join(","),
        });
        const rows = result.items || [];

        if (append) {
          setSkills(prev => [...prev, ...rows]);
        } else {
          setSkills(rows);
        }

        // 如果返回的数据少于 pageSize，说明没有更多数据了
        setSkillsHasMore(rows.length >= pageSize);
      } catch (error) {
        console.error("Failed to fetch skills:", error);
      } finally {
        skillsLoadingRef.current = false;
        setSkillsLoading(false);
      }
    },
    [effectiveBbkIds, endDateText, startDateText],
  );

  const fetchErrorSummary = useCallback(
    async () => {
      if (errorLoadingRef.current) {
        return;
      }
      errorLoadingRef.current = true;
      setErrorLoading(true);

      try {
        const result = await tracingApi.getErrorSummary({
          start_date: startDateText,
          end_date: endDateText,
          bbk_ids: effectiveBbkIds?.join(","),
        });
        setErrorSummaryData(result);
      } catch (error) {
        console.error("Failed to fetch error summary:", error);
      } finally {
        errorLoadingRef.current = false;
        setErrorLoading(false);
      }
    },
    [effectiveBbkIds, endDateText, startDateText],
  );

  const fetchTaskStatusSummary = useCallback(async () => {
    try {
      const result = await tracingApi.getTaskStatusSummary({
        start_date: startDateText,
        end_date: endDateText,
        bbk_ids: effectiveBbkIds?.join(","),
      });
      setTaskStatusSummary(result);
    } catch (error) {
      console.error("Failed to fetch task status summary:", error);
    }
  }, [effectiveBbkIds, endDateText, startDateText]);

  const fetchDepthSummary = useCallback(async () => {
    try {
      const result = await tracingApi.getDepthSummary({
        start_date: startDateText,
        end_date: endDateText,
        bbk_ids: effectiveBbkIds?.join(","),
      });
      setDepthSummary(result);
    } catch (error) {
      console.error("Failed to fetch depth summary:", error);
    }
  }, [effectiveBbkIds, endDateText, startDateText]);

  const fetchHtmlPreviewClicks = useCallback(async () => {
    setHtmlPreviewLoading(true);
    try {
      const params = {
        startTime: dateRange[0].startOf("day").toISOString(),
        endTime: dateRange[1].endOf("day").toISOString(),
        bbkIds: effectiveBbkIds?.join(","),
        limit: 100,
      };
      const selectedListKey =
        selectedHtmlPreviewListKey === "all"
          ? null
          : selectedHtmlPreviewListKey;
      const detailParams = {
        ...params,
        listKey: selectedListKey,
      };
      const [summaryResult, listResult, customerResult] = await Promise.allSettled([
        htmlPreviewEventsApi.getSummary(detailParams),
        htmlPreviewEventsApi.getLists(params),
        htmlPreviewEventsApi.getCustomerClicks({
          ...detailParams,
          includeUnclicked: includeUnclickedCustomers,
          limit: 500,
        }),
      ]);
      if (summaryResult.status === "fulfilled") {
        setHtmlPreviewClicks(summaryResult.value.items || []);
      } else {
        console.error(
          "Failed to fetch HTML preview click summary:",
          summaryResult.reason,
        );
        setHtmlPreviewClicks([]);
      }
      if (listResult.status === "fulfilled") {
        setHtmlPreviewLists(listResult.value.items || []);
      } else {
        console.error(
          "Failed to fetch HTML preview list summary:",
          listResult.reason,
        );
        setHtmlPreviewLists([]);
      }
      if (customerResult.status === "fulfilled") {
        setHtmlPreviewCustomerClicks(customerResult.value.items || []);
      } else {
        console.error(
          "Failed to fetch HTML preview customer clicks:",
          customerResult.reason,
        );
        setHtmlPreviewCustomerClicks([]);
      }
    } catch (error) {
      console.error("Failed to fetch HTML preview click statistics:", error);
      setHtmlPreviewClicks([]);
      setHtmlPreviewLists([]);
      setHtmlPreviewCustomerClicks([]);
    } finally {
      setHtmlPreviewLoading(false);
    }
  }, [
    dateRange,
    effectiveBbkIds,
    includeUnclickedCustomers,
    selectedHtmlPreviewListKey,
  ]);

  useEffect(() => {
    fetchDashboard();
    fetchSkills();
    fetchErrorSummary();
    fetchTaskStatusSummary();
    fetchDepthSummary();
    fetchHtmlPreviewClicks();
    // 活跃用户请求由独立的 useEffect 处理
  }, [
    fetchDashboard,
    fetchDepthSummary,
    fetchErrorSummary,
    fetchHtmlPreviewClicks,
    fetchSkills,
    fetchTaskStatusSummary,
  ]);

  useEffect(() => {
    if (
      selectedHtmlPreviewListKey !== "all" &&
      !htmlPreviewLists.some((item) => item.list_key === selectedHtmlPreviewListKey)
    ) {
      setSelectedHtmlPreviewListKey("all");
    }
  }, [htmlPreviewLists, selectedHtmlPreviewListKey]);

  // 活跃用户请求独立处理，避免 activeFilterType 变化触发其他请求
  useEffect(() => {
    setActivePage(1);
    fetchActiveUsers(1, false);
  }, [
    fetchActiveUsers,
  ]);

  useEffect(() => {
    setActiveTrendIndex(null);
  }, [trendData]);

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

  const handleDateRangeChange = (dates: [Dayjs | null, Dayjs | null] | null) => {
    if (!dates || !dates[0] || !dates[1]) {
      return;
    }

    const [start, end] = dates;
    const today = dayjs().startOf("day");

    // 检测是否匹配快捷按钮范围
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

  const handleActiveScroll = useCallback(
    (event: UIEvent<HTMLDivElement>) => {
      const target = event.currentTarget;
      if (
        target.scrollHeight - target.scrollTop <= target.clientHeight + 40 &&
        activeHasMore &&
        !activeLoadingRef.current
      ) {
        const nextPage = activePage + 1;
        setActivePage(nextPage);
        fetchActiveUsers(nextPage, true);
      }
    },
    [activeHasMore, activePage, fetchActiveUsers],
  );

  const handleSkillsScroll = useCallback(
    (event: UIEvent<HTMLDivElement>) => {
      const target = event.currentTarget;
      if (
        target.scrollHeight - target.scrollTop <= target.clientHeight + 40 &&
        skillsHasMore &&
        !skillsLoadingRef.current
      ) {
        const nextPage = skillsPage + 1;
        setSkillsPage(nextPage);
        fetchSkills(nextPage, true);
      }
    },
    [skillsHasMore, skillsPage, fetchSkills],
  );

  const disabledDate = (current: Dayjs | null): boolean =>
    !!current && current.isAfter(dayjs().startOf("day"), "day");

  const metricCards = useMemo(
    () => buildMetricCards(overviewStats, taskStatusSummary, growthStats),
    [growthStats, overviewStats, taskStatusSummary],
  );
  const depthCards = useMemo(
    () => buildDepthCards(depthSummary, growthStats),
    [growthStats, depthSummary],
  );
  const executionSummary = useMemo(
    () => buildExecutionSummary(taskStatusSummary),
    [taskStatusSummary],
  );
  const errorSummaryItems = useMemo(
    () => buildErrorSummary(errorSummaryData),
    [errorSummaryData],
  );
  const selectedHtmlPreviewList = useMemo(
    () =>
      htmlPreviewLists.find(
        (item) => item.list_key === selectedHtmlPreviewListKey,
      ) || null,
    [htmlPreviewLists, selectedHtmlPreviewListKey],
  );
  const displayedHtmlPreviewLists = useMemo(
    () =>
      selectedHtmlPreviewListKey === "all"
        ? htmlPreviewLists
        : htmlPreviewLists.filter(
            (item) => item.list_key === selectedHtmlPreviewListKey,
          ),
    [htmlPreviewLists, selectedHtmlPreviewListKey],
  );
  const htmlPreviewListCount = displayedHtmlPreviewLists.length;
  const htmlPreviewInsightClicks = useMemo(
    () =>
      displayedHtmlPreviewLists.reduce(
        (sum, item) => sum + safeNumber(item.insight_count),
        0,
      ),
    [displayedHtmlPreviewLists],
  );
  const htmlPreviewPhoneClicks = useMemo(
    () =>
      displayedHtmlPreviewLists.reduce(
        (sum, item) => sum + safeNumber(item.phone_count),
        0,
      ),
    [displayedHtmlPreviewLists],
  );
  const htmlPreviewTotalClicks = useMemo(
    () =>
      displayedHtmlPreviewLists.reduce(
        (sum, item) => sum + safeNumber(item.total_click_count),
        0,
      ),
    [displayedHtmlPreviewLists],
  );
  const htmlPreviewMaxActionClicks = Math.max(
    htmlPreviewInsightClicks,
    htmlPreviewPhoneClicks,
    1,
  );
  const htmlPreviewActionTotal =
    htmlPreviewInsightClicks + htmlPreviewPhoneClicks;
  const htmlPreviewInsightPercent =
    htmlPreviewActionTotal > 0
      ? Math.round((htmlPreviewInsightClicks / htmlPreviewActionTotal) * 100)
      : 0;
  const htmlPreviewPhonePercent =
    htmlPreviewActionTotal > 0
      ? Math.round((htmlPreviewPhoneClicks / htmlPreviewActionTotal) * 100)
      : 0;
  const htmlPreviewCustomerTotal = useMemo(
    () =>
      displayedHtmlPreviewLists.reduce(
        (sum, item) => sum + safeNumber(item.customer_count),
        0,
      ),
    [displayedHtmlPreviewLists],
  );
  const htmlPreviewClickedCustomerCount = useMemo(
    () =>
      displayedHtmlPreviewLists.reduce(
        (sum, item) => sum + safeNumber(item.clicked_customer_count),
        0,
      ),
    [displayedHtmlPreviewLists],
  );
  const htmlPreviewLatestClick = useMemo(() => {
    const sortedClickTimes = htmlPreviewClicks
      .map((item) => item.last_clicked_at)
      .filter(Boolean)
      .sort();
    const latest = sortedClickTimes[sortedClickTimes.length - 1];
    return latest ? dayjs(latest).format("YYYY-MM-DD HH:mm") : "-";
  }, [htmlPreviewClicks]);
  const hasHtmlPreviewAnalysisData =
    htmlPreviewClicks.length > 0 ||
    htmlPreviewLists.length > 0 ||
    htmlPreviewCustomerClicks.length > 0;
  const trendSvg = useMemo(() => buildTrendSvgData(trendData), [trendData]);
  const activeTrendZone =
    activeTrendIndex === null ? null : trendSvg.hoverZones[activeTrendIndex] ?? null;
  const trendTooltipStyle = activeTrendZone
    ? {
        left: `${Math.min(92, Math.max(8, (activeTrendZone.pointX / trendSvg.width) * 100))}%`,
        top: `${Math.min(78, Math.max(10, (activeTrendZone.pointY / trendSvg.height) * 100))}%`,
      }
    : undefined;
  return (
    <div className={styles.businessOverviewPage}>
      <header className={styles.pageHeader}>
        <div className={styles.toolbar}>
          <div className={styles.toolbarLeft}>
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

          <div className={styles.toolbarRight}>
            <Select
              className={styles.scopeSelect}
              mode="multiple"
              value={bbkIds}
              onChange={setBbkIds}
              placeholder="全部分行"
              maxTagCount="responsive"
              maxTagPlaceholder={(omittedValues) => (
                <Tooltip
                  title={omittedValues
                    .map((item) => {
                      const value = String(item.value ?? "");
                      return BBK_ID_TO_NAME_MAP[value] || value;
                    })
                    .join("、")}
                >
                  <span>+{omittedValues.length} 个分行</span>
                </Tooltip>
              )}
              allowClear
            >
              {BBK_ID_MAP.map((item) => (
                <Option key={item.value} value={item.value}>
                  {item.label}
                </Option>
              ))}
            </Select>
            <button
              type="button"
              className={styles.refreshButton}
              onClick={() => {
                fetchDashboard();
                fetchActiveUsers(1, false);
                fetchSkills();
                fetchErrorSummary();
                fetchTaskStatusSummary();
                fetchDepthSummary();
                fetchHtmlPreviewClicks();
              }}
            >
              <RotateCw size={16} />
              刷新
            </button>
          </div>
        </div>
      </header>

      <section className={styles.metricGrid} data-testid="overview-metric-grid">
        {metricCards.map((card) => {
          const MetricIcon =
            iconMap[card.key as keyof typeof iconMap] || Sparkles;

          return (
            <article
              key={card.key}
              className={styles.metricPanel}
              data-testid="overview-metric-card"
            >
              <div className={styles.metricHeader}>
                <span
                  className={styles.metricIcon}
                  style={{
                    background: `linear-gradient(180deg, ${card.accentColor} 0%, ${card.accentColor}dd 100%)`,
                  }}
                >
                  <MetricIcon size={20} strokeWidth={2.2} />
                </span>
                <div className={styles.metricText}>
                  <div className={styles.metricTitle}>{card.title}</div>
                  <div className={styles.metricValue}>{card.valueText}</div>
                  <div
                    className={
                      card.changeDirection === "up"
                        ? styles.metricChangeUp
                        : card.changeDirection === "down"
                        ? styles.metricChangeDown
                        : styles.metricChangeFlat
                    }
                  >
                    环比
                    {card.changeDirection === "up" && <TrendingUp size={14} />}
                    {card.changeDirection === "down" && <TrendingDown size={14} />}
                    {card.changeText}
                  </div>
                </div>
              </div>
              <div className={styles.breakdownTitle}>Top5分行</div>
              {card.breakdown && card.breakdown.length > 0 ? (
                <div className={styles.breakdownList}>
                  {card.breakdown.map((item) => (
                    <div
                      key={`${card.key}-${item.name}`}
                      className={styles.breakdownRow}
                    >
                      <span className={styles.breakdownName}>
                        {truncateName(item.name, 6)}
                      </span>
                      <div className={styles.breakdownTrack}>
                        <div
                          className={styles.breakdownBar}
                          style={{
                            width: `${Math.max(item.value, 10)}%`,
                            background: card.accentColor,
                          }}
                        />
                      </div>
                      <span className={styles.breakdownValue}>
                        {item.valueText}
                      </span>
                    </div>
                  ))}
                </div>
              ) : (
                <div className={styles.emptyBreakdown}>
                  <Database className={styles.emptyBreakdownIcon} />
                  <span className={styles.emptyBreakdownText}>暂无分行数据</span>
                </div>
              )}
            </article>
          );
        })}
      </section>

      <section
        className={styles.analysisGrid}
        data-testid="overview-analysis-grid"
      >
        <article className={styles.panelLarge}>
          <div className={styles.panelHeader}>
            <h3 className={styles.panelTitle}>调用量趋势</h3>
          </div>
          <div className={styles.trendLegend}>
            <span className={styles.legendItem}>
              <i className={styles.legendBarMark} />
              调用用户
            </span>
            <span className={styles.legendItem}>
              <i className={styles.legendLineMark} />
              调用次数
            </span>
          </div>
          <div className={styles.trendChart}>
            <div className={styles.axisLeft}>
              {trendSvg.leftAxisTicks.map((tick, index) => (
                <span key={`left-${tick.value}-${index}`}>{tick.label}</span>
              ))}
            </div>
            <div
              className={styles.trendPlotArea}
              onMouseLeave={() => {
                setActiveTrendIndex(null);
              }}
            >
              <svg
                viewBox={`0 0 ${trendSvg.width} ${trendSvg.height}`}
                className={styles.trendSvg}
              >
                <defs>
                  <linearGradient
                    id="overviewBarGradient"
                    x1="0%"
                    y1="0%"
                    x2="0%"
                    y2="100%"
                  >
                    <stop offset="0%" stopColor="#4f7fff" />
                    <stop offset="100%" stopColor="#2563eb" />
                  </linearGradient>
                </defs>

                {[0, 1, 2, 3, 4].map((row) => {
                  const y = trendSvg.chartTop + (trendSvg.chartHeight / 4) * row;
                  return (
                    <line
                      key={`grid-${row}`}
                      x1={trendSvg.chartLeft}
                      y1={y}
                      x2={trendSvg.width - trendSvg.chartRight}
                      y2={y}
                      className={styles.gridLine}
                    />
                  );
                })}

                {activeTrendZone && (
                  <line
                    x1={activeTrendZone.pointX}
                    y1={trendSvg.chartTop}
                    x2={activeTrendZone.pointX}
                    y2={trendSvg.chartTop + trendSvg.chartHeight}
                    className={styles.trendGuideLine}
                  />
                )}

                {trendSvg.bars.map((bar, index) => (
                  <g key={bar.key}>
                    <rect
                      x={bar.x}
                      y={bar.y}
                      width={bar.width}
                      height={bar.height}
                      rx="4"
                      fill="url(#overviewBarGradient)"
                      className={
                        activeTrendIndex === index
                          ? styles.trendBarActive
                          : styles.trendBar
                      }
                    />
                    {bar.showLabel && (
                      <text x={bar.x + bar.width / 2} y={233} className={styles.axisLabel}>
                        {bar.label}
                      </text>
                    )}
                  </g>
                ))}

                <polyline
                  points={trendSvg.polyline}
                  className={styles.trendLine}
                />

                {trendSvg.points.map((point, index) => (
                  <circle
                    key={`${point.x}-${point.y}`}
                    cx={point.x}
                    cy={point.y}
                    r={activeTrendIndex === index ? "5.5" : "4.5"}
                    className={
                      activeTrendIndex === index
                        ? styles.trendPointActive
                        : styles.trendPoint
                    }
                  />
                ))}

                {trendSvg.hoverZones.map((zone, index) => (
                  <rect
                    key={`hover-${zone.key}`}
                    data-testid={`trend-hover-zone-${index}`}
                    x={zone.x}
                    y={zone.y}
                    width={zone.width}
                    height={zone.height}
                    className={styles.trendHoverZone}
                    onMouseEnter={() => {
                      setActiveTrendIndex(index);
                    }}
                  />
                ))}
              </svg>

              {activeTrendZone && (
                <div
                  data-testid="trend-tooltip"
                  className={styles.trendTooltip}
                  style={trendTooltipStyle}
                >
                  <div className={styles.trendTooltipDate}>{activeTrendZone.label}</div>
                  <div className={styles.trendTooltipRow}>
                    <span className={styles.trendTooltipLabel}>调用用户</span>
                    <strong className={styles.trendTooltipValue}>
                      {formatNumber(activeTrendZone.users)}
                    </strong>
                  </div>
                  <div className={styles.trendTooltipRow}>
                    <span className={styles.trendTooltipLabel}>调用次数</span>
                    <strong className={styles.trendTooltipValue}>
                      {formatNumber(activeTrendZone.calls)}
                    </strong>
                  </div>
                </div>
              )}
            </div>
            <div className={styles.axisRight}>
              {trendSvg.rightAxisTicks.map((tick, index) => (
                <span key={`right-${tick.value}-${index}`}>{tick.label}</span>
              ))}
            </div>
          </div>
        </article>

        <article className={styles.panelMedium}>
          <div className={styles.panelHeader}>
            <h3 className={styles.panelTitle}>活跃用户排行榜</h3>
            <div className={styles.filterTab}>
              <span
                className={activeFilterType === "all" ? styles.filterTabActive : styles.filterTabItem}
                onClick={() => {
                  if (activeFilterType !== "all") {
                    setActiveFilterType("all");
                    setActiveUsers([]);
                    setActivePage(1);
                    setActiveHasMore(true);
                  }
                }}
              >
                全部
              </span>
              <span
                className={activeFilterType === "filtered" ? styles.filterTabActive : styles.filterTabItem}
                onClick={() => {
                  if (activeFilterType !== "filtered") {
                    setActiveFilterType("filtered");
                    setActiveUsers([]);
                    setActivePage(1);
                    setActiveHasMore(true);
                  }
                }}
              >
                过滤IT人员
              </span>
            </div>
          </div>
          <div className={styles.rankHeader}>
            <span>排名</span>
            <span>用户</span>
            <span>使用次数</span>
          </div>
          <div className={styles.rankList} onScroll={handleActiveScroll}>
            {activeUsers.length === 0 ? (
              <div className={styles.emptyState}>暂无用户数据</div>
            ) : (
              activeUsers.map((item, index) => {
                const rank = index + 1;
                const rankClass =
                  rank === 1
                    ? styles.rankBadgeGold
                    : rank === 2
                    ? styles.rankBadgeSilver
                    : rank === 3
                    ? styles.rankBadgeBronze
                    : styles.rankBadge;

                // 格式化显示：分行名称/用户姓名(用户ID)
                const displayParts: string[] = [];
                if (item.bbkId && getBbkDisplayName(item.bbkId) !== "-") {
                  displayParts.push(getBbkDisplayName(item.bbkId));
                }
                if (item.userName) {
                  displayParts.push(item.userName);
                }
                const displayName = displayParts.length > 0
                  ? `${displayParts.join("/")}(${item.userId})`
                  : item.userId;

                return (
                  <button
                    key={`${item.userId}-${rank}`}
                    type="button"
                    className={styles.rankRow}
                    onClick={() => {
                      setSelectedUserId(item.userId);
                      setSelectedUserName(item.userName);
                      setModalOpen(true);
                    }}
                  >
                    <span className={rankClass}>{rank}</span>
                    <Tooltip title={displayName} placement="top">
                      <span className={styles.rankUser}>
                        {displayName}
                      </span>
                    </Tooltip>
                    <span className={styles.rankCalls}>
                      {formatNumber(item.calls)}
                    </span>
                  </button>
                );
              })
            )}
            {activeLoading && (
              <div className={styles.listFootnote}>加载中...</div>
            )}
          </div>
        </article>

        <article className={styles.panelMedium}>
          <div className={styles.panelHeader}>
            <h3 className={styles.panelTitle}>使用深度</h3>
          </div>
          <div className={styles.depthGrid}>
            {depthCards.map((card) => (
              <div key={card.key} className={styles.depthCard}>
                <div className={styles.depthIconWrap}>
                  {card.key === "avg-rounds" && <MessageCircleMore size={15} />}
                  {card.key === "multi-round" && <Users size={15} />}
                  {card.key === "avg-duration" && <Clock3 size={15} />}
                  {card.key === "avg-sessions" && <ArrowUpRight size={15} />}
                </div>
                <div className={styles.depthValue}>{card.valueText}</div>
                <Tooltip title={card.title} placement="top">
                  <div className={styles.depthTitle}>{card.title}</div>
                </Tooltip>
                <div
                  className={
                    card.changeDirection === "up"
                      ? styles.metricChangeUp
                      : card.changeDirection === "down"
                      ? styles.metricChangeDown
                      : styles.metricChangeFlat
                  }
                >
                  环比
                  {card.changeDirection === "up" && <TrendingUp size={12} />}
                  {card.changeDirection === "down" && (
                    <TrendingDown size={12} />
                  )}
                  {card.changeText}
                </div>
              </div>
            ))}
          </div>
        </article>
      </section>

      <section
        className={styles.summaryGrid}
        data-testid="overview-summary-grid"
      >
        <article className={styles.panelLarge}>
          <div className={styles.panelHeader}>
            <h3 className={styles.panelTitle}>任务执行概览</h3>
            <button
              type="button"
              className={styles.detailLink}
              onClick={() => navigate("/analytics/cron-job-overview")}
            >
              查看详情
              <ChevronRight size={14} />
            </button>
          </div>
          <div className={styles.donutLayout}>
            <div className={styles.donutColumn}>
              <div className={styles.donutWrap}>
                <svg viewBox="0 0 120 120" className={styles.donutSvg}>
                  <circle cx="60" cy="60" r="45" className={styles.donutTrack} />
                  {buildDonutSegments(executionSummary).map((item) => (
                    <circle
                      key={item.key}
                      cx="60"
                      cy="60"
                      r="45"
                      className={styles.donutSegment}
                      style={{
                        stroke: item.color,
                        strokeDasharray: item.dasharray,
                        strokeDashoffset: item.dashoffset,
                      }}
                    />
                  ))}
                </svg>
                <div className={styles.donutCenter}>
                  <strong>
                    {formatNumber(taskStatusSummary?.total_tasks ?? 0)}
                  </strong>
                  <span>总任务数</span>
                </div>
              </div>
              <div className={styles.donutLegend}>
                {executionSummary.map((item) => {
                  const total = Math.max(
                    executionSummary.reduce((sum, row) => sum + row.value, 0),
                    1,
                  );

                  return (
                    <div key={item.key} className={styles.donutLegendItem}>
                      <span className={styles.donutLegendDot} style={{ background: item.color }} />
                      <span>{item.label}</span>
                      <span className={styles.donutLegendValue}>
                        {formatNumber(item.value)}&nbsp;({formatPercent((item.value / total) * 100)})
                      </span>
                    </div>
                  );
                })}
              </div>
            </div>
            <TaskFunnel taskStatusSummary={taskStatusSummary} />
          </div>
        </article>

        <article className={styles.panelMedium}>
          <div className={styles.panelHeader}>
            <h3 className={styles.panelTitle}>技能使用排行榜</h3>
          </div>
          <div className={styles.rankList} onScroll={handleSkillsScroll}>
            {skills.length === 0 ? (
              <div className={styles.emptyState}>暂无技能数据</div>
            ) : (
              skills.map((skill, index) => {
                const rank = index + 1;
                const rankClass =
                  rank === 1
                    ? styles.rankBadgeGold
                    : rank === 2
                    ? styles.rankBadgeSilver
                    : rank === 3
                    ? styles.rankBadgeBronze
                    : styles.rankBadge;
                const descLen = skill.skill_description?.length || 0;
                const tooltipWidth = descLen <= 30 ? 240 : descLen <= 60 ? 320 : descLen <= 100 ? 400 : 520;
                return (
                  <button
                    key={`${skill.skill_name}-${rank}`}
                    type="button"
                    className={styles.rankRow}
                    onClick={() => {
                      setSelectedSkillName(skill.skill_name);
                      setSkillModalOpen(true);
                    }}
                  >
                    <span className={rankClass}>{rank}</span>
                    <Tooltip
                      placement="top"
                      overlayInnerStyle={{ width: tooltipWidth, maxWidth: tooltipWidth }}
                      title={
                        skill.skill_description ? (
                          <div className={styles.skillTooltip}>
                            <div className={styles.skillTooltipName}>
                              {skill.skill_name}
                            </div>
                            <div className={styles.skillTooltipDesc}>
                              {skill.skill_description}
                            </div>
                          </div>
                        ) : (
                          skill.skill_name
                        )
                      }
                    >
                      <span className={styles.rankUser}>
                        {truncateName(skill.skill_name, 20)}
                      </span>
                    </Tooltip>
                    <span className={styles.rankCalls}>
                      {formatNumber(skill.count)}
                    </span>
                  </button>
                );
              })
            )}
            {skillsLoading && (
              <div className={styles.listFootnote}>加载中...</div>
            )}
          </div>
        </article>

        <article className={styles.panelLarge}>
          <div className={styles.panelHeader}>
            <h3 className={styles.panelTitle}>报错分析</h3>
          </div>
          <div className={styles.donutLayoutCompact}>
            <div className={styles.donutCompact}>
              <svg viewBox="0 0 120 120" className={styles.donutCompactSvg}>
                <circle cx="60" cy="60" r="45" className={styles.donutTrack} />
                {buildDonutSegments(errorSummaryItems).map((item) => (
                  <circle
                    key={item.key}
                    cx="60"
                    cy="60"
                    r="45"
                    className={styles.donutSegment}
                    style={{
                      stroke: item.color,
                      strokeDasharray: item.dasharray,
                      strokeDashoffset: item.dashoffset,
                    }}
                  />
                ))}
              </svg>
              <div className={styles.donutCenter}>
                <strong>
                  {formatNumber(safeNumber(errorSummaryData?.total_errors))}
                </strong>
                <span>报错总数</span>
              </div>
            </div>
            <div className={styles.legendHorizontal}>
              <div className={styles.legendGroup}>
                {errorSummaryItems.map((item) => {
                  const total = Math.max(
                    errorSummaryItems.reduce((sum, row) => sum + row.value, 0),
                    1,
                  );

                  return (
                    <div key={item.key} className={styles.legendRow}>
                      <span className={styles.legendLabel}>
                        <i style={{ background: item.color }} />
                        {item.label}
                      </span>
                      <span className={styles.legendValue}>
                        {formatNumber(item.value)} (
                        {formatPercent((item.value / total) * 100)})
                      </span>
                    </div>
                  );
                })}
              </div>
              {errorLoading && (
                <div className={styles.listFootnote}>加载中...</div>
              )}
            </div>
          </div>
        </article>
      </section>

      <section
        className={styles.htmlPreviewSection}
        data-testid="html-preview-click-section"
      >
        <article className={styles.panelLarge}>
          <div className={styles.panelHeader}>
            <h3 className={styles.panelTitle}>客户洞察/电访点击统计</h3>
            <span className={styles.panelMeta}>
              最近点击：{htmlPreviewLatestClick}
            </span>
          </div>
          <div className={styles.htmlPreviewFilters}>
            <Select
              className={styles.htmlPreviewListSelect}
              value={selectedHtmlPreviewListKey}
              onChange={setSelectedHtmlPreviewListKey}
              placeholder="全部名单"
              showSearch
              optionFilterProp="label"
            >
              <Option value="all" label="全部名单">
                全部名单
              </Option>
              {htmlPreviewLists.map((item) => (
                <Option
                  key={item.list_key}
                  value={item.list_key}
                  label={item.list_name}
                >
                  {item.list_name}
                </Option>
              ))}
            </Select>
            <label className={styles.htmlPreviewSwitch}>
              <Switch
                size="small"
                checked={includeUnclickedCustomers}
                onChange={setIncludeUnclickedCustomers}
              />
              <span>显示未点击客户</span>
            </label>
          </div>
          {!hasHtmlPreviewAnalysisData ? (
            <div className={styles.emptyChartState}>
              <Database className={styles.emptyBreakdownIcon} />
              <span>
                {htmlPreviewLoading ? "加载中..." : "暂无客户洞察/电访点击数据"}
              </span>
            </div>
          ) : (
            <div className={styles.htmlPreviewDashboard}>
              <div className={styles.htmlPreviewStats}>
                <div className={styles.htmlPreviewStatCard}>
                  <span>名单数</span>
                  <strong>{formatNumber(htmlPreviewListCount)}</strong>
                </div>
                <div className={styles.htmlPreviewStatCard}>
                  <span>名单总客户数</span>
                  <strong>{formatNumber(htmlPreviewCustomerTotal)}</strong>
                </div>
                <div className={styles.htmlPreviewStatCard}>
                  <span>被点击客户数</span>
                  <strong>{formatNumber(htmlPreviewClickedCustomerCount)}</strong>
                </div>
                <div className={styles.htmlPreviewStatCard}>
                  <span>点击总数</span>
                  <strong>{formatNumber(htmlPreviewTotalClicks)}</strong>
                </div>
              </div>
              <div className={styles.htmlPreviewDistribution}>
                <div className={styles.htmlPreviewTopTitle}>点击分布</div>
                <div className={styles.htmlPreviewDistributionRows}>
                  <div className={styles.htmlPreviewDistributionRow}>
                    <div className={styles.htmlPreviewDistributionMeta}>
                      <span>洞察</span>
                      <strong>
                        {formatNumber(htmlPreviewInsightClicks)}
                        <em>{htmlPreviewInsightPercent}%</em>
                      </strong>
                    </div>
                    <div className={styles.htmlPreviewDistributionTrack}>
                      <i
                        className={styles.htmlPreviewInsightBar}
                        style={{
                          width: `${Math.max(
                            6,
                            (htmlPreviewInsightClicks /
                              htmlPreviewMaxActionClicks) *
                              100,
                          )}%`,
                        }}
                      />
                    </div>
                  </div>
                  <div className={styles.htmlPreviewDistributionRow}>
                    <div className={styles.htmlPreviewDistributionMeta}>
                      <span>电访</span>
                      <strong>
                        {formatNumber(htmlPreviewPhoneClicks)}
                        <em>{htmlPreviewPhonePercent}%</em>
                      </strong>
                    </div>
                    <div className={styles.htmlPreviewDistributionTrack}>
                      <i
                        className={styles.htmlPreviewPhoneBar}
                        style={{
                          width: `${Math.max(
                            6,
                            (htmlPreviewPhoneClicks /
                              htmlPreviewMaxActionClicks) *
                              100,
                          )}%`,
                        }}
                      />
                    </div>
                  </div>
                </div>
              </div>
              <div className={styles.htmlPreviewTopList}>
                <div className={styles.htmlPreviewTopTitle}>名单点击概览</div>
                {displayedHtmlPreviewLists.slice(0, 5).map((item) => (
                  <div
                    key={item.list_key}
                    className={styles.htmlPreviewListRow}
                  >
                    <Tooltip
                      title={item.file_name || item.file_url || item.list_name}
                      placement="top"
                    >
                      <span className={styles.htmlPreviewTopName}>
                        {item.list_name}
                      </span>
                    </Tooltip>
                    <div className={styles.htmlPreviewListMetrics}>
                      <span>
                        <em>名单客户</em>
                        <strong>{formatNumber(item.customer_count)}</strong>
                      </span>
                      <span>
                        <em>点击客户</em>
                        <strong>
                          {formatNumber(item.clicked_customer_count)}
                        </strong>
                      </span>
                      <span>
                        <em>洞察</em>
                        <strong>{formatNumber(item.insight_count)}</strong>
                      </span>
                      <span>
                        <em>电访</em>
                        <strong>{formatNumber(item.phone_count)}</strong>
                      </span>
                    </div>
                  </div>
                ))}
                {displayedHtmlPreviewLists.length === 0 && (
                  <div className={styles.htmlPreviewEmptyLine}>
                    暂无名单点击概览数据
                  </div>
                )}
              </div>
              <div className={styles.htmlPreviewEventList}>
                <div className={styles.htmlPreviewTopTitle}>客户点击明细</div>
                {selectedHtmlPreviewList && (
                  <div className={styles.htmlPreviewSelectedList}>
                    当前名单：{selectedHtmlPreviewList.list_name}
                  </div>
                )}
                {htmlPreviewCustomerClicks.length === 0 ? (
                  <div className={styles.htmlPreviewEmptyLine}>
                    暂无客户点击明细
                  </div>
                ) : (
                  <div className={styles.htmlPreviewCustomerTable}>
                    <div className={styles.htmlPreviewCustomerHeader}>
                      <span>客户</span>
                      <span>洞察</span>
                      <span>电访</span>
                      <span>总点击</span>
                      <span>最近</span>
                    </div>
                    {htmlPreviewCustomerClicks.map((item) => (
                      <div
                        key={`${item.customer_id || item.customer_name}-${
                          item.last_clicked_at || ""
                        }`}
                        className={styles.htmlPreviewCustomerRow}
                      >
                        <div className={styles.htmlPreviewCustomerCell}>
                          <span className={styles.htmlPreviewCustomerName}>
                            {item.customer_name || "未知客户"}
                          </span>
                          {item.customer_id && (
                            <span className={styles.htmlPreviewCustomerId}>
                              {item.customer_id}
                            </span>
                          )}
                        </div>
                        <strong>{formatNumber(item.insight_count)}</strong>
                        <strong>{formatNumber(item.phone_count)}</strong>
                        <strong>{formatNumber(item.total_click_count)}</strong>
                        <span className={styles.htmlPreviewEventTime}>
                          {item.last_clicked_at
                            ? dayjs(item.last_clicked_at).format("MM-DD HH:mm")
                            : "-"}
                        </span>
                      </div>
                    ))}
                  </div>
                )}
              </div>
            </div>
          )}
        </article>
      </section>

      <UserDetailModal
        open={modalOpen}
        userId={selectedUserId}
        userName={selectedUserName}
        startDate={startDateText}
        endDate={endDateText}
        bbkIds={effectiveBbkIds?.join(",")}
        onClose={() => {
          setModalOpen(false);
          setSelectedUserId(null);
        }}
      />

      <SkillDetailModal
        open={skillModalOpen}
        skillName={selectedSkillName}
        startDate={startDateText}
        endDate={endDateText}
        onClose={() => {
          setSkillModalOpen(false);
          setSelectedSkillName("");
        }}
      />
    </div>
  );
}
