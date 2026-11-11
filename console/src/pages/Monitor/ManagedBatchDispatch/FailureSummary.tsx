import { useCallback, useState } from "react";
import { Button, Popover, Spin, Tag } from "antd";
import { managedBatchBoard, type ManagedFailureFilters } from "@/api/modules/managedBatchBoard";
import { useBoardResource } from "./useBoardResource";
import { timeText } from "./format";
import styles from "./hoverDetails.module.less";

interface Props {
  source: string;
  filters: ManagedFailureFilters;
  refresh: number;
  value?: number;
}

function FailureDetails({ source, filters, value }: Omit<Props, "refresh">) {
  const [retry, setRetry] = useState(0);
  const { start_time, end_time, status, query } = filters;
  const load = useCallback((signal: AbortSignal) => {
    void retry;
    return managedBatchBoard.failureSummary(source, { start_time, end_time, status, query }, signal);
  }, [source, start_time, end_time, status, query, retry]);
  const resource = useBoardResource(load);
  if (resource.loading) return <Spin size="small" aria-label="加载失败明细" />;
  if (resource.error) return <div role="alert">失败明细加载失败 <Button size="small" onClick={() => setRetry(current => current + 1)}>重试查询</Button></div>;
  const data = resource.value;
  if (!data) return null;
  return <section aria-label="失败类型及详情" className={styles.failureDetails}>
    <div className={styles.summaryHeader}>当前失败总数：{data.total}</div>
    {data.total !== value && <p className={styles.hint}>失败数量已变化，明细按本次查询展示；可刷新看板。</p>}
    {data.groups.length === 0 ? <p>当前筛选范围暂无失败任务</p> : data.groups.map(group => <div key={group.failure_type} className={styles.failureGroup}>
      <Tag color="error">{group.label} · {group.count}</Tag>
      {group.error_types?.length ? <ul aria-label="子任务失败类型">{group.error_types.map(item => <li key={item.error_type ?? ""}>
        <span>{item.error_type ?? "未记录类型"}</span><strong>{item.count}</strong>
      </li>)}</ul> : null}
      <ul>{group.details.map(item => <li key={item.message}>
        <span>{item.message}</span><strong>{item.count}</strong>
      </li>)}</ul>
    </div>)}
    <div className={styles.hint}>查询时间：{timeText(data.queried_at)} · 当前筛选范围全部批次</div>
  </section>;
}

function FailureCount(props: Props) {
  const [requested, setRequested] = useState(false);
  const [open, setOpen] = useState(false);
  if (props.value === undefined) return <strong>-</strong>;
  return <Popover
    placement="bottomRight"
    trigger={["hover", "focus", "click"]}
    open={open}
    onOpenChange={next => { setOpen(next); if (next) setRequested(true); }}
    content={requested ? <FailureDetails source={props.source} filters={props.filters} value={props.value} /> : "加载失败明细…"}
  >
    <button type="button" className={styles.failureCount}
      aria-label={`失败任务 ${props.value} 个，查看失败类型及详情`}>
      <strong style={{ color: props.value > 0 ? "#d92d20" : undefined }}>{props.value}</strong>
    </button>
  </Popover>;
}

export default function FailureSummary(props: Props) {
  const { source, filters, refresh } = props;
  // 条件切换卸载旧查询，迟到响应和缓存不能串到下一来源/筛选。
  const key = JSON.stringify([source, filters.start_time, filters.end_time, filters.status, filters.query, refresh]);
  return <FailureCount key={key} {...props} />;
}
