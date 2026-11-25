import { useCallback, useMemo } from "react";
import { Alert, Empty, Grid, Modal, Spin } from "antd";
import dayjs from "dayjs";
import ReactECharts from "echarts-for-react";
import type { EChartsOption, LineSeriesOption } from "echarts";
import {
  managedBatchBoard,
  type ManagedWorkerTrendPoint,
} from "@/api/modules/managedBatchBoard";
import type { CronDispatchCapacityItem } from "@/api/modules/monitor";
import { sampledOccupancy } from "../CronBatchDispatch/workerHistory";
import { useBoardResource } from "./useBoardResource";

type Point = [time: number, value: number | null];
type TrendLine = {
  name: string;
  data: Point[];
  color: string;
  step?: boolean;
  dash?: boolean;
};

const AXIS_GROUPS = [
  [0, 1],
  [2, 3],
];
const AXES = AXIS_GROUPS.flat();

function valueAt(data: Point[], time: number) {
  let value: number | null = null;
  for (const [at, current] of data) {
    if (at > time) break;
    value = current;
  }
  return value ?? "—";
}

function workerTrendOption(
  capacity: CronDispatchCapacityItem[],
  points: ManagedWorkerTrendPoint[],
  from: number,
  to: number,
  compact: boolean,
): EChartsOption {
  const tickFormat = to - from > 86_400_000 ? "MM-DD HH:mm" : "HH:mm";
  const samples = capacity
    .map((row) => ({ row, time: Date.parse(row.created_at) }))
    .sort((a, b) => a.time - b.time || a.row.id - b.row.id);
  const sampled = (
    read: (row: CronDispatchCapacityItem) => number | null,
  ): Point[] => samples.map(({ row, time }) => [time, read(row)]);
  const trend = (
    key: Exclude<keyof ManagedWorkerTrendPoint, "at">,
  ): Point[] => {
    if (!points.length) return [];
    const values: Point[] = points.map((point) => [
      Date.parse(point.at),
      point[key],
    ]);
    return [[from, 0], ...values];
  };
  const panels: {
    title: string;
    unit: string;
    lines: TrendLine[];
  }[] = [
    {
      title: "Worker 容量",
      unit: "个",
      lines: [
        {
          name: "有效 Worker",
          data: sampled((row) => row.effective_workers),
          color: "#3769FC",
          step: true,
        },
        {
          name: "占用 Worker",
          data: sampled(sampledOccupancy),
          color: "#8059AE",
          dash: true,
        },
      ],
    },
    {
      title: "待运行任务",
      unit: "条",
      lines: [
        {
          name: "待运行任务（采样）",
          data: sampled((row) =>
            sampledOccupancy(row) === null ? null : row.pending_count,
          ),
          color: "#B55D1E",
        },
      ],
    },
    {
      title: "累计执行结果",
      unit: "条",
      lines: [
        {
          name: "累计执行数",
          data: trend("total"),
          color: "#68758B",
          dash: true,
        },
        { name: "累计成功", data: trend("success"), color: "#238757" },
        { name: "累计失败", data: trend("failed"), color: "#C63C56" },
      ],
    },
    {
      title: "累计派发请求",
      unit: "次",
      lines: [
        { name: "发起请求总数", data: trend("requests"), color: "#3769FC" },
        {
          name: "其中重试",
          data: trend("retries"),
          color: "#8059AE",
          dash: true,
        },
      ],
    },
  ];
  const plotHeight = compact ? 74 : 142;
  const layout = panels.map((_, index) => {
    const secondGroup = index >= 2;
    const rightColumn = !compact && secondGroup;
    const top = compact
      ? 58 + index * 135 + (secondGroup ? 52 : 0)
      : 58 + (index % 2) * 240;
    return {
      top,
      left: compact ? 52 : rightColumn ? "56%" : "6%",
      right: compact ? 18 : rightColumn ? "6%" : "56%",
      labelLeft: rightColumn ? "52%" : 16,
    };
  });
  const series: LineSeriesOption[] = panels.flatMap((panel, index) =>
    panel.lines.map((line) => ({
      type: "line",
      name: line.name,
      xAxisIndex: index,
      yAxisIndex: index,
      data: line.data,
      step: line.step ? "end" : false,
      showSymbol: line.data.length < 2,
      connectNulls: false,
      itemStyle: { color: line.color },
      lineStyle: { color: line.color, type: line.dash ? "dashed" : "solid" },
    })),
  );
  return {
    animation: false,
    aria: { enabled: true },
    title: panels.map((panel, index) => ({
      text: `${panel.title}（${panel.unit}）`,
      left: layout[index].labelLeft,
      top: layout[index].top - 53,
      textStyle: { fontSize: 13, color: "#27344A" },
    })),
    legend: panels.map((panel, index) => ({
      data: panel.lines.map((line) => line.name),
      type: "scroll",
      top: layout[index].top - 32,
      left: layout[index].labelLeft,
      right: layout[index].right,
      itemWidth: 13,
      textStyle: { fontSize: 11 },
    })),
    grid: layout.map(({ top, left, right }) => ({
      top,
      left,
      right,
      height: plotHeight,
    })),
    xAxis: AXES.map((i) => ({
      type: "time",
      gridIndex: i,
      min: from,
      max: to,
      axisLabel: {
        show: AXIS_GROUPS.some(([, bottomAxis]) => i === bottomAxis),
        hideOverlap: true,
        formatter: (value: number) => dayjs(value).format(tickFormat),
      },
    })),
    yAxis: AXES.map((gridIndex) => ({
      type: "value",
      gridIndex,
      min: 0,
      minInterval: 1,
      splitNumber: 3,
      axisLabel: { fontSize: 11 },
    })),
    axisPointer: {
      link: AXIS_GROUPS.map((axes) => ({ xAxisIndex: axes })),
    },
    tooltip: {
      trigger: "axis",
      renderMode: "richText",
      confine: true,
      formatter: (params) => {
        const item = (Array.isArray(params) ? params[0] : params) as
          | { axisIndex?: number; axisValue?: number }
          | undefined;
        const time = Number(item?.axisValue);
        const group = AXIS_GROUPS.find((axes) =>
          axes.includes(item?.axisIndex ?? -1),
        );
        if (!group || !Number.isFinite(time)) return "";
        return [
          dayjs(time).format("MM-DD HH:mm:ss"),
          ...group.flatMap((index) => {
            const { lines, unit } = panels[index];
            return lines.map(
              (line) => `${line.name} ${valueAt(line.data, time)} ${unit}`,
            );
          }),
        ].join("\n");
      },
    },
    dataZoom: AXIS_GROUPS.flatMap((axes) => [
      { type: "inside" as const, xAxisIndex: axes, filterMode: "none" as const },
      {
        type: "slider" as const,
        xAxisIndex: axes,
        filterMode: "none" as const,
        top: layout[axes[1]].top + plotHeight + 32,
        left: layout[axes[0]].left,
        right: layout[axes[0]].right,
        height: 18,
      },
    ]),
    series,
  };
}

