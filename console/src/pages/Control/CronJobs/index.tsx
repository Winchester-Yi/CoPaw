import { useState, useEffect, useRef } from "react";
import {
  Button,
  Card,
  Form,
  InputNumber,
  Modal,
  Switch,
  Table,
} from "@agentscope-ai/design";
import { Segmented } from "antd";
import type {
  CronBroadcastTaskResponse,
  CronBroadcastTarget,
  CronBroadcastTenantResult,
  CronJobSpecOutput,
} from "../../../api/types";
import { useTranslation } from "react-i18next";
import api from "../../../api";
import {
  createColumns,
  JobDrawer,
  useCronJobs,
  DEFAULT_FORM_VALUES,
  BroadcastChildrenModal,
  isBroadcastChildJob,
} from "./components";
import { PageHeader } from "@/components/PageHeader";
import { TenantSelector } from "@/components/TenantSelector";
import { useAppMessage } from "../../../hooks/useAppMessage";
import { getUserId } from "../../../utils/identity";
import { getIframeContext } from "../../../stores/iframeStore";
import { DEFAULT_SOURCE_ID } from "../../../constants/identity";
import {
  buildExecutionModelKey,
  useExecutionModelOptions,
} from "@/hooks/useExecutionModelOptions";
import {
  buildCronJobFormValues,
  buildCronJobSubmitPayload,
  buildSkillSelectOptions,
  getBroadcastResultMessage,
  getBroadcastTaskProgressText,
  type CronJobFormValues,
  type SkillSelectOption,
} from "./helpers";
import styles from "./index.module.less";

type CronJob = CronJobSpecOutput;
type BroadcastDispatchMode = "normal" | "batch";
const DEFAULT_BROADCAST_OFFSET_WINDOW_HOURS = 4;
const DEFAULT_TABLE_PAGE_SIZE = 10;
const TABLE_PAGE_SIZE_OPTIONS = ["10", "20", "50", "100"];

function getCurrentSourceId(): string {
  return getIframeContext().source || DEFAULT_SOURCE_ID;
}

function isBatchDispatchEnabled(job: CronJob): boolean {
  return job.meta?.broadcast_dispatch_intents_enabled === true;
}

function getBatchDispatchOffsetWindowHours(job: CronJob): number {
  const parsed = Number(job.meta?.batch_dispatch_offset_window_hours);
  if (!Number.isFinite(parsed)) {
    return DEFAULT_BROADCAST_OFFSET_WINDOW_HOURS;
  }
  return Math.min(24, Math.max(1, Math.round(parsed)));
}

