import { useMemo } from "react";
import { Empty } from "antd";
import ReactECharts from "echarts-for-react";
import type { EChartsOption } from "echarts";
import dayjs from "dayjs";
import type { CronDispatchCapacityItem } from "../../../api/modules/monitor";
import { buildWorkerSeries, type WorkerPoint } from "./workerHistory";

export default function WorkerHistoryChart({
  items,
}: {
  items: CronDispatchCapacityItem[];
}) {
  const series = useMemo(() => buildWorkerSeries(items), [items]);
  const option = useMemo<EChartsOption>(
    () => ({
      animation: false,
      aria: {
        enabled: true,
        label: {
          description: `模型 Worker 调整图，共 ${series.length} 个模型、${items.length} 条记录。横轴为调整时间，纵轴为有效 Worker 数量。详细调整原因可在下方记录中查看。`,
        },
      },
      legend: { type: "scroll", top: 0, textStyle: { fontSize: 12 } },
      grid: { left: 48, right: 24, top: 60, bottom: 72, containLabel: true },
      tooltip: {
        trigger: "axis",
        renderMode: "richText",
        confine: true,
        formatter: (params) => {
          const entries = Array.isArray(params) ? params : [params];
          return entries
            .map((entry) => {
              const point = entry.data as WorkerPoint;
              if (!point?.record) return "";
              const r = point.record;
              return `${dayjs(r.created_at).format("MM-DD HH:mm:ss")}\n${
                entry.seriesName
              }\nWorker ${r.previous_workers} → ${r.effective_workers}\n${
                r.decision_reason || "未记录原因"
              }`;
            })
            .join("\n\n");
        },
      },
      xAxis: {
        type: "time",
        axisLabel: { hideOverlap: true },
        name: "调整时间",
        nameLocation: "middle",
        nameGap: 30,
      },
      yAxis: { type: "value", min: 0, minInterval: 1, name: "有效 Worker" },
      dataZoom: [
        { type: "inside", filterMode: "none" },
        { type: "slider", bottom: 8, height: 22, filterMode: "none" },
      ],
      series,
    }),
    [series, items.length],
  );
  return (
    <section aria-label="各模型 Worker 调整折线图">
      <h3>模型 Worker 调整过程</h3>
      <p>
        每条线对应一个
        provider/model；点击图例筛选，拖动下方滑块缩放。仅绘制区间内实际记录。
      </p>
      {series.length ? (
        <ReactECharts
          option={option}
          notMerge
          style={{ height: 320, width: "100%" }}
        />
      ) : (
        <Empty
          image={Empty.PRESENTED_IMAGE_SIMPLE}
          description="当前时间范围内暂无可绘制的调整记录"
        />
      )}
    </section>
  );
}
