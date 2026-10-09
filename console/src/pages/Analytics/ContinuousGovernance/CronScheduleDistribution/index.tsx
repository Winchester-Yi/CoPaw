import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  Button,
  DatePicker,
  Drawer,
  Empty,
  Segmented,
  Spin,
  Table,
  Tag,
} from "antd";
import type { ColumnsType } from "antd/es/table";
import type { Dayjs } from "dayjs";
import dayjs from "dayjs";
import type { EChartsType } from "echarts";
import ReactECharts from "echarts-for-react";
import {
  AlertTriangle,
  Bot,
  CalendarClock,
  Clock3,
  Search,
  Type,
  Workflow,
} from "lucide-react";

import {
  monitorApi,
  type CronScheduleBucketMinutes,
  type CronScheduleDistributionBucket,
  type CronScheduleDistributionDetailsParams,
  type CronScheduleDistributionDetailsResponse,
  type CronScheduleDistributionDiagnostics,
  type CronScheduleDistributionParams,
  type CronScheduleDistributionResponse,
  type CronScheduleOccurrenceItem,
  type CronScheduleTaskType,
} from "../../../../api/modules/monitor";
import styles from "./index.module.less";

type DetailTaskFilter = "all" | CronScheduleTaskType;
type ChartEventParams = {
  componentType?: string;
  dataIndex?: number;
};

interface FilterDraft {
  start: Dayjs | null;
  end: Dayjs | null;
  bucketMinutes: CronScheduleBucketMinutes;
}

const BUCKET_OPTIONS: Array<{
  label: string;
  value: CronScheduleBucketMinutes;
}> = [
  { label: "5 分钟", value: 5 },
  { label: "10 分钟", value: 10 },
  { label: "15 分钟", value: 15 },
  { label: "30 分钟", value: 30 },
  { label: "1 小时", value: 60 },
];
const DETAIL_PAGE_SIZE = 20;
const MAX_RANGE_MS = 7 * 24 * 60 * 60 * 1000;
const RANK_ROW_HEIGHT = 43;
const RANK_VIEWPORT_HEIGHT = 344;
const RANK_OVERSCAN_ROWS = 3;

function buildDefaultDraft(): FilterDraft {
  const start = dayjs().startOf("minute");
  return {
    start,
    end: start.add(24, "hour"),
    bucketMinutes: 15,
  };
}

function toQuery(draft: FilterDraft): CronScheduleDistributionParams | null {
  if (!draft.start || !draft.end) {
    return null;
  }
  return {
    start_time: draft.start.toISOString(),
    end_time: draft.end.toISOString(),
    bucket_minutes: draft.bucketMinutes,
  };
}

function validateDraft(draft: FilterDraft): string | null {
  if (!draft.start || !draft.end) {
    return "请选择完整的开始时间和结束时间";
  }
  const duration = draft.end.valueOf() - draft.start.valueOf();
  if (duration <= 0) {
    return "结束时间必须晚于开始时间";
  }
  if (duration > MAX_RANGE_MS) {
    return "统计时间范围不能超过 7 天";
  }
  return null;
}

function formatNumber(value: number): string {
  return new Intl.NumberFormat("zh-CN").format(value || 0);
}

function formatBucketRange(bucket: CronScheduleDistributionBucket): string {
  const start = dayjs(bucket.start_time);
  const end = dayjs(bucket.end_time);
  return `${start.format("MM-DD HH:mm")} – ${end.format("HH:mm")}`;
}

function formatScheduledAt(value: string): string {
  return dayjs(value).format("YYYY-MM-DD HH:mm:ss");
}

function getErrorMessage(error: unknown, fallback: string): string {
  const httpError = error as {
    message?: string;
    data?: { detail?: { message?: string } };
  };
  return httpError?.data?.detail?.message || httpError?.message || fallback;
}

function isRevisionConflict(error: unknown): boolean {
  const httpError = error as {
    status?: number;
    data?: { detail?: { code?: string } };
  };
  return (
    httpError?.status === 409 ||
    httpError?.data?.detail?.code === "schedule_definition_revision_conflict"
  );
}

function diagnosticText(
  diagnostics?: CronScheduleDistributionDiagnostics,
): string[] {
  if (!diagnostics) return [];
  const items: Array<[number, string]> = [
    [diagnostics.invalid_timezone_jobs, "时区回退 UTC"],
    [diagnostics.unsupported_task_type_jobs, "不支持的任务类型"],
    [diagnostics.invalid_metadata_jobs, "元数据异常"],
    [diagnostics.managed_child_jobs, "批调度托管子任务"],
  ];
  return items
    .filter(([count]) => count > 0)
    .map(([count, label]) => `${label} ${count}`);
}

