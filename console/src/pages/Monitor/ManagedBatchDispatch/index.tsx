import { useCallback, useMemo, useState } from "react";
import {
  Alert,
  Button,
  DatePicker,
  Dropdown,
  Empty,
  Input,
  Pagination,
  Progress,
  Segmented,
  Select,
  Space,
  Spin,
  Tag,
  message,
} from "antd";
import dayjs, { type Dayjs } from "dayjs";
import { RefreshCw } from "lucide-react";
import {
  managedBatchBoard,
  type ManagedRetryResult,
} from "../../../api/modules/managedBatchBoard";
import { useIframeStore } from "../../../stores/iframeStore";
import { DEFAULT_SOURCE_ID } from "../../../constants/identity";
import { getUserId } from "../../../utils/identity";
import { loadWorkerHistory } from "../CronBatchDispatch/workerHistory";
import styles from "../CronBatchDispatch/index.module.less";
import { followLevelStars, statusTag, timeText } from "./format";
import { followLevelOptions } from "@/constants/batchFollow";
import { useBoardResource } from "./useBoardResource";
import BatchDetail from "./BatchDetail";
import SharedWorkers from "./SharedWorkers";
import WorkerOccupants from "./WorkerOccupants";
import FailureSummary from "./FailureSummary";

const followFilterOptions = [
  { value: "", label: "全部关注等级" },
  { value: "followed", label: "已关注" },
  { value: "unfollowed", label: "未关注" },
  { value: "1", label: "关注 L1" },
  { value: "2", label: "关注 L2" },
  { value: "3", label: "关注 L3" },
  { value: "4", label: "关注 L4" },
  { value: "5", label: "关注 L5" },
];

/** 关注筛选转成看板接口的等级区间；空值表示不筛选。 */
function followRange(value: string) {
  if (value === "followed") return { follow_min: 1 };
  if (value === "unfollowed") return { follow_max: 0 };
  if (value) {
    const level = Number(value);
    return { follow_min: level, follow_max: level };
  }
  return {};
}

