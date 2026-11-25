import { useCallback, useState } from "react";
import { Button, Popover, Spin } from "antd";
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
  const { start_time, end_time, status, has_members, query } = filters;
  const load = useCallback(
    (signal: AbortSignal) => {
      void retry;
      return managedBatchBoard.failureSummary(
        source,
        { start_time, end_time, status, has_members, query },
        signal,
      );
    },
    [
      source,
      start_time,
      end_time,
      status,
      has_members,
      query,
      retry,
    ],
  );
  const resource = useBoardResource(load);
  if (resource.loading) return <Spin size="small" aria-label="加载失败明细" />;
  if (resource.error) return <div role="alert">失败明细加载失败 <Button size="small" onClick={() => setRetry(current => current + 1)}>重试查询</Button></div>;
  const data = resource.value;
  if (!data) return null;
  return (
    <section aria-label="失败类型及详情" className={styles.failureDetails}>
      <header className={styles.summaryHeader}>
        <h3>失败任务明细</h3>
        <span>
          <strong className={data.total > 0 ? styles.failedTotal : undefined}>
            {data.total}
          </strong>{" "}
          个
        </span>
      </header>
      <div className={styles.failureBody}>
        {data.total !== value && (
          <p className={styles.hint}>
            失败数量已变化，明细按本次查询展示。可刷新看板。
          </p>
        )}
        {data.groups.length === 0 && <p>当前筛选范围暂无失败任务</p>}
        {data.groups.map((group) => {
          const types = group.error_types;
          return (
            <section key={group.failure_type} className={styles.failureGroup}>
              <div className={styles.groupHeader}>
                <h4>{group.label}</h4>
                <strong>{group.count}</strong>
              </div>
              {types?.length ? (
                <>
                  <div className={styles.typeHeading}>错误类型</div>
                  <ul className={styles.typeGrid} aria-label="子任务失败类型">
                    {types.map((item) => (
                      <li key={item.error_type ?? ""}>
                        <span>
                          {item.error_type
                            ?.split(",")
                            .map((type) => type.trim())
                            .join(" + ") || "未记录类型"}
                        </span>
                        <strong>{item.count}</strong>
                      </li>
                    ))}
                  </ul>
                </>
              ) : (
                group.details.length > 0 && (
                  <ul className={styles.failureReasons}>
                    {group.details.map((item) => (
                      <li key={item.message}>
                        <span>{item.message}</span>
                        <strong>{item.count}</strong>
                      </li>
                    ))}
                  </ul>
                )
              )}
            </section>
          );
        })}
      </div>
      <footer className={styles.summaryFooter}>
        查询时间：{timeText(data.queried_at)} · 当前筛选范围全部批次
      </footer>
    </section>
  );
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
  const key = JSON.stringify([
    source,
    filters.start_time,
    filters.end_time,
    filters.status,
    filters.has_members,
    filters.query,
    refresh,
  ]);
  return <FailureCount key={key} {...props} />;
}