export default function CronScheduleDistribution() {
  const [draft, setDraft] = useState<FilterDraft>(() => buildDefaultDraft());
  const [appliedQuery, setAppliedQuery] =
    useState<CronScheduleDistributionParams | null>(null);
  const [distribution, setDistribution] =
    useState<CronScheduleDistributionResponse | null>(null);
  const [loading, setLoading] = useState(false);
  const [filterError, setFilterError] = useState<string | null>(null);
  const [aggregateError, setAggregateError] = useState<string | null>(null);
  const [selectedBucket, setSelectedBucket] =
    useState<CronScheduleDistributionBucket | null>(null);
  const [drawerOpen, setDrawerOpen] = useState(false);
  const [detailFilter, setDetailFilter] = useState<DetailTaskFilter>("all");
  const [detail, setDetail] =
    useState<CronScheduleDistributionDetailsResponse | null>(null);
  const [detailLoading, setDetailLoading] = useState(false);
  const [detailError, setDetailError] = useState<string | null>(null);
  const [detailPage, setDetailPage] = useState(1);
  const [detailPageSize, setDetailPageSize] = useState(DETAIL_PAGE_SIZE);
  const [detailRevision, setDetailRevision] = useState<string | null>(null);
  const [rankScrollTop, setRankScrollTop] = useState(0);

  const aggregateRequestId = useRef(0);
  const detailRequestId = useRef(0);
  const lastAttemptRef = useRef<CronScheduleDistributionParams | null>(null);
  const lastTriggerRef = useRef<HTMLElement | null>(null);
  const drawerFocusRef = useRef<HTMLDivElement | null>(null);
  const rankListRef = useRef<HTMLDivElement | null>(null);

  const runAggregate = useCallback(
    async (params: CronScheduleDistributionParams) => {
      const requestId = ++aggregateRequestId.current;
      lastAttemptRef.current = params;
      setLoading(true);
      setAggregateError(null);
      try {
        const response = await monitorApi.getScheduleDistribution(params);
        if (requestId !== aggregateRequestId.current) return;
        setRankScrollTop(0);
        if (rankListRef.current) {
          rankListRef.current.scrollTop = 0;
        }
        setDistribution(response);
        setAppliedQuery(params);
        detailRequestId.current += 1;
        setSelectedBucket(null);
        setDrawerOpen(false);
        setDetail(null);
        setDetailRevision(null);
      } catch (error) {
        if (requestId !== aggregateRequestId.current) return;
        setAggregateError(
          getErrorMessage(error, "计划触发次数加载失败，请稍后重试"),
        );
      } finally {
        if (requestId === aggregateRequestId.current) {
          setLoading(false);
        }
      }
    },
    [],
  );

  useEffect(() => {
    const initialQuery = toQuery(draft);
    if (initialQuery) {
      void runAggregate(initialQuery);
    }
    // The initial snapshot is fixed at mount time; draft edits are explicit.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [runAggregate]);

  const submitQuery = () => {
    const validationError = validateDraft(draft);
    setFilterError(validationError);
    if (validationError) return;
    const params = toQuery(draft);
    if (params) {
      void runAggregate(params);
    }
  };

  const loadDetails = useCallback(
    async (
      bucket: CronScheduleDistributionBucket,
      taskFilter: DetailTaskFilter,
      page: number,
      pageSize: number,
      revision?: string,
    ) => {
      const requestId = ++detailRequestId.current;
      setDetailLoading(true);
      setDetailError(null);
      setDetail(null);
      const params: CronScheduleDistributionDetailsParams = {
        start_time: bucket.start_time,
        end_time: bucket.end_time,
        page,
        page_size: pageSize,
      };
      if (taskFilter !== "all") {
        params.task_type = taskFilter;
      }
      if (page > 1 && revision) {
        params.definition_revision = revision;
      }

      try {
        const response = await monitorApi.getScheduleDistributionDetails(
          params,
        );
        if (requestId !== detailRequestId.current) return;
        setDetail(response);
        setDetailPage(response.page);
        setDetailPageSize(response.page_size);
        setDetailRevision(response.definition_revision);
      } catch (error) {
        if (requestId !== detailRequestId.current) return;
        if (page > 1 && isRevisionConflict(error)) {
          setDetailPage(1);
          setDetailRevision(null);
          void loadDetails(bucket, taskFilter, 1, pageSize);
          return;
        }
        setDetailError(
          getErrorMessage(error, "计划触发明细加载失败，请稍后重试"),
        );
      } finally {
        if (requestId === detailRequestId.current) {
          setDetailLoading(false);
        }
      }
    },
    [],
  );

  const openBucket = useCallback(
    (bucket: CronScheduleDistributionBucket, trigger?: HTMLElement) => {
      lastTriggerRef.current = trigger || null;
      setSelectedBucket(bucket);
      setDrawerOpen(true);
      setDetailFilter("all");
      setDetail(null);
      setDetailPage(1);
      setDetailPageSize(DETAIL_PAGE_SIZE);
      setDetailRevision(null);
      void loadDetails(bucket, "all", 1, DETAIL_PAGE_SIZE);
    },
    [loadDetails],
  );

  const buckets = useMemo(
    () => distribution?.buckets || [],
    [distribution?.buckets],
  );
  const peakBucket = useMemo(
    () =>
      buckets
        .filter((bucket) => bucket.total_count > 0)
        .reduce<CronScheduleDistributionBucket | null>(
          (peak, bucket) =>
            !peak || bucket.total_count > peak.total_count ? bucket : peak,
          null,
        ),
    [buckets],
  );
  const rankedBuckets = useMemo(
    () =>
      [...buckets]
        .filter((bucket) => bucket.total_count > 0)
        .sort(
          (left, right) =>
            right.total_count - left.total_count ||
            left.start_time.localeCompare(right.start_time),
        ),
    [buckets],
  );
  const rankWindow = useMemo(() => {
    const firstVisibleIndex = Math.floor(rankScrollTop / RANK_ROW_HEIGHT);
    const startIndex = Math.max(0, firstVisibleIndex - RANK_OVERSCAN_ROWS);
    const endIndex = Math.min(
      rankedBuckets.length,
      firstVisibleIndex +
        Math.ceil(RANK_VIEWPORT_HEIGHT / RANK_ROW_HEIGHT) +
        RANK_OVERSCAN_ROWS,
    );
    return {
      totalHeight: rankedBuckets.length * RANK_ROW_HEIGHT,
      items: rankedBuckets
        .slice(startIndex, endIndex)
        .map((bucket, offset) => ({
          bucket,
          index: startIndex + offset,
        })),
    };
  }, [rankScrollTop, rankedBuckets]);

  const chartOption = useMemo(
    () => ({
      animation: false,
      color: ["#2563eb", "#16a34a"],
      grid: {
        top: 44,
        right: 20,
        bottom: 56,
        left: 48,
        containLabel: false,
      },
      legend: {
        top: 0,
        right: 0,
        itemWidth: 12,
        itemHeight: 8,
        textStyle: { color: "#64748b", fontSize: 12 },
      },
      tooltip: {
        trigger: "axis",
        axisPointer: {
          type: "none",
        },
        backgroundColor: "rgba(15, 23, 42, 0.94)",
        borderColor: "transparent",
        textStyle: { color: "#f8fafc" },
      },
      xAxis: {
        type: "category",
        data: buckets.map((bucket) =>
          dayjs(bucket.start_time).format("MM-DD HH:mm"),
        ),
        axisTick: { alignWithLabel: true },
        axisLine: { lineStyle: { color: "#cbd5e1" } },
        axisLabel: {
          color: "#64748b",
          hideOverlap: true,
          rotate: buckets.length > 32 ? 45 : 0,
        },
      },
      yAxis: {
        type: "value",
        minInterval: 1,
        axisLabel: { color: "#64748b" },
        splitLine: { lineStyle: { color: "#edf2f7" } },
      },
      series: [
        {
          name: "Text",
          type: "bar",
          stack: "planned-firing",
          barMaxWidth: 28,
          data: buckets.map((bucket) => bucket.text_count),
          emphasis: {
            itemStyle: { borderWidth: 0 },
          },
          select: { disabled: true },
          selectedMode: false,
        },
        {
          name: "Agent",
          type: "bar",
          stack: "planned-firing",
          barMaxWidth: 28,
          data: buckets.map((bucket) => bucket.agent_count),
          emphasis: {
            itemStyle: { borderWidth: 0 },
          },
          select: { disabled: true },
          selectedMode: false,
        },
        {
          name: "技能任务",
          type: "bar",
          stack: "planned-firing",
          barMaxWidth: 28,
          data: buckets.map((bucket) => bucket.workflow_count || 0),
          itemStyle: { borderRadius: [3, 3, 0, 0] },
          emphasis: { itemStyle: { borderWidth: 0 } },
          select: { disabled: true },
          selectedMode: false,
        },
      ],
    }),
    [buckets],
  );
  const chartEvents = useMemo(
    () => ({
      mouseover: (params: ChartEventParams, instance: EChartsType) => {
        if (
          params.componentType === "series" &&
          typeof params.dataIndex === "number"
        ) {
          instance.dispatchAction({ type: "downplay" });
          instance.dispatchAction({
            type: "highlight",
            seriesIndex: [0, 1],
            dataIndex: params.dataIndex,
          });
        }
      },
      mouseout: (_params: ChartEventParams, instance: EChartsType) => {
        instance.dispatchAction({ type: "downplay" });
      },
      click: (params: ChartEventParams) => {
        if (
          params.componentType === "series" &&
          typeof params.dataIndex === "number" &&
          buckets[params.dataIndex]
        ) {
          openBucket(buckets[params.dataIndex]);
        }
      },
    }),
    [buckets, openBucket],
  );

  const detailColumns: ColumnsType<CronScheduleOccurrenceItem> = [
    {
      title: "计划触发时间",
      dataIndex: "scheduled_at",
      width: 170,
      render: (value: string) => formatScheduledAt(value),
    },
    {
      title: "任务名称",
      dataIndex: "job_name",
      ellipsis: true,
      width: 180,
    },
    {
      title: "用户/账号",
      key: "user_identity",
      width: 180,
      render: (_value, record) => (
        <div className={styles.userIdentity}>
          <span className={styles.userName} title={record.user_name}>
            {record.user_name || "-"}
          </span>
          <span className={styles.userId} title={record.user_id}>
            {record.user_id || "-"}
          </span>
        </div>
      ),
    },
    {
      title: "类型",
      dataIndex: "task_type",
      width: 88,
      render: (value: CronScheduleTaskType) => (
        <Tag
          color={
            value === "text" ? "blue" : value === "agent" ? "green" : "cyan"
          }
        >
          {value === "text" ? "Text" : value === "agent" ? "Agent" : "技能任务"}
        </Tag>
      ),
    },
    {
      title: "Cron 表达式",
      dataIndex: "cron_expr",
      width: 150,
    },
    {
      title: "时区",
      dataIndex: "timezone",
      width: 150,
      ellipsis: true,
    },
    {
      title: "任务 ID",
      dataIndex: "job_id",
      width: 180,
      ellipsis: true,
    },
  ];

  const diagnostics = diagnosticText(distribution?.diagnostics);
  const hasResults = Boolean(distribution && distribution.total_count > 0);

  return (
    <section className={styles.root} aria-label="定时任务触发分布">
      <div className={styles.filterPanel}>
        <div className={styles.rangeFields}>
          <label className={styles.field}>
            <span>开始时间</span>
            <DatePicker
              showTime
              allowClear
              value={draft.start}
              status={filterError ? "error" : undefined}
              format="YYYY-MM-DD HH:mm"
              onChange={(value) => {
                setDraft((current) => ({ ...current, start: value }));
                setFilterError(null);
              }}
            />
          </label>
          <span className={styles.rangeSeparator}>至</span>
          <label className={styles.field}>
            <span>结束时间</span>
            <DatePicker
              showTime
              allowClear
              value={draft.end}
              status={filterError ? "error" : undefined}
              format="YYYY-MM-DD HH:mm"
              onChange={(value) => {
                setDraft((current) => ({ ...current, end: value }));
                setFilterError(null);
              }}
            />
          </label>
        </div>

        <label className={styles.intervalField}>
          <span>时间间隔</span>
          <Segmented
            block
            options={BUCKET_OPTIONS}
            value={draft.bucketMinutes}
            onChange={(value) =>
              setDraft((current) => ({
                ...current,
                bucketMinutes: value as CronScheduleBucketMinutes,
              }))
            }
          />
        </label>

        <Button
          type="primary"
          icon={<Search size={15} />}
          onClick={submitQuery}
        >
          查询
        </Button>
        {filterError && (
          <div className={styles.filterError} role="alert">
            {filterError}
          </div>
        )}
      </div>

      {aggregateError && (
        <div className={styles.requestError} role="alert">
          <span>{aggregateError}</span>
          <Button
            type="link"
            size="small"
            onClick={() => {
              if (lastAttemptRef.current) {
                void runAggregate(lastAttemptRef.current);
              }
            }}
          >
            重试
          </Button>
        </div>
      )}

      <Spin spinning={loading}>
        {distribution && (
          <>
            <div className={styles.kpiGrid}>
              <article
                className={styles.kpiCard}
                data-testid="schedule-kpi-total"
              >
                <div className={styles.kpiHeader}>
                  <CalendarClock size={16} />
                  <span>计划触发总次数</span>
                </div>
                <div className={styles.kpiValue}>
                  {formatNumber(distribution?.total_count || 0)}
                </div>
              </article>
              <article
                className={`${styles.kpiCard} ${styles.kpiText}`}
                data-testid="schedule-kpi-text"
              >
                <div className={styles.kpiHeader}>
                  <Type size={16} />
                  <span>Text型任务</span>
                </div>
                <div className={styles.kpiValue}>
                  {formatNumber(distribution?.text_count || 0)}
                </div>
              </article>
              <article
                className={`${styles.kpiCard} ${styles.kpiAgent}`}
                data-testid="schedule-kpi-agent"
              >
                <div className={styles.kpiHeader}>
                  <Bot size={16} />
                  <span>Agent型任务</span>
                </div>
                <div className={styles.kpiValue}>
                  {formatNumber(distribution?.agent_count || 0)}
                </div>
              </article>
              <article
                className={`${styles.kpiCard} ${styles.kpiWorkflow}`}
                data-testid="schedule-kpi-workflow"
              >
                <div className={styles.kpiHeader}>
                  <Workflow size={16} />
                  <span>技能任务</span>
                </div>
                <div className={styles.kpiValue}>
                  {formatNumber(distribution?.workflow_count || 0)}
                </div>
              </article>
              <article
                className={`${styles.kpiCard} ${styles.kpiPeak}`}
                data-testid="schedule-kpi-peak"
              >
                <div className={styles.kpiHeader}>
                  <Clock3 size={16} />
                  <span>峰值时段</span>
                </div>
                <div className={styles.kpiValue}>
                  {formatNumber(peakBucket?.total_count || 0)}
                </div>
                <div className={styles.kpiFoot}>
                  {peakBucket ? formatBucketRange(peakBucket) : "-"}
                </div>
              </article>
            </div>

            {diagnostics.length > 0 && (
              <div className={styles.diagnostics} role="status">
                <AlertTriangle size={14} />
                <span>{diagnostics.join(" · ")}</span>
              </div>
            )}

            {distribution.eligible_job_count === 0 ? (
              <div className={styles.emptyPanel}>
                <Empty
                  image={Empty.PRESENTED_IMAGE_SIMPLE}
                  description="当前没有可统计的定时任务"
                />
              </div>
            ) : distribution.total_count === 0 ? (
              <div className={styles.emptyPanel}>
                <Empty
                  image={Empty.PRESENTED_IMAGE_SIMPLE}
                  description="所选时间段内没有计划触发"
                />
              </div>
            ) : (
              <div className={styles.contentGrid}>
                <section className={styles.panel}>
                  <div className={styles.panelHeader}>
                    <span>计划触发次数分布</span>
                    <span className={styles.panelMeta}>
                      {appliedQuery
                        ? `${appliedQuery.bucket_minutes} 分钟/区段`
                        : ""}
                    </span>
                  </div>
                  {hasResults ? (
                    <>
                      <div
                        className={styles.chart}
                        role="img"
                        aria-label={`计划触发次数分布，共 ${
                          distribution.total_count
                        } 次，Text ${distribution.text_count} 次，Agent ${
                          distribution.agent_count
                        } 次，技能任务 ${distribution.workflow_count || 0} 次`}
                      >
                        <ReactECharts
                          option={chartOption}
                          notMerge
                          style={{ height: 340, width: "100%" }}
                          onEvents={chartEvents}
                        />
                      </div>
                      <p className={styles.srOnly}>
                        点击任一柱状时段可查看该半开区间内的计划触发明细。
                      </p>
                    </>
                  ) : (
                    <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} />
                  )}
                </section>

                <section className={styles.panel}>
                  <div className={styles.panelHeader}>
                    <span>触发高峰区段</span>
                    <span className={styles.panelMeta}>按次数排序</span>
                  </div>
                  <div
                    ref={rankListRef}
                    className={styles.rankList}
                    data-testid="schedule-ranked-buckets"
                    role="region"
                    aria-label="全部非空触发区段排名"
                    tabIndex={0}
                    style={{
                      height: RANK_VIEWPORT_HEIGHT,
                      overflowY: "auto",
                    }}
                    onScroll={(event) =>
                      setRankScrollTop(event.currentTarget.scrollTop)
                    }
                  >
                    <div
                      className={styles.rankListInner}
                      role="list"
                      style={{ height: rankWindow.totalHeight }}
                    >
                      {rankWindow.items.map(({ bucket, index }) => (
                        <div
                          className={styles.rankRow}
                          key={bucket.start_time}
                          role="listitem"
                          aria-posinset={index + 1}
                          aria-setsize={rankedBuckets.length}
                          style={{
                            height: RANK_ROW_HEIGHT,
                            transform: `translateY(${
                              index * RANK_ROW_HEIGHT
                            }px)`,
                          }}
                        >
                          <span className={styles.rankIndex}>{index + 1}</span>
                          <span className={styles.rankTime}>
                            {formatBucketRange(bucket)}
                          </span>
                          <span className={styles.rankTypes}>
                            <i className={styles.textDot} /> {bucket.text_count}
                            <i className={styles.agentDot} />{" "}
                            {bucket.agent_count}
                            <i className={styles.workflowDot} />{" "}
                            {bucket.workflow_count || 0}
                          </span>
                          <span className={styles.rankTotal}>
                            {formatNumber(bucket.total_count)}
                          </span>
                          <Button
                            type="link"
                            size="small"
                            aria-label={`查看 ${formatBucketRange(
                              bucket,
                            )} 计划触发明细`}
                            onClick={(event) =>
                              openBucket(bucket, event.currentTarget)
                            }
                          >
                            详情
                          </Button>
                        </div>
                      ))}
                    </div>
                  </div>
                </section>
              </div>
            )}
          </>
        )}
      </Spin>

      <Drawer
        title={
          selectedBucket
            ? `计划触发明细 · ${formatBucketRange(selectedBucket)}`
            : "计划触发明细"
        }
        width={900}
        open={drawerOpen}
        destroyOnClose
        onClose={() => {
          detailRequestId.current += 1;
          setDrawerOpen(false);
          window.setTimeout(() => lastTriggerRef.current?.focus(), 0);
        }}
        afterOpenChange={(open) => {
          if (open) {
            drawerFocusRef.current?.focus();
          }
        }}
      >
        <div
          className={styles.drawerToolbar}
          ref={drawerFocusRef}
          tabIndex={-1}
        >
          <Segmented
            aria-label="任务类型"
            options={[
              { label: "全部", value: "all" },
              { label: "Text", value: "text" },
              { label: "Agent", value: "agent" },
              { label: "技能任务", value: "workflow" },
            ]}
            value={detailFilter}
            onChange={(value) => {
              const nextFilter = value as DetailTaskFilter;
              setDetailFilter(nextFilter);
              setDetailPage(1);
              setDetailRevision(null);
              if (selectedBucket) {
                void loadDetails(selectedBucket, nextFilter, 1, detailPageSize);
              }
            }}
          />
          {detailError && (
            <div className={styles.detailError} role="alert">
              <span>{detailError}</span>
              <Button
                type="link"
                size="small"
                onClick={() => {
                  if (selectedBucket) {
                    void loadDetails(
                      selectedBucket,
                      detailFilter,
                      detailPage,
                      detailPageSize,
                      detailRevision || undefined,
                    );
                  }
                }}
              >
                重试
              </Button>
            </div>
          )}
        </div>
        <Table
          rowKey={(record) => `${record.scheduled_at}-${record.job_id}`}
          size="small"
          loading={detailLoading}
          columns={detailColumns}
          dataSource={detail?.items || []}
          scroll={{ x: 1100 }}
          pagination={{
            current: detailPage,
            pageSize: detailPageSize,
            total: detail?.total || 0,
            showSizeChanger: true,
            showTotal: (total) => `共 ${total} 条`,
            onChange: (page, pageSize) => {
              setDetailPage(page);
              setDetailPageSize(pageSize);
              if (selectedBucket) {
                void loadDetails(
                  selectedBucket,
                  detailFilter,
                  page,
                  pageSize,
                  detailRevision || undefined,
                );
              }
            },
          }}
        />
      </Drawer>
    </section>
  );
}