function buildRange(key: string): [Dayjs, Dayjs] {
  const now = dayjs();
  if (key === "today") return [now.startOf("day"), now.endOf("day")];
  return [
    key === "last24h"
      ? now.subtract(24, "hour")
      : now.subtract(6, "day").startOf("day"),
    now,
  ];
}
function Board({ source, actor }: { source: string; actor: string }) {
  const [shortcut, setShortcut] = useState("today");
  const [range, setRange] = useState<[Dayjs, Dayjs]>(() => buildRange("today"));
  const [status, setStatus] = useState("");
  const [memberPresence, setMemberPresence] = useState("");
  const [followFilter, setFollowFilter] = useState("");
  const [query, setQuery] = useState("");
  const [page, setPage] = useState(1);
  const [selected, setSelected] = useState("");
  const [refresh, setRefresh] = useState(0);
  const [retryResult, setRetryResult] = useState<ManagedRetryResult | null>(
    null,
  );
  const reload = useCallback(() => setRefresh((value) => value + 1), []);
  // 右键菜单设置关注等级：成功后刷新列表，失败保留原状态。
  const setFollowLevel = useCallback(
    async (batchId: string, level: number) => {
      try {
        await managedBatchBoard.follow(source, batchId, level);
        reload();
      } catch {
        message.error("关注等级更新失败，请重试");
      }
    },
    [source, reload],
  );
  const onQueued = useCallback((result: ManagedRetryResult) => {
    setRetryResult(result);
    setRefresh((value) => value + 1);
  }, []);
  const start = range[0].toISOString();
  const end = range[1].toISOString();
  const batchFilters = useMemo(
    () => ({
      start_time: start,
      end_time: end,
      status,
      has_members: memberPresence ? memberPresence === "true" : undefined,
      query,
      ...followRange(followFilter),
    }),
    [start, end, status, memberPresence, query, followFilter],
  );
  const loadBatches = useCallback(
    (signal: AbortSignal) => {
      void refresh;
      return managedBatchBoard.batches(
        source,
        {
          ...batchFilters,
          page,
          page_size: 4,
        },
        signal,
      );
    },
    [source, batchFilters, page, refresh],
  );
  const batches = useBoardResource(loadBatches);
  const loadWorkers = useCallback(
    (signal: AbortSignal) => {
      void refresh;
      return loadWorkerHistory(
        (cursor) =>
          managedBatchBoard.workers(
            source,
            { start_time: start, end_time: end, capacity_cursor: cursor },
            signal,
          ),
        () => !signal.aborted,
      );
    },
    [source, start, end, refresh],
  );
  const workers = useBoardResource(loadWorkers);
  const items = batches.value?.items || [];
  const batch = items.find((item) => item.batch_id === selected) || items[0];
  const stats = batches.value?.stats;
  const metrics = [
    ["批次总数", stats?.total_batches],
    ["任务总数", stats?.total_members],
    ["运行中批次", stats?.running_batches],
    [
      "任务完成率",
      stats?.total_members
        ? `${((stats.done_members / stats.total_members) * 100).toFixed(1)}%`
        : "-",
    ],
    ["失败任务", stats?.failed_members],
    [
      "有效 Worker",
      workers.value?.current_capacity.reduce(
        (sum, item) => sum + item.effective_workers,
        0,
      ),
    ],
  ];
  return (
    <div className={styles.page}>
      <header className={styles.header}>
        <div>
          <h1>批调度看板（新）</h1>
        </div>
        <Space>
          <Tag color="blue" className={styles.sourceTag}>
            当前渠道 {source}
          </Tag>
          <Button icon={<RefreshCw size={16} />} onClick={reload}>
            刷新
          </Button>
        </Space>
      </header>
      <section className={styles.toolbar}>
        <Segmented
          value={shortcut}
          options={[
            { value: "today", label: "今天" },
            { value: "last24h", label: "近24h" },
            { value: "last7", label: "近7天" },
          ]}
          onChange={(value) => {
            setShortcut(String(value));
            setRange(buildRange(String(value)));
            setPage(1);
          }}
        />
        <DatePicker.RangePicker
          showTime
          allowClear={false}
          value={range}
          className={styles.rangePicker}
          onChange={(value) => {
            if (value?.[0] && value[1]) {
              setRange([value[0], value[1]]);
              setShortcut("custom");
              setPage(1);
            }
          }}
        />
        <Select
          aria-label="新版批次状态"
          value={status}
          className={styles.statusSelect}
          options={[
            { value: "", label: "全部状态" },
            { value: "received", label: "已接收" },
            { value: "pending", label: "等待处理" },
            { value: "running", label: "运行中" },
            { value: "completed", label: "已完成" },
            { value: "failed", label: "失败" },
          ]}
          onChange={(value) => {
            setStatus(value);
            setPage(1);
          }}
        />
        <Select
          aria-label="可执行成员"
          value={memberPresence}
          className={styles.statusSelect}
          options={[
            { value: "", label: "全部成员" },
            { value: "true", label: "有可执行成员" },
            { value: "false", label: "无可执行成员" },
          ]}
          onChange={(value) => {
            setMemberPresence(value);
            setPage(1);
          }}
        />
        <Select
          aria-label="关注等级筛选"
          value={followFilter}
          className={styles.statusSelect}
          options={followFilterOptions}
          onChange={(value) => {
            setFollowFilter(value);
            setPage(1);
          }}
        />
      </section>
      <section className={styles.summaryStrip} aria-label="新版批调度概览">
        {metrics.map(([title, value]) => (
          <div className={styles.summaryMetric} key={title}>
            <span>{title}</span>
            {title === "失败任务" ? (
              <FailureSummary
                source={source}
                filters={batchFilters}
                refresh={refresh}
                value={stats?.failed_members}
              />
            ) : (
              <strong>{value ?? "-"}</strong>
            )}
          </div>
        ))}
      </section>
      {batches.error && <Alert type="error" showIcon message={batches.error} />}
      {retryResult && (
        <Alert
          type="success"
          showIcon
          closable
          onClose={() => setRetryResult(null)}
          message={`人工重试已受理 ${retryResult.queued} 个，跳过 ${
            retryResult.skipped
          } 个${retryResult.reused ? "（复用原请求）" : ""}`}
          description={
            <details>
              <summary>逐项受理结果</summary>
              <ul>
                {retryResult.results.map((item) => (
                  <li key={item.member_id}>
                    {item.member_id}：
                    {item.status === "queued"
                      ? item.transport_only
                        ? "运输重投"
                        : "新增业务尝试"
                      : item.reason}
                    {item.execution_id && (
                      <div style={{ overflowWrap: "anywhere" }}>
                        执行 ID：{item.execution_id}
                      </div>
                    )}
                  </li>
                ))}
              </ul>
            </details>
          }
        />
      )}
      <section className={styles.workspace}>
        <article className={styles.batchPane}>
          <div className={styles.paneHeader}>
            <div>
              <h2>所有批次</h2>
            </div>
            <strong>共 {batches.value?.total ?? "-"} 个全局结果</strong>
          </div>
          <Input
            allowClear
            aria-label="全局筛选新版批次"
            placeholder="全局筛选批次、任务、租户、Agent 或模型"
            value={query}
            onChange={(event) => {
              setQuery(event.target.value);
              setPage(1);
            }}
          />
          <div className={styles.batchListHeader}>
            <span>任务 / 批次</span>
            <span>计划 / 回调</span>
            <span>状态 / 进度</span>
          </div>
          <Spin
            spinning={batches.loading}
            wrapperClassName={styles.batchListSpin}
          >
            <div className={styles.batchList}>
              {items.map((item) => (
                <Dropdown
                  key={item.batch_id}
                  trigger={["contextMenu"]}
                  disabled={typeof item.batch_follow_level !== "number"}
                  menu={{
                    items: followLevelOptions.map((option) => ({
                      key: String(option.value),
                      label: option.label,
                    })),
                    selectable: true,
                    selectedKeys: [String(item.batch_follow_level ?? 0)],
                    onClick: ({ key }: { key: string }) => {
                      void setFollowLevel(item.batch_id, Number(key));
                    },
                  }}
                >
                  <button
                    type="button"
                    onClick={() => setSelected(item.batch_id)}
                    className={`${styles.batchRow} ${
                      batch?.batch_id === item.batch_id
                        ? styles.batchRowSelected
                        : ""
                    }`}
                  >
                    <span className={styles.batchIdentity}>
                      <strong>{item.job_name || "未命名任务"}</strong>
                      {followLevelStars(item.batch_follow_level)}
                      <em title={item.batch_id}>{item.batch_id}</em>
                      <small>{item.job_id}</small>
                    </span>
                    <span className={styles.batchTiming}>
                      <strong>{timeText(item.logical_fire_at)}</strong>
                      <em>回调 {timeText(item.created_at)}</em>
                      {["completed", "failed"].includes(item.status) &&
                        item.finished_at && (
                          <em>完成 {timeText(item.finished_at)}</em>
                        )}
                      <small title="父任务本次执行的冻结模型；各成员模型见成员列表">
                        {item.provider_id || "-"} / {item.model_id || "-"}
                      </small>
                    </span>
                    <span className={styles.batchProgress}>
                      {statusTag(item.status)}
                      {item.batch_paused && (
                        <Tag color="warning">本批已暂停</Tag>
                      )}
                      {item.dispatch_paused &&
                        item.done_count < item.member_count && (
                          <Tag color="warning">批调度入口已暂停</Tag>
                        )}
                      {(item.skipped_count ?? 0) > 0 && (
                        <strong>跳过／取消 {item.skipped_count} 个</strong>
                      )}
                      {item.status === "failed" ? (
                        <strong className={styles.batchOutcome}>
                          <span>成功 {item.success_count ?? "-"} 个</span>
                          <span>失败 {item.failed_count} 个</span>
                        </strong>
                      ) : item.completion_rate === null ? (
                        <small>无可执行成员</small>
                      ) : (
                        <>
                          <strong>
                            {item.done_count}/{item.member_count}
                          </strong>
                          <Progress
                            percent={Math.round(item.completion_rate * 100)}
                            size="small"
                            showInfo={false}
                            status={item.failed_count ? "exception" : "normal"}
                          />
                        </>
                      )}
                    </span>
                  </button>
                </Dropdown>
              ))}
              {!batches.loading && !items.length && (
                <Empty description="当前范围内暂无批次" />
              )}
            </div>
          </Spin>
          <nav className={styles.batchPagination} aria-label="新版批次分页">
            <Pagination
              current={page}
              pageSize={4}
              total={batches.value?.total || 0}
              showSizeChanger={false}
              size="small"
              onChange={setPage}
            />
          </nav>
        </article>
        <article className={styles.detailPane}>
          {batch ? (
            <BatchDetail
              key={batch.batch_id}
              source={source}
              actor={actor}
              batch={batch}
              refresh={refresh}
              onQueued={onQueued}
              onPauseChanged={reload}
            />
          ) : (
            <div className={styles.detailEmpty}>
              <Empty description="请选择一个批次" />
            </div>
          )}
        </article>
      </section>
      <SharedWorkers
        scope={{ source, start, end, refresh }}
        value={workers.value}
        loading={workers.loading}
        error={workers.error}
        occupants={
          <WorkerOccupants
            source={source}
            refresh={refresh}
            pools={[
              ...(workers.value?.policies ?? []),
              ...(workers.value?.current_capacity ?? []),
            ]}
          />
        }
      />
    </div>
  );
}

export default function ManagedBatchDispatchPage() {
  const source = useIframeStore((state) => state.source) || DEFAULT_SOURCE_ID;
  const user = useIframeStore((state) => state.userId);
  const admin = useIframeStore((state) => state.isSuperManager);
  const manager = useIframeStore((state) => state.manager);
  if (!admin && !manager)
    return (
      <div className={styles.page}>
        <Alert
          type="warning"
          showIcon
          message="仅管理员可访问批调度看板（新）"
        />
      </div>
    );
  const actor = user || getUserId();
  return (
    <Board
      key={JSON.stringify([source, actor, admin, manager])}
      source={source}
      actor={actor}
    />
  );
}
