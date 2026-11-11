import { useCallback, useEffect, useRef, useState } from "react";
import { Alert, Descriptions, Space, Spin, Tag, Typography } from "antd";
import { Button } from "@agentscope-ai/design";
import type { ManagedCronContext } from "@/api/types/schedulerCron";
import type {
  SystemCronJob,
  SystemScopeKind,
  SystemSyncSubmission,
  SystemTaskType,
} from "@/api/types/systemCron";
import { systemCronApi } from "@/api/modules/systemCron";
import { RequestIdentity } from "@/api/requestIdentity";
import { failureMessage } from "@/pages/Control/SchedulerCronJobs/helpers";
import { useManagedPanel } from "@/pages/Control/SchedulerCronJobs/useManagedPanel";

const LABELS: Record<SystemTaskType, string> = {
  heartbeat: "心跳",
  dream: "梦境",
  task_session_cleanup: "会话清理",
  archive_maintenance: "归档维护",
};
const SYNC_LABELS = {
  pending: "待同步",
  running: "同步中",
  failed: "平台同步失败",
  synced: "已同步",
};
const pending = (jobs: SystemCronJob[]) =>
  jobs.some(
    (job) => job.sync_status === "pending" || job.sync_status === "running",
  );
const TASK_SCOPES: Record<SystemTaskType, SystemScopeKind> = {
  heartbeat: "agent",
  dream: "agent",
  task_session_cleanup: "source",
  archive_maintenance: "source",
};

interface Props {
  context: ManagedCronContext;
  scope: SystemScopeKind;
  taskTypes: readonly SystemTaskType[];
  submission?: SystemSyncSubmission;
}

function submitted(
  submission: SystemSyncSubmission | undefined,
  type: SystemTaskType,
) {
  return (
    submission?.items?.find((item) => item.task_type === type) ?? submission
  );
}

function matchesJobContext(
  job: SystemCronJob,
  context: ManagedCronContext,
  scope: SystemScopeKind,
) {
  return (
    job.management_owner === "scheduler" &&
    job.scope_kind === scope &&
    TASK_SCOPES[job.task_type] === scope &&
    job.source_id === context.source_id &&
    (scope === "source"
      ? job.tenant_id === "" && job.agent_id === ""
      : job.tenant_id === context.tenant_id &&
        job.agent_id === context.agent_id)
  );
}

function JobFacts({ job }: { job: SystemCronJob }) {
  return (
    <>
      <Space wrap style={{ marginBottom: 8 }}>
        <Tag
          color={
            job.sync_status === "failed"
              ? "error"
              : job.sync_status === "synced"
              ? "success"
              : "processing"
          }
        >
          {SYNC_LABELS[job.sync_status] ?? job.sync_status}
        </Tag>
        <Typography.Text type="secondary">
          {job.has_effective_binding
            ? "已有生效绑定，执行仍受当前配置开关约束"
            : job.binding_known
            ? "已取得平台绑定，尚未确认生效"
            : "尚无生效绑定"}
        </Typography.Text>
      </Space>
      <Descriptions
        size="small"
        column={1}
        items={[
          {
            key: "desired",
            label: "最近读取的期望排程",
            children: job.desired_config?.cron || "待读取或未设置",
          },
          {
            key: "effective",
            label: "已确认生效的排程",
            children: job.effective_config?.cron || "尚无生效配置",
          },
          {
            key: "enabled",
            label: "已确认生效的开关",
            children:
              job.effective_config?.enabled === undefined
                ? "尚未确认"
                : job.effective_config.enabled
                ? "开启"
                : "关闭",
          },
          {
            key: "result",
            label: "最近业务状态",
            children: job.last_status || "尚未执行",
          },
        ]}
      />
      {job.error_code && (
        <Alert
          type="warning"
          showIcon
          message={job.error_code}
          style={{ marginBottom: 8 }}
        />
      )}
      {job.error_code === "system_platform_registration_unknown" && (
        <Alert
          type="warning"
          showIcon
          message="平台注册响应未确认，重试可能产生重复定时器，请先在平台人工核对。"
          style={{ marginBottom: 8 }}
        />
      )}
      {job.needs_verification && (
        <Alert
          type="warning"
          showIcon
          message="执行结果待核实，同身份后续触发将跳过；请管理员通过运维接口核实，不会自动重跑。"
          description={
            <Typography.Text code copyable>
              {job.active_execution_id || "执行标识缺失，请核查存储"}
            </Typography.Text>
          }
          style={{ marginBottom: 8 }}
        />
      )}
    </>
  );
}