function CronJobsPage() {
  const { t } = useTranslation();
  const { message } = useAppMessage();
  const {
    jobs,
    loading,
    fetchJobs,
    createJob,
    updateJob,
    deleteJob,
    toggleEnabled,
    setBatchDispatch,
    executeNow,
  } = useCronJobs();
  const [drawerOpen, setDrawerOpen] = useState(false);
  const [editingJob, setEditingJob] = useState<CronJob | null>(null);
  const [broadcastingJob, setBroadcastingJob] = useState<CronJob | null>(null);
  const [selectedBroadcastTenantIds, setSelectedBroadcastTenantIds] = useState<
    string[]
  >([]);
  const [selectedBroadcastTargets, setSelectedBroadcastTargets] = useState<
    CronBroadcastTarget[]
  >([]);
  const [broadcastResults, setBroadcastResults] = useState<
    CronBroadcastTenantResult[]
  >([]);
  const [broadcastTask, setBroadcastTask] =
    useState<CronBroadcastTaskResponse | null>(null);
  const [broadcastOffsetEnabled, setBroadcastOffsetEnabled] = useState(true);
  const [broadcastOffsetWindowHours, setBroadcastOffsetWindowHours] = useState(
    DEFAULT_BROADCAST_OFFSET_WINDOW_HOURS,
  );
  const [broadcastDispatchMode, setBroadcastDispatchMode] =
    useState<BroadcastDispatchMode>("normal");
  const [childrenManagementJob, setChildrenManagementJob] =
    useState<CronJob | null>(null);
  const [broadcasting, setBroadcasting] = useState(false);
  const [broadcastRefreshing, setBroadcastRefreshing] = useState(false);
  const [saving, setSaving] = useState(false);
  const [skillOptions, setSkillOptions] = useState<SkillSelectOption[]>([]);
  const [skillOptionsLoading, setSkillOptionsLoading] = useState(false);
  const [tablePage, setTablePage] = useState(1);
  const [tablePageSize, setTablePageSize] = useState(DEFAULT_TABLE_PAGE_SIZE);
  const [form] = Form.useForm<CronJobFormValues>();
  const userTimezoneRef = useRef("UTC");
  const currentTenantId = getUserId();
  const {
    loading: executionModelLoading,
    options: executionModelOptions,
    tenantDefaultLabel,
  } = useExecutionModelOptions(true);
  const hasVisibleBroadcastTask = Boolean(broadcastTask);
  const hasBroadcastDispatchModeChange = broadcastingJob
    ? isBatchDispatchEnabled(broadcastingJob) !==
      (broadcastDispatchMode === "batch")
    : false;

  useEffect(() => {
    api
      .getUserTimezone()
      .then((res) => {
        if (res.timezone) userTimezoneRef.current = res.timezone;
      })
      .catch((err) => console.error("Failed to fetch user timezone:", err));
  }, []);

  useEffect(() => {
    setSkillOptionsLoading(true);
    api
      .listSweSkills(getCurrentSourceId())
      .then((res) => {
        setSkillOptions(buildSkillSelectOptions(res.skills ?? []));
      })
      .catch((err) => {
        console.error("Failed to fetch cron skill options:", err);
        message.error("技能列表加载失败");
      })
      .finally(() => setSkillOptionsLoading(false));
  }, [message]);

  useEffect(() => {
    const maxPage = Math.max(1, Math.ceil(jobs.length / tablePageSize));
    setTablePage((current) => Math.min(current, maxPage));
  }, [jobs.length, tablePageSize]);

  const handleCreate = () => {
    setEditingJob(null);
    form.resetFields();
    const nextValues: Partial<CronJobFormValues> = {
      ...DEFAULT_FORM_VALUES,
      schedule: {
        ...DEFAULT_FORM_VALUES.schedule,
        timezone: userTimezoneRef.current,
      },
      execution_model_key: buildExecutionModelKey(undefined),
    };
    form.setFieldsValue(nextValues);
    setDrawerOpen(true);
  };

  const handleEdit = (job: CronJob) => {
    setEditingJob(job);
    form.resetFields();
    form.setFieldsValue(buildCronJobFormValues(job));
    setDrawerOpen(true);
  };

  const handleDelete = (jobId: string) => {
    Modal.confirm({
      title: t("cronJobs.confirmDelete"),
      content: t("cronJobs.deleteConfirm"),
      okText: t("cronJobs.deleteText"),
      okType: "primary",
      cancelText: t("cronJobs.cancelText"),
      onOk: async () => {
        await deleteJob(jobId);
      },
    });
  };

  const handleToggleEnabled = async (job: CronJob) => {
    await toggleEnabled(job);
  };

  const handleExecuteNow = async (job: CronJob) => {
    Modal.confirm({
      title: t("cronJobs.executeNowTitle"),
      content: t("cronJobs.executeNowContent", { name: job.name }),
      okText: t("cronJobs.executeNowConfirm"),
      okType: "primary",
      cancelText: t("cronJobs.cancelText"),
      onOk: async () => {
        await executeNow(job.id);
      },
    });
  };

  const handleBroadcast = async (job: CronJob) => {
    if (isBroadcastChildJob(job)) {
      message.warning("分发子任务不支持广播到租户");
      return;
    }
    setBroadcastingJob(job);
    setSelectedBroadcastTenantIds([]);
    setSelectedBroadcastTargets([]);
    setBroadcastResults([]);
    setBroadcastTask(null);
    setBroadcastRefreshing(false);
    setBroadcastOffsetEnabled(true);
    setBroadcastOffsetWindowHours(getBatchDispatchOffsetWindowHours(job));
    setBroadcastDispatchMode(isBatchDispatchEnabled(job) ? "batch" : "normal");
    setBroadcasting(true);
    try {
      const currentTask = await api.getCurrentCronBroadcastTask(job.id);
      if (currentTask.task) {
        setBroadcastTask(currentTask.task);
        setBroadcastResults(currentTask.task.results);
      }
    } catch (error) {
      console.error("Failed to fetch current cron broadcast task", error);
      message.error("Broadcast status refresh failed");
    } finally {
      setBroadcasting(false);
    }
  };

  const handleManageChildren = (job: CronJob) => {
    if (isBroadcastChildJob(job)) {
      message.warning("分发子任务不支持查看分发用户");
      return;
    }
    setChildrenManagementJob(job);
  };

  const handleBroadcastCancel = () => {
    setBroadcastingJob(null);
    setSelectedBroadcastTenantIds([]);
    setSelectedBroadcastTargets([]);
    setBroadcastResults([]);
    setBroadcastTask(null);
    setBroadcasting(false);
    setBroadcastRefreshing(false);
    setBroadcastOffsetEnabled(true);
    setBroadcastOffsetWindowHours(DEFAULT_BROADCAST_OFFSET_WINDOW_HOURS);
    setBroadcastDispatchMode("normal");
  };

  const handleBroadcastProgressRefresh = async () => {
    if (
      !broadcastingJob ||
      !broadcastTask ||
      broadcastTask.status !== "running"
    ) {
      return;
    }
    setBroadcastRefreshing(true);
    try {
      const refreshedTask = await api.getCronBroadcastTask(
        broadcastingJob.id,
        broadcastTask.task_id,
      );
      setBroadcastTask(refreshedTask);
      setBroadcastResults(refreshedTask.results);
      if (refreshedTask.status !== "running") {
        await fetchJobs();
        const refreshedJob = await api.getCronJob(broadcastingJob.id);
        setBroadcastingJob(refreshedJob.spec);
        setBroadcastDispatchMode(
          isBatchDispatchEnabled(refreshedJob.spec) ? "batch" : "normal",
        );
      }
    } catch (error) {
      console.error("Failed to refresh cron broadcast task", error);
      message.error("刷新分发进度失败");
    } finally {
      setBroadcastRefreshing(false);
    }
  };

  const handleBroadcastOffsetWindowChange = (value: number | string | null) => {
    const numericValue = Number(value);
    if (!Number.isFinite(numericValue)) {
      setBroadcastOffsetWindowHours(DEFAULT_BROADCAST_OFFSET_WINDOW_HOURS);
      return;
    }
    setBroadcastOffsetWindowHours(
      Math.min(24, Math.max(1, Math.round(numericValue))),
    );
  };

  const handleBroadcastConfirm = async () => {
    if (!broadcastingJob) return;
    if (hasVisibleBroadcastTask) {
      if (broadcastTask?.status === "running") {
        message.info("Broadcast task is already running");
      } else {
        message.info("Close and reopen this dialog to start another broadcast");
      }
      return;
    }
    const targetTenantIds = Array.from(new Set(selectedBroadcastTenantIds));
    const targetByTenantId = new Map(
      selectedBroadcastTargets.map((target) => [target.tenant_id, target]),
    );
    const targets = targetTenantIds.map((tenantId) => {
      const target = targetByTenantId.get(tenantId);
      return {
        tenant_id: tenantId,
        tenant_name: target?.tenant_name ?? null,
        bbk_id: target?.bbk_id ?? null,
      };
    });
    const hasBroadcastTargets = targets.length > 0;
    const shouldUseBatchDispatch = broadcastDispatchMode === "batch";
    const dispatchModeChanged =
      isBatchDispatchEnabled(broadcastingJob) !== shouldUseBatchDispatch;
    if (!hasBroadcastTargets && !dispatchModeChanged) return;

    setBroadcasting(true);
    setBroadcastRefreshing(false);
    setBroadcastTask(null);
    setBroadcastResults([]);
    try {
      if (!hasBroadcastTargets) {
        const syncedJob = await setBatchDispatch(
          broadcastingJob,
          shouldUseBatchDispatch,
          { offset_window_hours: broadcastOffsetWindowHours },
        );
        if (!syncedJob) {
          return;
        }
        setBroadcastingJob(syncedJob);
        handleBroadcastCancel();
        return;
      }
      const res = await api.broadcastCronJob(broadcastingJob.id, targets, {
        enable_offset: broadcastOffsetEnabled,
        ...(dispatchModeChanged
          ? { enable_batch_dispatch: shouldUseBatchDispatch }
          : {}),
        offset_window_hours: broadcastOffsetWindowHours,
      });
      setBroadcastTask(res);
      setBroadcastResults(res.results);
      if (res.status === "running") {
        if (res.reused) {
          message.info("Broadcast task is already running");
        } else {
          message.info(`定时任务分发任务已提交：${res.task_id}`);
        }
        return;
      }
      if (res.status === "failed" && res.failure_summary) {
        message.warning(getBroadcastTaskProgressText(res));
      } else {
        const resultMessage = getBroadcastResultMessage(res.results);
        if (resultMessage.tone === "warning") {
          message.warning(resultMessage.text);
        } else {
          message.success(resultMessage.text);
        }
      }
    } catch (error) {
      console.error("Failed to broadcast cron job", error);
      message.error("Broadcast failed");
    } finally {
      setBroadcasting(false);
    }
  };

  const handleDrawerClose = () => {
    setDrawerOpen(false);
    setEditingJob(null);
  };

  const handleSubmit = async (values: CronJobFormValues) => {
    let processedValues;
    try {
      processedValues = buildCronJobSubmitPayload(values);
    } catch (error) {
      console.error("❌ Failed to normalize cron job payload:", error);
      return;
    }

    let success = false;
    setSaving(true);
    try {
      if (editingJob) {
        success = await updateJob(editingJob.id, processedValues);
      } else {
        success = await createJob(processedValues);
      }
    } finally {
      setSaving(false);
    }
    if (success) {
      setDrawerOpen(false);
    }
  };

  const columns = createColumns({
    onToggleEnabled: handleToggleEnabled,
    onExecuteNow: handleExecuteNow,
    onBroadcast: handleBroadcast,
    onManageChildren: handleManageChildren,
    onEdit: handleEdit,
    onDelete: handleDelete,
    onCopySuccess: () => message.success(t("common.copied")),
    onCopyError: () => message.error(t("common.copyFailed")),
    executionModelOptions,
    tenantDefaultModelLabel: tenantDefaultLabel,
    t,
  });

  return (
    <div className={styles.cronJobsPage}>
      <PageHeader
        items={[{ title: t("nav.runCenter") }, { title: t("cronJobs.title") }]}
        extra={
          <Button type="primary" onClick={handleCreate}>
            + {t("cronJobs.createJob")}
          </Button>
        }
      />

      <Card className={styles.tableCard} bodyStyle={{ padding: 0 }}>
        <Table
          columns={columns}
          dataSource={jobs}
          loading={loading}
          rowKey="id"
          scroll={{ x: 3010 }}
          pagination={{
            current: tablePage,
            pageSize: tablePageSize,
            showSizeChanger: true,
            pageSizeOptions: TABLE_PAGE_SIZE_OPTIONS,
            showTotal: (total) => `共 ${total} 条`,
            onChange: (nextPage, nextPageSize) => {
              setTablePage(nextPage);
              setTablePageSize(nextPageSize || DEFAULT_TABLE_PAGE_SIZE);
            },
          }}
        />
      </Card>

      <JobDrawer
        open={drawerOpen}
        editingJob={editingJob}
        form={form}
        saving={saving}
        executionModelOptions={executionModelOptions}
        executionModelLoading={executionModelLoading}
        tenantDefaultModelLabel={tenantDefaultLabel}
        skillOptions={skillOptions}
        skillOptionsLoading={skillOptionsLoading}
        onClose={handleDrawerClose}
        onSubmit={handleSubmit}
      />

      <BroadcastChildrenModal
        open={Boolean(childrenManagementJob)}
        job={childrenManagementJob}
        onClose={() => setChildrenManagementJob(null)}
      />

      <Modal
        open={Boolean(broadcastingJob)}
        title="广播到租户"
        onCancel={handleBroadcastCancel}
        onOk={handleBroadcastConfirm}
        confirmLoading={broadcasting}
        okButtonProps={{
          disabled:
            (selectedBroadcastTenantIds.length === 0 &&
              !hasBroadcastDispatchModeChange) ||
            broadcasting ||
            hasVisibleBroadcastTask,
        }}
        width={640}
      >
        {broadcastingJob && (
          <div style={{ display: "grid", gap: 12 }}>
            <div>
              任务：{broadcastingJob.name}；时区：
              {broadcastingJob.schedule?.timezone || "UTC"}；
              {broadcastDispatchMode === "batch" ? "批调度" : "正常调度"}；
              {broadcastOffsetEnabled
                ? `散列窗口 ${broadcastOffsetWindowHours} 小时`
                : "不做散列"}
            </div>
            <div className={styles.broadcastOffsetControls}>
              <div className={styles.broadcastDispatchMode}>
                <span>调度方式</span>
                <Segmented
                  value={broadcastDispatchMode}
                  options={[
                    { label: "正常调度", value: "normal" },
                    { label: "批调度", value: "batch" },
                  ]}
                  onChange={(value) =>
                    setBroadcastDispatchMode(value as BroadcastDispatchMode)
                  }
                />
              </div>
              <div className={styles.broadcastOffsetSwitch}>
                <span>启用散列</span>
                <Switch
                  checked={broadcastOffsetEnabled}
                  onChange={(checked) =>
                    setBroadcastOffsetEnabled(Boolean(checked))
                  }
                />
              </div>
              <div className={styles.broadcastOffsetWindow}>
                <span>散列窗口</span>
                <InputNumber
                  min={1}
                  max={24}
                  precision={0}
                  value={broadcastOffsetWindowHours}
                  disabled={!broadcastOffsetEnabled}
                  onChange={handleBroadcastOffsetWindowChange}
                />
                <span>小时</span>
              </div>
            </div>
            <TenantSelector
              selectedTenantIds={selectedBroadcastTenantIds}
              onChange={setSelectedBroadcastTenantIds}
              onSelectionInfoChange={setSelectedBroadcastTargets}
              hint="选择需要接收该定时任务的租户"
              excludeTenantId={currentTenantId}
            />
            {broadcastTask && (
              <div className={styles.broadcastTaskProgress}>
                <span className={styles.broadcastTaskProgressText}>
                  {getBroadcastTaskProgressText(broadcastTask)}
                  {broadcastTask.status !== "running" ? (
                    <span className={styles.broadcastTaskProgressHint}>
                      Close and reopen this dialog to start another broadcast.
                    </span>
                  ) : null}
                </span>
                {broadcastTask.status === "running" ? (
                  <Button
                    size="small"
                    loading={broadcastRefreshing}
                    disabled={broadcastRefreshing}
                    onClick={handleBroadcastProgressRefresh}
                  >
                    刷新进度
                  </Button>
                ) : null}
              </div>
            )}
            {broadcastResults.length > 0 && (
              <div style={{ display: "grid", gap: 6 }}>
                {broadcastResults.map((item) => (
                  <div key={item.tenant_id}>
                    <div>
                      {item.tenant_id}:{" "}
                      {item.success
                        ? `${item.cron} (${item.timezone})`
                        : item.error || "failed"}
                    </div>
                    {item.warning ? (
                      <div style={{ color: "#d46b08", fontSize: 12 }}>
                        {item.warning}
                      </div>
                    ) : null}
                  </div>
                ))}
              </div>
            )}
          </div>
        )}
      </Modal>
    </div>
  );
}

export default CronJobsPage;