export type WorkerTrendScope = {
  source: string;
  start: string;
  end: string;
  refresh: number;
};

export default function WorkerTrendDialog({
  pool,
  history,
  scope,
  onClose,
}: {
  pool: CronDispatchCapacityItem;
  history: CronDispatchCapacityItem[];
  scope: WorkerTrendScope;
  onClose: () => void;
}) {
  const compact = Grid.useBreakpoint().md === false;
  const { source, start, end, refresh } = scope;
  const { provider_id, model_id } = pool;
  const load = useCallback(
    async (signal: AbortSignal) => {
      void refresh;
      const cutoff = Math.min(Date.parse(end), Date.now());
      if (!(Date.parse(start) < cutoff)) return { cutoff, points: [] };
      const { points } = await managedBatchBoard.workerTrends(
        source,
        {
          start_time: start,
          end_time: new Date(cutoff).toISOString(),
          provider_id,
          model_id,
        },
        signal,
      );
      return { cutoff, points };
    },
    [source, start, end, provider_id, model_id, refresh],
  );
  const trends = useBoardResource(load);
  const points = trends.value?.points;
  const cutoff = trends.value?.cutoff;
  const option = useMemo(() => {
    const from = Date.parse(start);
    const to = cutoff ?? Math.min(Date.parse(end), Date.now());
    if (!(from < to)) return null;
    const withinRange = (value: string) => {
      const time = Date.parse(value);
      return time >= from && time <= to;
    };
    const capacity = history.filter(
      (row) =>
        row.provider_id === provider_id &&
        row.model_id === model_id &&
        withinRange(row.created_at),
    );
    const trendPoints = (points ?? []).filter((point) => withinRange(point.at));
    return capacity.length || trendPoints.length
      ? workerTrendOption(capacity, trendPoints, from, to, compact)
      : null;
  }, [history, provider_id, model_id, points, start, end, cutoff, compact]);
  return (
    <Modal
      open
      title={`${provider_id} / ${model_id} · 运行趋势`}
      width={1120}
      footer={null}
      onCancel={onClose}
    >
      <p>容量和积压为共享池采样。累计结果和派发请求按新版批执行统计。</p>
      {trends.error && <Alert type="error" message="新版批执行趋势加载失败" />}
      <Spin spinning={trends.loading}>
        {option ? (
          <ReactECharts
            option={option}
            notMerge
            style={{ height: compact ? 646 : 500, width: "100%" }}
          />
        ) : (
          !trends.loading && <Empty description="当前时间范围内暂无趋势记录" />
        )}
      </Spin>
    </Modal>
  );
}