function SyncPanel({ context, scope, taskTypes, submission }: Props) {
  const kinds = taskTypes.join("|");
  const load = useCallback(async () => {
    const response = await systemCronApi.jobs(context, scope);
    if (!Array.isArray(response.items)) throw new Error("系统同步状态响应无效");
    for (const job of response.items) {
      if (!matchesJobContext(job, context, scope))
        throw new Error("系统同步状态身份不一致");
    }
    const wanted = new Set(kinds.split("|"));
    return response.items.filter((job) => wanted.has(job.task_type));
  }, [context, scope, kinds]);
  // 复用新协议面板的迟到响应/轮询保护，不复用任何用户任务写请求。
  const panel = useManagedPanel(load, pending);
  const reload = panel.reload;
  const previousSubmission = useRef(submission);
  const latestSubmission = useRef(submission);
  latestSubmission.current = submission;
  const [acknowledged, setAcknowledged] = useState<{
    submission: SystemSyncSubmission | undefined;
    kinds: Set<SystemTaskType>;
  }>({ submission, kinds: new Set() });
  const isUnconfirmed = (kind: SystemTaskType) =>
    submitted(submission, kind)?.submission_status === "failed" &&
    !(acknowledged.submission === submission && acknowledged.kinds.has(kind));
  const identities = useRef({
    submission,
    values: new Map<
      SystemTaskType,
      { identity: RequestIdentity; version?: number }
    >(),
  });
  const posting = useRef(false);
  const [writing, setWriting] = useState<SystemTaskType | null>(null);
  useEffect(() => {
    if (previousSubmission.current === submission) return;
    previousSubmission.current = submission;
    void reload();
  }, [submission, reload]);

  const synchronize = async (kind: SystemTaskType, job?: SystemCronJob) => {
    if (posting.current || !panel.alive.current) return;
    posting.current = true;
    setWriting(kind);
    try {
      if (identities.current.submission !== submission)
        identities.current = { submission, values: new Map() };
      const attempt = identities.current.values.get(kind) ?? {
        identity: new RequestIdentity(),
        version: undefined,
      };
      identities.current.values.set(kind, attempt);
      // 回执未知但后续GET确认更高版本已失败时，旧管理轮次已经结束。
      // 同版本未知仍复用原请求号；不能在在途状态盲目开始另一轮。
      if (
        job?.sync_status === "failed" &&
        (attempt.version === undefined || job.version > attempt.version)
      )
        attempt.identity.reset();
      const identity = attempt.identity;
      const receipt = submitted(submission, kind);
      const refresh = !job || isUnconfirmed(kind);
      const requestId =
        refresh && receipt?.request_id
          ? receipt.request_id
          : identity.for({
              kind,
              job: job?.job_id,
              version: job?.version,
              refresh,
            });
      attempt.version = job?.version;
      const next = refresh
        ? await systemCronApi.refresh(context, kind, requestId)
        : await systemCronApi.retry(context, job, requestId);
      if (!panel.alive.current || latestSubmission.current !== submission)
        return;
      if (
        !matchesJobContext(next, context, scope) ||
        next.task_type !== kind ||
        (!refresh && next.job_id !== job?.job_id)
      )
        throw new Error("系统同步响应身份不一致");
      identity.reset();
      setAcknowledged((value) => ({
        submission,
        kinds: new Set(value.submission === submission ? value.kinds : []).add(
          kind,
        ),
      }));
      panel.replaceValue([
        ...(panel.value ?? []).filter((item) => item.task_type !== kind),
        next,
      ]);
      await panel.reload();
    } catch (error) {
      if (panel.alive.current && latestSubmission.current === submission)
        panel.setError(failureMessage(error));
    } finally {
      posting.current = false;
      if (panel.alive.current) setWriting(null);
    }
  };

  return (
    <div style={{ marginTop: 12 }}>
      <Space wrap style={{ marginBottom: 8 }}>
        <Typography.Text strong>系统调度同步</Typography.Text>
        <Button
          size="small"
          aria-label="刷新同步状态"
          onClick={() => void panel.reload()}
          loading={panel.loading}
          disabled={Boolean(writing)}
        >
          刷新同步状态
        </Button>
      </Space>
      <Typography.Paragraph type="secondary">
        只同步已保存的配置，不包含未保存修改，也不会重新执行业务。
      </Typography.Paragraph>
      {taskTypes.some(isUnconfirmed) && (
        <Alert
          type="warning"
          showIcon
          message="本次保存的同步受理未确认，配置仍已保存；下方为最近同步状态，可按原请求号重新提交。"
          style={{ marginBottom: 8 }}
        />
      )}
      {panel.error && (
        <Alert
          type="error"
          showIcon
          message={`系统同步状态不可用：${panel.error}`}
          style={{ marginBottom: 8 }}
        />
      )}
      {panel.loading && panel.value === null && (
        <Spin aria-label="加载系统同步状态" />
      )}
      {panel.value !== null &&
        taskTypes.map((kind) => {
          const job = panel.value?.find((item) => item.task_type === kind);
          const resubmit = !job || isUnconfirmed(kind);
          const retry = job?.sync_status === "failed";
          return (
            <section key={kind} style={{ marginBottom: 12 }}>
              <Typography.Paragraph strong style={{ marginBottom: 8 }}>
                {LABELS[kind]}
              </Typography.Paragraph>
              {job ? (
                <JobFacts job={job} />
              ) : (
                <Typography.Paragraph type="secondary">
                  尚未登记系统调度
                </Typography.Paragraph>
              )}
              {(resubmit || retry) && (
                <Button
                  size="small"
                  aria-label={
                    resubmit
                      ? `重新提交${LABELS[kind]}同步`
                      : `重试${LABELS[kind]}同步`
                  }
                  loading={writing === kind}
                  disabled={Boolean(writing) || panel.loading}
                  onClick={() => void synchronize(kind, job)}
                >
                  {resubmit
                    ? `重新提交${LABELS[kind]}同步`
                    : `重试${LABELS[kind]}同步`}
                </Button>
              )}
            </section>
          );
        })}
    </div>
  );
}

/** 身份或类型切换整体卸载，旧状态与写请求结果不能覆盖新工作区。 */
export function SystemCronSyncStatus(props: Props) {
  return (
    <SyncPanel
      key={JSON.stringify([props.context, props.scope, props.taskTypes])}
      {...props}
    />
  );
}
