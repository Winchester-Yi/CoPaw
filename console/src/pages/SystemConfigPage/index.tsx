import { useEffect, useRef, useState } from "react";
import {
  Alert,
  Button,
  Card,
  InputNumber,
  Modal,
  Result,
  Select,
  Space,
  Spin,
  Switch,
  Tag,
} from "antd";
import { useTranslation } from "react-i18next";
import isEqual from "lodash/isEqual";

import { PageHeader } from "@/components/PageHeader";
import { useAppMessage } from "@/hooks/useAppMessage";
import { sourceSystemConfigApi } from "@/api/modules/sourceSystemConfig";
import type {
  CurrentSourceSystemConfigResponse,
  SourceSystemConfig,
} from "@/api/types/sourceSystemConfig";
import { useIframeStore } from "@/stores/iframeStore";
import { useSourceSystemConfigStore } from "@/stores/sourceSystemConfigStore";
import { DEFAULT_SOURCE_ID } from "@/constants/identity";
import { useCronContext } from "@/hooks/useCronContext";
import { SystemCronSyncStatus } from "@/components/SystemCronSyncStatus";
import type { SystemSyncSubmission } from "@/api/types/systemCron";

import {
  ARCHIVE_MAINTENANCE_RUN_TIME_OPTIONS,
  CURRENT_SOURCE_SYSTEM_CONFIG_SWITCHES,
  CRON_TASK_SESSION_CLEANUP_RUN_TIME_OPTIONS,
  LLM_RATE_LIMITER_NUMBER_FIELDS,
  QUERY_RETRY_NUMBER_FIELDS,
  TOOL_RESULT_COMPACT_NUMBER_FIELDS,
  clearModelCallPolicyConfig,
  enableModelCallPolicyConfig,
  readArchiveMaintenanceConfig,
  readCronNotificationConfig,
  readCronTaskSessionCleanupConfig,
  readCronUnreadAutoPauseConfig,
  readLlmRateLimiterConfigState,
  readQueryRetryConfigState,
  readRegisteredSwitchValue,
  readSystemPromptInjections,
  readToolResultCompactConfig,
  validateSourceSystemConfig,
  writeArchiveMaintenanceValue,
  writeCronNotificationValue,
  writeCronTaskSessionCleanupValue,
  writeCronUnreadAutoPauseValue,
  writeLlmRateLimiterValue,
  writeQueryRetryValue,
  writeRegisteredSwitchValue,
  writeSystemPromptInjections,
  writeToolResultCompactValue,
} from "./registry";
import type {
  LlmRateLimiterConfig,
  ModelCallPolicyConfigKey,
} from "./registry";
import {
  CapabilityGrid,
  ConfigDetailDrawer,
  SystemPromptSegments,
} from "./components";
import { SourceToolLibrary } from "./SourceToolLibrary";
import {
  addPromptSegment,
  buildCapabilitySummaries,
  filterCapabilitySummaries,
  movePromptSegment,
  removePromptSegment,
  type CapabilityFilter,
  type CapabilityId,
} from "./workbench";
import styles from "./index.module.less";

function formatUpdatedAt(value?: string | null): string {
  if (!value) {
    return "未保存";
  }
  return value;
}

export default function SystemConfigPage() {
  const { t } = useTranslation();
  const { message } = useAppMessage();
  const isSuperManager = useIframeStore((state) => state.isSuperManager);
  const manager = useIframeStore((state) => state.manager);
  const activeSourceId =
    useIframeStore((state) => state.source) || DEFAULT_SOURCE_ID;
  const loadEffectiveConfig = useSourceSystemConfigStore(
    (state) => state.loadEffectiveConfig,
  );
  const effectiveSourceConfig = useSourceSystemConfigStore(
    (state) => state.config,
  );
  const effectiveSourceId = useSourceSystemConfigStore(
    (state) => state.sourceId,
  );
  const canManage = isSuperManager || manager;
  const syncContext = useCronContext();
  const [syncReceipt, setSyncReceipt] = useState<{
    source: string;
    value: SystemSyncSubmission;
  } | null>(null);
  const pendingLoad = useRef<{ sourceId: string; requestId: number } | null>(
    null,
  );
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [requestError, setRequestError] = useState<string | null>(null);
  const [validationError, setValidationError] = useState<string | null>(null);
  const [record, setRecord] =
    useState<CurrentSourceSystemConfigResponse | null>(null);
  const [draftConfig, setDraftConfig] = useState<SourceSystemConfig>({});
  const [capabilityFilter, setCapabilityFilter] =
    useState<CapabilityFilter>("all");
  const [selectedCapabilityId, setSelectedCapabilityId] =
    useState<CapabilityId | null>(null);
  const [promptSegments, setPromptSegments] = useState<string[]>([]);
  const [databaseGuardDisablePending, setDatabaseGuardDisablePending] =
    useState(false);
  const [restoreDefaultsPending, setRestoreDefaultsPending] = useState(false);
  const [restoreDefaultsSourceId, setRestoreDefaultsSourceId] = useState<
    string | null
  >(null);
  const [pendingSourceId, setPendingSourceId] = useState<string | null>(null);
  const requestSeqRef = useRef(0);
  const activeSourceRef = useRef(activeSourceId);

  useEffect(() => {
    activeSourceRef.current = activeSourceId;
  }, [activeSourceId]);

  useEffect(() => {
    if (
      restoreDefaultsSourceId !== null &&
      restoreDefaultsSourceId !== activeSourceId
    ) {
      setRestoreDefaultsPending(false);
      setRestoreDefaultsSourceId(null);
    }
  }, [activeSourceId, restoreDefaultsSourceId]);

  const beginRequest = (sourceId: string) => {
    requestSeqRef.current += 1;
    return {
      sourceId,
      requestId: requestSeqRef.current,
    };
  };

  const isCurrentRequest = (request: {
    sourceId: string;
    requestId: number;
  }) => {
    return (
      activeSourceRef.current === request.sourceId &&
      requestSeqRef.current === request.requestId
    );
  };

  const isLoadedSourceCurrent =
    record !== null && record.source_id === activeSourceId;
  const formDisabled = loading || saving || !isLoadedSourceCurrent;
  const isEffectiveConfigCurrent =
    effectiveSourceId === activeSourceId &&
    effectiveSourceConfig?.source_id === activeSourceId;
  const effectiveConfigPayload = isEffectiveConfigCurrent
    ? effectiveSourceConfig.config
    : null;
  const modelCallPolicyDisabled = formDisabled || !isEffectiveConfigCurrent;
  const isDirty = !isEqual(draftConfig, record?.config ?? {});

  useEffect(() => {
    if (!canManage) {
      pendingLoad.current = null;
      requestSeqRef.current += 1;
      setLoading(false);
      setSaving(false);
      setRequestError(null);
      setValidationError(null);
      setRecord(null);
      setDraftConfig({});
      setPromptSegments([]);
      setRestoreDefaultsPending(false);
      setRestoreDefaultsSourceId(null);
      setPendingSourceId(null);
      return;
    }

    if (pendingSourceId) {
      return;
    }

    if (isDirty && record?.source_id && activeSourceId !== record.source_id) {
      setPendingSourceId(activeSourceId);
      useIframeStore.getState().setContext({ source: record.source_id });
      return;
    }

    if (record?.source_id === activeSourceId) {
      return;
    }
    // 清空旧草稿会再次渲染，同一来源的在途读取不能被二次请求作废并吞掉错误。
    if (pendingLoad.current?.sourceId === activeSourceId) return;

    const request = beginRequest(activeSourceId);
    pendingLoad.current = request;
    setLoading(true);
    setSaving(false);
    setRequestError(null);
    setValidationError(null);
    setRecord(null);
    setDraftConfig({});
    setPromptSegments([]);
    void loadEffectiveConfig(activeSourceId);

    sourceSystemConfigApi
      .getCurrent()
      .then((response) => {
        if (
          !isCurrentRequest(request) ||
          response.source_id !== request.sourceId
        ) {
          return;
        }
        setRecord(response);
        setDraftConfig(response.config);
        setPromptSegments(readSystemPromptInjections(response.config));
      })
      .catch((requestError) => {
        if (!isCurrentRequest(request)) {
          return;
        }
        setRequestError(
          requestError instanceof Error
            ? requestError.message
            : String(requestError),
        );
      })
      .finally(() => {
        if (pendingLoad.current?.requestId === request.requestId)
          pendingLoad.current = null;
        if (isCurrentRequest(request)) {
          setLoading(false);
        }
      });
  }, [
    activeSourceId,
    canManage,
    isDirty,
    loadEffectiveConfig,
    pendingSourceId,
    record?.source_id,
  ]);

  useEffect(() => {
    const preventUnload = (event: BeforeUnloadEvent) => {
      if (!isDirty) {
        return;
      }
      event.preventDefault();
      event.returnValue = "";
    };
    window.addEventListener("beforeunload", preventUnload);
    return () => window.removeEventListener("beforeunload", preventUnload);
  }, [isDirty]);

  if (!canManage) {
    return (
      <div className={styles.systemConfigPage}>
        <PageHeader
          parent={t("nav.systemSettings")}
          current={t("nav.currentSourceConfig", {
            defaultValue: "当前系统配置",
          })}
        />
        <div className={styles.centerState}>
          <Result
            status="403"
            title="403"
            subTitle={t("sourceSystemConfigPage.forbidden", {
              defaultValue: "仅管理员可访问当前系统配置页面。",
            })}
          />
        </div>
      </div>
    );
  }

  const handleSwitchChange = (key: string, checked: boolean) => {
    if (formDisabled) {
      return;
    }
    const definition = CURRENT_SOURCE_SYSTEM_CONFIG_SWITCHES.find(
      (item) => item.key === key,
    );
    if (!definition) {
      return;
    }
    if (key === "feature_switches.database_access_guard_enabled" && !checked) {
      setDatabaseGuardDisablePending(true);
      return;
    }
    setValidationError(null);
    setDraftConfig((previous) =>
      writeRegisteredSwitchValue(previous, definition, checked),
    );
  };

  const confirmDatabaseGuardDisable = () => {
    const definition = CURRENT_SOURCE_SYSTEM_CONFIG_SWITCHES.find(
      (item) => item.key === "feature_switches.database_access_guard_enabled",
    );
    if (!definition || formDisabled) {
      return;
    }
    setValidationError(null);
    setDraftConfig((previous) =>
      writeRegisteredSwitchValue(previous, definition, false),
    );
    setDatabaseGuardDisablePending(false);
  };

  const handleToolResultEnabledChange = (checked: boolean) => {
    if (formDisabled) {
      return;
    }
    setValidationError(null);
    setDraftConfig((previous) =>
      writeToolResultCompactValue(previous, "enabled", checked),
    );
  };

  const handleCronUnreadAutoPauseEnabledChange = (checked: boolean) => {
    if (formDisabled) {
      return;
    }
    setValidationError(null);
    setDraftConfig((previous) =>
      writeCronUnreadAutoPauseValue(previous, "enabled", checked),
    );
  };

  const handleCronUnreadAutoPauseThresholdChange = (value: number | null) => {
    if (formDisabled || typeof value !== "number") {
      return;
    }
    setValidationError(null);
    setDraftConfig((previous) =>
      writeCronUnreadAutoPauseValue(previous, "threshold", value),
    );
  };

  const handleCronSkipWeekendZhaohuEnabledChange = (checked: boolean) => {
    if (formDisabled) {
      return;
    }
    setValidationError(null);
    setDraftConfig((previous) =>
      writeCronNotificationValue(
        previous,
        "skip_weekend_zhaohu_enabled",
        checked,
      ),
    );
  };

  const handleCronTaskSessionCleanupEnabledChange = (checked: boolean) => {
    if (formDisabled) {
      return;
    }
    setValidationError(null);
    setDraftConfig((previous) =>
      writeCronTaskSessionCleanupValue(previous, "enabled", checked),
    );
  };

  const handleCronTaskSessionCleanupRetentionChange = (
    value: number | null,
  ) => {
    if (formDisabled || typeof value !== "number") {
      return;
    }
    setValidationError(null);
    setDraftConfig((previous) =>
      writeCronTaskSessionCleanupValue(previous, "retention_days", value),
    );
  };

  const handleCronTaskSessionCleanupRunTimeChange = (value: string) => {
    if (formDisabled) {
      return;
    }
    setValidationError(null);
    setDraftConfig((previous) =>
      writeCronTaskSessionCleanupValue(previous, "run_time", value),
    );
  };

  const handleArchiveMaintenanceEnabledChange = (checked: boolean) => {
    if (formDisabled) {
      return;
    }
    setValidationError(null);
    setDraftConfig((previous) =>
      writeArchiveMaintenanceValue(previous, "enabled", checked),
    );
  };

  const handleArchiveMaintenanceRunTimeChange = (value: string) => {
    if (formDisabled) {
      return;
    }
    setValidationError(null);
    setDraftConfig((previous) =>
      writeArchiveMaintenanceValue(previous, "run_time", value),
    );
  };

  const handleSystemPromptInjectionsChange = (value: string[]) => {
    if (formDisabled) {
      return;
    }
    setValidationError(null);
    setPromptSegments(value);
    setDraftConfig((previous) => writeSystemPromptInjections(previous, value));
  };

  const handleEnableModelCallPolicy = (configKey: ModelCallPolicyConfigKey) => {
    if (modelCallPolicyDisabled) {
      return;
    }
    setValidationError(null);
    setDraftConfig((previous) =>
      enableModelCallPolicyConfig(previous, configKey, effectiveConfigPayload),
    );
  };

  const handleRestoreModelCallPolicyInheritance = (
    configKey: ModelCallPolicyConfigKey,
  ) => {
    if (modelCallPolicyDisabled) {
      return;
    }
    setValidationError(null);
    setDraftConfig((previous) =>
      clearModelCallPolicyConfig(previous, configKey),
    );
  };

  const handleQueryRetryEnabledChange = (checked: boolean) => {
    if (modelCallPolicyDisabled) {
      return;
    }
    setValidationError(null);
    setDraftConfig((previous) =>
      writeQueryRetryValue(previous, "enabled", checked),
    );
  };

  const handleQueryRetryNumberChange = (
    key: (typeof QUERY_RETRY_NUMBER_FIELDS)[number]["key"],
    value: number | null,
  ) => {
    if (modelCallPolicyDisabled || typeof value !== "number") {
      return;
    }
    setValidationError(null);
    setDraftConfig((previous) => writeQueryRetryValue(previous, key, value));
  };

  const handleLlmRateLimiterNumberChange = <
    K extends keyof LlmRateLimiterConfig,
  >(
    key: K,
    value: LlmRateLimiterConfig[K],
  ) => {
    if (modelCallPolicyDisabled) {
      return;
    }
    setValidationError(null);
    setDraftConfig((previous) =>
      writeLlmRateLimiterValue(previous, key, value),
    );
  };

  const handleToolResultNumberChange = (
    key: (typeof TOOL_RESULT_COMPACT_NUMBER_FIELDS)[number]["key"],
    value: number | null,
  ) => {
    if (formDisabled || typeof value !== "number") {
      return;
    }
    setValidationError(null);
    setDraftConfig((previous) =>
      writeToolResultCompactValue(previous, key, value),
    );
  };

  const handleSave = async () => {
    if (formDisabled) {
      return;
    }
    const validationError = validateSourceSystemConfig(
      draftConfig,
      effectiveConfigPayload,
    );
    if (validationError) {
      setValidationError(validationError);
      message.error(validationError);
      return;
    }
    const request = beginRequest(activeSourceId);
    setSaving(true);
    setRequestError(null);
    setValidationError(null);
    try {
      const configToSave = { ...draftConfig };
      delete configToSave.file_read_truncation;
      const nextRecord = await sourceSystemConfigApi.updateCurrent({
        config: configToSave,
      });
      if (
        !isCurrentRequest(request) ||
        nextRecord.source_id !== request.sourceId
      ) {
        return;
      }
      setRecord(nextRecord);
      setDraftConfig(nextRecord.config);
      setPromptSegments(readSystemPromptInjections(nextRecord.config));
      setSyncReceipt({
        source: request.sourceId,
        value: nextRecord.scheduler_sync ?? {
          submission_status: "failed",
          error_code: "system_sync_receipt_unavailable",
        },
      });
      await loadEffectiveConfig(request.sourceId);
      if (!isCurrentRequest(request)) {
        return;
      }
      message.success(
        t("sourceSystemConfigPage.saveSuccess", {
          defaultValue: "当前系统配置已保存",
        }),
      );
      if (pendingSourceId) {
        const sourceId = pendingSourceId;
        setPendingSourceId(null);
        useIframeStore.getState().setContext({ source: sourceId });
      }
    } catch (requestError) {
      const nextError =
        requestError instanceof Error
          ? requestError.message
          : String(requestError);
      if (!isCurrentRequest(request)) {
        return;
      }
      setRequestError(nextError);
      message.error(nextError);
    } finally {
      if (isCurrentRequest(request)) {
        setSaving(false);
      }
    }
  };

  const handleDiscardAndSwitchSource = () => {
    if (!pendingSourceId) {
      return;
    }
    const sourceId = pendingSourceId;
    setPendingSourceId(null);
    setRecord(null);
    setDraftConfig({});
    setPromptSegments([]);
    useIframeStore.getState().setContext({ source: sourceId });
  };

  const cronUnreadAutoPauseConfig = readCronUnreadAutoPauseConfig(draftConfig);
  const cronNotificationConfig = readCronNotificationConfig(draftConfig);
  const cronTaskSessionCleanupConfig =
    readCronTaskSessionCleanupConfig(draftConfig);
  const archiveMaintenanceConfig = readArchiveMaintenanceConfig(draftConfig);
  const systemPromptInjections = promptSegments;
  const queryRetryState = readQueryRetryConfigState(
    draftConfig,
    effectiveConfigPayload,
  );
  const llmRateLimiterState = readLlmRateLimiterConfigState(
    draftConfig,
    effectiveConfigPayload,
  );
  const toolResultCompactConfig = readToolResultCompactConfig(draftConfig);
  const capabilitySummaries = buildCapabilitySummaries({
    savedConfig: record?.config ?? {},
    draftConfig,
    effectiveConfig: effectiveConfigPayload ?? {},
  });
  const visibleCapabilitySummaries = filterCapabilitySummaries(
    capabilitySummaries,
    capabilityFilter,
  );
  const selectedCapability =
    capabilitySummaries.find(
      (summary) => summary.id === selectedCapabilityId,
    ) ?? null;
  const drawerEditor =
    selectedCapabilityId === "conversation" ? (
      <SystemPromptSegments
        disabled={formDisabled}
        prompts={systemPromptInjections}
        onAdd={() =>
          handleSystemPromptInjectionsChange(
            addPromptSegment(systemPromptInjections),
          )
        }
        onChange={(index, value) =>
          handleSystemPromptInjectionsChange(
            systemPromptInjections.map((prompt, promptIndex) =>
              promptIndex === index ? value : prompt,
            ),
          )
        }
        onMove={(index, direction) =>
          handleSystemPromptInjectionsChange(
            movePromptSegment(systemPromptInjections, index, direction),
          )
        }
        onRemove={(index) =>
          handleSystemPromptInjectionsChange(
            removePromptSegment(systemPromptInjections, index),
          )
        }
      />
    ) : selectedCapabilityId === "safety" ? (
      <div className={styles.drawerEditor}>
        <section className={styles.drawerSettingSection}>
          <div className={styles.drawerSettingSectionHeader}>
            <span>访问防护</span>
            <span>1 项设置</span>
          </div>
          {CURRENT_SOURCE_SYSTEM_CONFIG_SWITCHES.filter(
            (definition) =>
              definition.key ===
              "feature_switches.database_access_guard_enabled",
          ).map((definition) => (
            <div key={definition.key} className={styles.switchRow}>
              <div className={styles.switchCopy}>
                <span className={styles.switchTitle}>{definition.title}</span>
                <span className={styles.switchDescription}>
                  {definition.description}
                </span>
              </div>
              <Switch
                checked={readRegisteredSwitchValue(draftConfig, definition)}
                disabled={formDisabled}
                onChange={(checked) =>
                  handleSwitchChange(definition.key, checked)
                }
              />
            </div>
          ))}
        </section>
        <section className={styles.drawerSettingSection}>
          <div className={styles.drawerSettingSectionHeader}>
            <span>审批通知</span>
            <span>1 项设置</span>
          </div>
          {CURRENT_SOURCE_SYSTEM_CONFIG_SWITCHES.filter(
            (definition) =>
              definition.key ===
              "approval_notifications.zhaohu_tool_guard_enabled",
          ).map((definition) => (
            <div key={definition.key} className={styles.switchRow}>
              <div className={styles.switchCopy}>
                <span className={styles.switchTitle}>{definition.title}</span>
                <span className={styles.switchDescription}>
                  {definition.description}
                </span>
              </div>
              <Switch
                checked={readRegisteredSwitchValue(draftConfig, definition)}
                disabled={formDisabled}
                onChange={(checked) =>
                  handleSwitchChange(definition.key, checked)
                }
              />
            </div>
          ))}
        </section>
      </div>
    ) : selectedCapabilityId === "model" ? (
      <div className={styles.drawerEditor}>
        <section className={styles.toolOutputSection}>
          <div className={styles.toolOutputSectionHeader}>
            <span className={styles.switchTitle}>查询重试</span>
            <Tag color={queryRetryState.explicit ? "green" : "blue"}>
              {queryRetryState.explicit
                ? "当前系统显式覆盖"
                : "继承 Agent 配置"}
            </Tag>
          </div>
          {queryRetryState.explicit ? (
            <>
              <div className={styles.switchRow}>
                <span className={styles.switchTitle}>启用查询重试</span>
                <Switch
                  checked={queryRetryState.config.enabled}
                  disabled={modelCallPolicyDisabled}
                  onChange={handleQueryRetryEnabledChange}
                />
              </div>
              <details className={styles.advancedParameters} open>
                <summary>高级参数</summary>
                <div className={styles.numberGrid}>
                  {QUERY_RETRY_NUMBER_FIELDS.map((definition) => (
                    <label key={definition.key} className={styles.numberField}>
                      <span className={styles.numberLabel}>
                        {definition.title}
                      </span>
                      <InputNumber
                        min={definition.min}
                        step={definition.step}
                        value={queryRetryState.config[definition.key]}
                        disabled={
                          modelCallPolicyDisabled ||
                          !queryRetryState.config.enabled
                        }
                        onChange={(value) =>
                          handleQueryRetryNumberChange(definition.key, value)
                        }
                      />
                    </label>
                  ))}
                </div>
              </details>
              <Button
                disabled={modelCallPolicyDisabled}
                onClick={() =>
                  handleRestoreModelCallPolicyInheritance("query_retry")
                }
              >
                恢复继承
              </Button>
            </>
          ) : (
            <Button
              disabled={modelCallPolicyDisabled}
              onClick={() => handleEnableModelCallPolicy("query_retry")}
            >
              启用覆盖
            </Button>
          )}
        </section>
        <section className={styles.toolOutputSection}>
          <div className={styles.toolOutputSectionHeader}>
            <span className={styles.switchTitle}>LLM 并发限流</span>
            <Tag color={llmRateLimiterState.explicit ? "green" : "blue"}>
              {llmRateLimiterState.explicit
                ? "当前系统显式覆盖"
                : "继承 Agent 配置"}
            </Tag>
          </div>
          {llmRateLimiterState.explicit ? (
            <>
              <details className={styles.advancedParameters} open>
                <summary>高级参数</summary>
                <div className={styles.numberGrid}>
                  {LLM_RATE_LIMITER_NUMBER_FIELDS.map((definition) => (
                    <label key={definition.key} className={styles.numberField}>
                      <span className={styles.numberLabel}>
                        {definition.title}
                      </span>
                      <InputNumber
                        min={definition.min}
                        step={definition.step}
                        value={
                          llmRateLimiterState.config[definition.key] ??
                          undefined
                        }
                        disabled={modelCallPolicyDisabled}
                        onChange={(value) => {
                          if (value === null && !definition.nullable) {
                            return;
                          }
                          handleLlmRateLimiterNumberChange(
                            definition.key,
                            (value ??
                              null) as LlmRateLimiterConfig[typeof definition.key],
                          );
                        }}
                      />
                    </label>
                  ))}
                </div>
              </details>
              <Button
                disabled={modelCallPolicyDisabled}
                onClick={() =>
                  handleRestoreModelCallPolicyInheritance("llm_rate_limiter")
                }
              >
                恢复继承
              </Button>
            </>
          ) : (
            <Button
              disabled={modelCallPolicyDisabled}
              onClick={() => handleEnableModelCallPolicy("llm_rate_limiter")}
            >
              启用覆盖
            </Button>
          )}
        </section>
      </div>
    ) : selectedCapabilityId === "cron" ? (
      <div className={styles.drawerEditor}>
        <section className={styles.toolOutputSection}>
          <div className={styles.switchRow}>
            <span className={styles.switchTitle}>定时任务未读自动暂停</span>
            <Switch
              checked={cronUnreadAutoPauseConfig.enabled}
              disabled={formDisabled}
              onChange={handleCronUnreadAutoPauseEnabledChange}
            />
          </div>
          <details className={styles.advancedParameters}>
            <summary>高级参数</summary>
            <label className={styles.numberField}>
              <span className={styles.numberLabel}>未读暂停条数</span>
              <InputNumber
                min={1}
                step={1}
                value={cronUnreadAutoPauseConfig.threshold}
                disabled={formDisabled || !cronUnreadAutoPauseConfig.enabled}
                onChange={handleCronUnreadAutoPauseThresholdChange}
              />
            </label>
          </details>
        </section>
        <section className={styles.toolOutputSection}>
          <div className={styles.switchRow}>
            <span className={styles.switchTitle}>非工作日不发招呼完成通知</span>
            <Switch
              checked={cronNotificationConfig.skip_weekend_zhaohu_enabled}
              disabled={formDisabled}
              onChange={handleCronSkipWeekendZhaohuEnabledChange}
            />
          </div>
        </section>
        <section className={styles.toolOutputSection}>
          <div className={styles.switchRow}>
            <span className={styles.switchTitle}>定时任务会话历史清理</span>
            <Switch
              checked={cronTaskSessionCleanupConfig.enabled}
              disabled={formDisabled}
              onChange={handleCronTaskSessionCleanupEnabledChange}
            />
          </div>
          <details className={styles.advancedParameters}>
            <summary>高级参数</summary>
            <div className={styles.numberGrid}>
              <label className={styles.numberField}>
                <span className={styles.numberLabel}>历史保留天数</span>
                <InputNumber
                  min={1}
                  step={1}
                  value={cronTaskSessionCleanupConfig.retention_days}
                  disabled={
                    formDisabled || !cronTaskSessionCleanupConfig.enabled
                  }
                  onChange={handleCronTaskSessionCleanupRetentionChange}
                />
              </label>
              <label className={styles.numberField}>
                <span className={styles.numberLabel}>每日运行时间</span>
                <Select
                  value={cronTaskSessionCleanupConfig.run_time}
                  disabled={
                    formDisabled || !cronTaskSessionCleanupConfig.enabled
                  }
                  options={CRON_TASK_SESSION_CLEANUP_RUN_TIME_OPTIONS.map(
                    (runTime) => ({ label: runTime, value: runTime }),
                  )}
                  onChange={handleCronTaskSessionCleanupRunTimeChange}
                />
              </label>
            </div>
          </details>
        </section>
        <section className={styles.toolOutputSection}>
          <div className={styles.switchRow}>
            <span className={styles.switchTitle}>文件归档维护</span>
            <Switch
              checked={archiveMaintenanceConfig.enabled}
              disabled={formDisabled}
              onChange={handleArchiveMaintenanceEnabledChange}
            />
          </div>
          <details className={styles.advancedParameters}>
            <summary>高级参数</summary>
            <label className={styles.numberField}>
              <span className={styles.numberLabel}>归档维护每日运行时间</span>
              <Select
                value={archiveMaintenanceConfig.run_time}
                disabled={formDisabled || !archiveMaintenanceConfig.enabled}
                options={ARCHIVE_MAINTENANCE_RUN_TIME_OPTIONS.map(
                  (runTime) => ({
                    label: runTime,
                    value: runTime,
                  }),
                )}
                onChange={handleArchiveMaintenanceRunTimeChange}
              />
            </label>
          </details>
        </section>
      </div>
    ) : selectedCapabilityId === "output" ? (
      <div className={styles.drawerEditor}>
        <section className={styles.toolOutputSection}>
          <div className={styles.switchRow}>
            <span className={styles.switchTitle}>启用工具结果压缩</span>
            <Switch
              checked={toolResultCompactConfig.enabled}
              disabled={formDisabled}
              onChange={handleToolResultEnabledChange}
            />
          </div>
          <details className={styles.advancedParameters}>
            <summary>高级参数</summary>
            <div className={styles.numberGrid}>
              {TOOL_RESULT_COMPACT_NUMBER_FIELDS.map((definition) => (
                <label key={definition.key} className={styles.numberField}>
                  <span className={styles.numberLabel}>{definition.title}</span>
                  <InputNumber
                    min={definition.min}
                    max={definition.max}
                    step={definition.step}
                    value={toolResultCompactConfig[definition.key]}
                    disabled={formDisabled}
                    onChange={(value) =>
                      handleToolResultNumberChange(definition.key, value)
                    }
                  />
                </label>
              ))}
            </div>
          </details>
        </section>
      </div>
    ) : null;
  const handleDelete = async () => {
    const currentSourceId =
      useIframeStore.getState().source || DEFAULT_SOURCE_ID;
    if (
      formDisabled ||
      restoreDefaultsSourceId === null ||
      restoreDefaultsSourceId !== activeSourceId ||
      restoreDefaultsSourceId !== currentSourceId
    ) {
      setRestoreDefaultsPending(false);
      setRestoreDefaultsSourceId(null);
      return;
    }
    setRestoreDefaultsPending(false);
    const request = beginRequest(restoreDefaultsSourceId);
    setRestoreDefaultsSourceId(null);
    setSaving(true);
    setRequestError(null);
    setValidationError(null);
    try {
      const deleted = await sourceSystemConfigApi.deleteCurrent();
      if (!isCurrentRequest(request)) {
        return;
      }
      setSyncReceipt({
        source: request.sourceId,
        value: deleted?.scheduler_sync ?? {
          submission_status: "failed",
          error_code: "system_sync_receipt_unavailable",
        },
      });
      const nextRecord = await sourceSystemConfigApi.getCurrent();
      if (
        !isCurrentRequest(request) ||
        nextRecord.source_id !== request.sourceId
      ) {
        return;
      }
      setRecord(nextRecord);
      setDraftConfig(nextRecord.config);
      setPromptSegments(readSystemPromptInjections(nextRecord.config));
      await loadEffectiveConfig(request.sourceId);
      if (!isCurrentRequest(request)) {
        return;
      }
      message.success(
        t("sourceSystemConfigPage.deleteSuccess", {
          defaultValue: "当前系统配置已恢复默认态",
        }),
      );
    } catch (requestError) {
      const nextError =
        requestError instanceof Error
          ? requestError.message
          : String(requestError);
      if (!isCurrentRequest(request)) {
        return;
      }
      setRequestError(nextError);
      message.error(nextError);
    } finally {
      if (isCurrentRequest(request)) {
        setSaving(false);
      }
    }
  };

  return (
    <div className={styles.systemConfigPage}>
      <PageHeader
        parent={t("nav.systemSettings")}
        current={t("nav.currentSourceConfig", {
          defaultValue: "系统特性配置",
        })}
        subRow={
          <Space size={8}>
            <Tag color="blue">{activeSourceId}</Tag>
            {record ? (
              <Tag color={record.is_default ? "default" : "gold"}>
                {record.is_default
                  ? t("sourceSystemConfigPage.defaultState", {
                      defaultValue: "继承默认值",
                    })
                  : t("sourceSystemConfigPage.overrideState", {
                      defaultValue: "存在显式覆盖",
                    })}
              </Tag>
            ) : null}
          </Space>
        }
      />
      <div className={styles.pageBody}>
        {requestError ? (
          <Alert
            type="error"
            showIcon
            message={t("sourceSystemConfigPage.requestFailed", {
              defaultValue: "当前系统配置请求失败",
            })}
            description={requestError}
          />
        ) : null}

        {validationError ? (
          <Alert
            type="error"
            showIcon
            message={t("sourceSystemConfigPage.validationFailed", {
              defaultValue: "当前系统配置校验失败",
            })}
            description={validationError}
          />
        ) : null}

        {loading ? (
          <div className={styles.centerState}>
            <Spin size="large" />
          </div>
        ) : (
          <>
            <Card className={styles.metaCard}>
              <div className={styles.metaGrid}>
                <div>
                  <span className={styles.metaLabel}>
                    {t("sourceSystemConfigPage.sourceLabel", {
                      defaultValue: "当前系统",
                    })}
                  </span>
                  <span className={styles.metaValue}>{activeSourceId}</span>
                </div>
                <div>
                  <span className={styles.metaLabel}>
                    {t("sourceSystemConfigPage.versionLabel", {
                      defaultValue: "原始配置版本",
                    })}
                  </span>
                  <span className={styles.metaValue}>
                    {record?.version ?? 0}
                  </span>
                </div>
                <div>
                  <span className={styles.metaLabel}>
                    {t("sourceSystemConfigPage.updatedByLabel", {
                      defaultValue: "最近修改人",
                    })}
                  </span>
                  <span className={styles.metaValue}>
                    {record?.updated_by || "未保存"}
                  </span>
                </div>
                <div>
                  <span className={styles.metaLabel}>
                    {t("sourceSystemConfigPage.updatedAtLabel", {
                      defaultValue: "最近修改时间",
                    })}
                  </span>
                  <span className={styles.metaValue}>
                    {formatUpdatedAt(record?.updated_at)}
                  </span>
                </div>
              </div>
            </Card>
            <SourceToolLibrary sourceId={activeSourceId} />

            <CapabilityGrid
              summaries={visibleCapabilitySummaries}
              filter={capabilityFilter}
              onFilterChange={setCapabilityFilter}
              onSelect={setSelectedCapabilityId}
            />

            <ConfigDetailDrawer
              capability={selectedCapability}
              hasUnsavedChanges={isDirty}
              open={selectedCapabilityId !== null}
              onClose={() => setSelectedCapabilityId(null)}
            >
              <div className={styles.drawerSummary}>
                <span>{selectedCapability?.summary}</span>
                <span>所有修改仍会通过页面底部统一保存。</span>
              </div>
              {drawerEditor}
            </ConfigDetailDrawer>

            <Card
              id="system-config-switches"
              className={styles.switchCard}
              title={t("sourceSystemConfigPage.switchesTitle", {
                defaultValue: "受控功能开关",
              })}
            >
              <div className={styles.switchList}>
                {CURRENT_SOURCE_SYSTEM_CONFIG_SWITCHES.map((definition) => (
                  <div key={definition.key} className={styles.switchRow}>
                    <div className={styles.switchCopy}>
                      <span className={styles.switchTitle}>
                        {definition.title}
                      </span>
                      <span className={styles.switchDescription}>
                        {definition.description}
                      </span>
                    </div>
                    <Switch
                      checked={readRegisteredSwitchValue(
                        draftConfig,
                        definition,
                      )}
                      disabled={formDisabled}
                      onChange={(checked) =>
                        handleSwitchChange(definition.key, checked)
                      }
                    />
                  </div>
                ))}
              </div>
            </Card>

            <Card
              id="system-config-prompts"
              className={styles.switchCard}
              title={t("sourceSystemConfigPage.systemPromptInjectionsTitle", {
                defaultValue: "系统提示词注入",
              })}
            >
              <div className={styles.toolResultIntro}>
                {t("sourceSystemConfigPage.systemPromptInjectionsIntro", {
                  defaultValue:
                    "配置当前系统运行时固定追加到对话的系统提示词。多段提示词使用空行分隔，保存时会自动去除空段和重复段。",
                })}
              </div>
              <SystemPromptSegments
                disabled={formDisabled}
                prompts={systemPromptInjections}
                onAdd={() =>
                  handleSystemPromptInjectionsChange(
                    addPromptSegment(systemPromptInjections),
                  )
                }
                onChange={(index, value) =>
                  handleSystemPromptInjectionsChange(
                    systemPromptInjections.map((prompt, promptIndex) =>
                      promptIndex === index ? value : prompt,
                    ),
                  )
                }
                onMove={(index, direction) =>
                  handleSystemPromptInjectionsChange(
                    movePromptSegment(systemPromptInjections, index, direction),
                  )
                }
                onRemove={(index) =>
                  handleSystemPromptInjectionsChange(
                    removePromptSegment(systemPromptInjections, index),
                  )
                }
              />
            </Card>

            <Card
              id="system-config-model"
              className={styles.switchCard}
              title={t("sourceSystemConfigPage.modelCallPolicyTitle", {
                defaultValue: "模型调用策略",
              })}
            >
              <div className={styles.toolResultIntro}>
                {t("sourceSystemConfigPage.modelCallPolicyIntro", {
                  defaultValue:
                    "当前系统下 Query 重试与 LLM 并发限流的显式覆盖。未启用覆盖时继承 Agent 运行配置。",
                })}
              </div>

              <section className={styles.toolOutputSection}>
                <div className={styles.toolOutputSectionHeader}>
                  <div className={styles.switchCopy}>
                    <span className={styles.switchTitle}>
                      {t("sourceSystemConfigPage.queryRetryTitle", {
                        defaultValue: "查询重试",
                      })}
                    </span>
                    <span className={styles.switchDescription}>
                      {t("sourceSystemConfigPage.queryRetryDescription", {
                        defaultValue:
                          "控制当前系统请求遇到瞬时错误时是否按退避策略重试整轮 Query。",
                      })}
                    </span>
                  </div>
                  <Tag color={queryRetryState.explicit ? "green" : "blue"}>
                    {queryRetryState.explicit
                      ? t("sourceSystemConfigPage.explicitOverrideState", {
                          defaultValue: "当前系统显式覆盖",
                        })
                      : t("sourceSystemConfigPage.inheritedAgentState", {
                          defaultValue: "继承 Agent 运行配置",
                        })}
                  </Tag>
                </div>
                {queryRetryState.explicit ? (
                  <>
                    <div className={styles.switchRow}>
                      <div className={styles.switchCopy}>
                        <span className={styles.switchTitle}>
                          {t("sourceSystemConfigPage.queryRetryEnabled", {
                            defaultValue: "启用查询重试",
                          })}
                        </span>
                        <span className={styles.switchDescription}>
                          {t(
                            "sourceSystemConfigPage.queryRetryEnabledDescription",
                            {
                              defaultValue:
                                "关闭后当前系统的 Query 失败不会由 Runner 自动重试。",
                            },
                          )}
                        </span>
                      </div>
                      <Switch
                        checked={queryRetryState.config.enabled}
                        disabled={modelCallPolicyDisabled}
                        onChange={handleQueryRetryEnabledChange}
                      />
                    </div>
                    <div className={styles.numberGrid}>
                      {QUERY_RETRY_NUMBER_FIELDS.map((definition) => (
                        <label
                          key={definition.key}
                          className={styles.numberField}
                        >
                          <span className={styles.numberLabel}>
                            {definition.title}
                          </span>
                          <InputNumber
                            min={definition.min}
                            step={definition.step}
                            value={queryRetryState.config[definition.key]}
                            disabled={
                              modelCallPolicyDisabled ||
                              !queryRetryState.config.enabled
                            }
                            onChange={(value) =>
                              handleQueryRetryNumberChange(
                                definition.key,
                                value,
                              )
                            }
                          />
                        </label>
                      ))}
                    </div>
                    <Button
                      disabled={modelCallPolicyDisabled}
                      onClick={() =>
                        handleRestoreModelCallPolicyInheritance("query_retry")
                      }
                    >
                      {t("sourceSystemConfigPage.restoreInheritance", {
                        defaultValue: "恢复继承",
                      })}
                    </Button>
                  </>
                ) : (
                  <Button
                    disabled={modelCallPolicyDisabled}
                    onClick={() => handleEnableModelCallPolicy("query_retry")}
                  >
                    {t("sourceSystemConfigPage.enableOverride", {
                      defaultValue: "启用覆盖",
                    })}
                  </Button>
                )}
              </section>

              <section className={styles.toolOutputSection}>
                <div className={styles.toolOutputSectionHeader}>
                  <div className={styles.switchCopy}>
                    <span className={styles.switchTitle}>
                      {t("sourceSystemConfigPage.llmRateLimiterTitle", {
                        defaultValue: "LLM 并发限流",
                      })}
                    </span>
                    <span className={styles.switchDescription}>
                      {t("sourceSystemConfigPage.llmRateLimiterDescription", {
                        defaultValue:
                          "控制当前系统请求使用的 LLM 并发、QPM、限流暂停和等待超时策略。",
                      })}
                    </span>
                  </div>
                  <Tag color={llmRateLimiterState.explicit ? "green" : "blue"}>
                    {llmRateLimiterState.explicit
                      ? t("sourceSystemConfigPage.explicitOverrideState", {
                          defaultValue: "当前系统显式覆盖",
                        })
                      : t("sourceSystemConfigPage.inheritedAgentState", {
                          defaultValue: "继承 Agent 运行配置",
                        })}
                  </Tag>
                </div>
                {llmRateLimiterState.explicit ? (
                  <>
                    <div className={styles.numberGrid}>
                      {LLM_RATE_LIMITER_NUMBER_FIELDS.map((definition) => (
                        <label
                          key={definition.key}
                          className={styles.numberField}
                        >
                          <span className={styles.numberLabel}>
                            {definition.title}
                          </span>
                          <InputNumber
                            min={definition.min}
                            step={definition.step}
                            value={
                              llmRateLimiterState.config[definition.key] ??
                              undefined
                            }
                            disabled={modelCallPolicyDisabled}
                            onChange={(value) => {
                              if (value === null && !definition.nullable) {
                                return;
                              }
                              handleLlmRateLimiterNumberChange(
                                definition.key,
                                (value ??
                                  null) as LlmRateLimiterConfig[typeof definition.key],
                              );
                            }}
                          />
                        </label>
                      ))}
                    </div>
                    <Button
                      disabled={modelCallPolicyDisabled}
                      onClick={() =>
                        handleRestoreModelCallPolicyInheritance(
                          "llm_rate_limiter",
                        )
                      }
                    >
                      {t("sourceSystemConfigPage.restoreInheritance", {
                        defaultValue: "恢复继承",
                      })}
                    </Button>
                  </>
                ) : (
                  <Button
                    disabled={modelCallPolicyDisabled}
                    onClick={() =>
                      handleEnableModelCallPolicy("llm_rate_limiter")
                    }
                  >
                    {t("sourceSystemConfigPage.enableOverride", {
                      defaultValue: "启用覆盖",
                    })}
                  </Button>
                )}
              </section>
            </Card>

            <Card
              id="system-config-cron"
              className={styles.switchCard}
              title={t("sourceSystemConfigPage.cronTaskSettingsTitle", {
                defaultValue: "定时任务设置",
              })}
            >
              <div className={styles.switchList}>
                <section className={styles.scheduledTaskSection}>
                  <div className={styles.switchRow}>
                    <div className={styles.switchCopy}>
                      <span className={styles.switchTitle}>
                        {t("sourceSystemConfigPage.cronUnreadAutoPauseTitle", {
                          defaultValue: "定时任务未读自动暂停",
                        })}
                      </span>
                      <span className={styles.switchDescription}>
                        {t(
                          "sourceSystemConfigPage.cronUnreadAutoPauseDescription",
                          {
                            defaultValue:
                              "开启后，当前渠道的定时任务连续产生未读结果达到阈值时会自动暂停。",
                          },
                        )}
                      </span>
                    </div>
                    <Switch
                      checked={cronUnreadAutoPauseConfig.enabled}
                      disabled={formDisabled}
                      onChange={handleCronUnreadAutoPauseEnabledChange}
                    />
                  </div>
                  <div className={styles.numberGrid}>
                    <label className={styles.numberField}>
                      <span className={styles.numberLabel}>
                        {t("sourceSystemConfigPage.cronUnreadPauseThreshold", {
                          defaultValue: "未读暂停条数",
                        })}
                      </span>
                      <InputNumber
                        min={1}
                        step={1}
                        value={cronUnreadAutoPauseConfig.threshold}
                        disabled={
                          formDisabled || !cronUnreadAutoPauseConfig.enabled
                        }
                        onChange={handleCronUnreadAutoPauseThresholdChange}
                      />
                    </label>
                  </div>
                </section>
                <section className={styles.scheduledTaskSection}>
                  <div className={styles.switchRow}>
                    <div className={styles.switchCopy}>
                      <span className={styles.switchTitle}>
                        {t(
                          "sourceSystemConfigPage.cronSkipWeekendZhaohuTitle",
                          {
                            defaultValue: "非工作日不发招呼完成通知",
                          },
                        )}
                      </span>
                      <span className={styles.switchDescription}>
                        {t(
                          "sourceSystemConfigPage.cronSkipWeekendZhaohuDescription",
                          {
                            defaultValue:
                              "新版任务按北京时间的原定通知日期（含延迟）判断非工作日；仅抑制招呼提醒，不影响执行结果和未读。旧任务保留原周末规则。",
                          },
                        )}
                      </span>
                    </div>
                    <Switch
                      checked={
                        cronNotificationConfig.skip_weekend_zhaohu_enabled
                      }
                      disabled={formDisabled}
                      onChange={handleCronSkipWeekendZhaohuEnabledChange}
                    />
                  </div>
                </section>
                <section className={styles.scheduledTaskSection}>
                  <div className={styles.switchRow}>
                    <div className={styles.switchCopy}>
                      <span className={styles.switchTitle}>
                        {t(
                          "sourceSystemConfigPage.cronTaskSessionCleanupTitle",
                          {
                            defaultValue: "定时任务会话历史清理",
                          },
                        )}
                      </span>
                      <span className={styles.switchDescription}>
                        {t(
                          "sourceSystemConfigPage.cronTaskSessionCleanupDescription",
                          {
                            defaultValue:
                              "每天按配置时间清理超过保留天数的定时任务会话历史。",
                          },
                        )}
                      </span>
                    </div>
                    <Switch
                      checked={cronTaskSessionCleanupConfig.enabled}
                      disabled={formDisabled}
                      onChange={handleCronTaskSessionCleanupEnabledChange}
                    />
                  </div>
                  <div className={styles.numberGrid}>
                    <label className={styles.numberField}>
                      <span className={styles.numberLabel}>
                        {t(
                          "sourceSystemConfigPage.cronTaskSessionCleanupRetention",
                          {
                            defaultValue: "历史保留天数",
                          },
                        )}
                      </span>
                      <InputNumber
                        min={1}
                        step={1}
                        value={cronTaskSessionCleanupConfig.retention_days}
                        disabled={
                          formDisabled || !cronTaskSessionCleanupConfig.enabled
                        }
                        onChange={handleCronTaskSessionCleanupRetentionChange}
                      />
                    </label>
                    <label className={styles.numberField}>
                      <span className={styles.numberLabel}>
                        {t(
                          "sourceSystemConfigPage.cronTaskSessionCleanupRunTime",
                          {
                            defaultValue: "每日运行时间",
                          },
                        )}
                      </span>
                      <Select
                        aria-label={t(
                          "sourceSystemConfigPage.cronTaskSessionCleanupRunTime",
                          {
                            defaultValue: "每日运行时间",
                          },
                        )}
                        value={cronTaskSessionCleanupConfig.run_time}
                        disabled={
                          formDisabled || !cronTaskSessionCleanupConfig.enabled
                        }
                        options={CRON_TASK_SESSION_CLEANUP_RUN_TIME_OPTIONS.map(
                          (runTime) => ({
                            label: runTime,
                            value: runTime,
                          }),
                        )}
                        onChange={handleCronTaskSessionCleanupRunTimeChange}
                      />
                    </label>
                  </div>
                </section>
                <section className={styles.scheduledTaskSection}>
                  <div className={styles.switchRow}>
                    <div className={styles.switchCopy}>
                      <span className={styles.switchTitle}>
                        {t("sourceSystemConfigPage.archiveMaintenanceTitle", {
                          defaultValue: "文件归档维护",
                        })}
                      </span>
                      <span className={styles.switchDescription}>
                        {t(
                          "sourceSystemConfigPage.archiveMaintenanceDescription",
                          {
                            defaultValue:
                              "每天按 source 维度归档旧孤儿文件，不会自动删除归档文件。",
                          },
                        )}
                      </span>
                    </div>
                    <Switch
                      checked={archiveMaintenanceConfig.enabled}
                      disabled={formDisabled}
                      onChange={handleArchiveMaintenanceEnabledChange}
                    />
                  </div>
                  <div className={styles.numberGrid}>
                    <label className={styles.numberField}>
                      <span className={styles.numberLabel}>
                        {t("sourceSystemConfigPage.archiveMaintenanceRunTime", {
                          defaultValue: "归档维护每日运行时间",
                        })}
                      </span>
                      <Select
                        aria-label={t(
                          "sourceSystemConfigPage.archiveMaintenanceRunTime",
                          {
                            defaultValue: "归档维护每日运行时间",
                          },
                        )}
                        value={archiveMaintenanceConfig.run_time}
                        disabled={
                          formDisabled || !archiveMaintenanceConfig.enabled
                        }
                        options={ARCHIVE_MAINTENANCE_RUN_TIME_OPTIONS.map(
                          (runTime) => ({
                            label: runTime,
                            value: runTime,
                          }),
                        )}
                        onChange={handleArchiveMaintenanceRunTimeChange}
                      />
                    </label>
                  </div>
                </section>
              </div>
              {canManage && (
                <SystemCronSyncStatus
                  context={syncContext}
                  scope="source"
                  taskTypes={["task_session_cleanup", "archive_maintenance"]}
                  submission={
                    syncReceipt?.source === activeSourceId
                      ? syncReceipt.value
                      : undefined
                  }
                />
              )}
            </Card>

            <Card
              id="system-config-output"
              className={styles.switchCard}
              title={t("sourceSystemConfigPage.toolResultCompactTitle", {
                defaultValue: "工具输出控制",
              })}
            >
              <div className={styles.toolResultIntro}>
                {t("sourceSystemConfigPage.toolResultCompactIntro", {
                  defaultValue:
                    "recent_max_bytes 同时控制新产生和近期的工具输出；old_max_bytes 仅控制历史工具输出。",
                })}
              </div>
              <section className={styles.toolOutputSection}>
                <div className={styles.toolOutputSectionHeader}>
                  <div className={styles.switchCopy}>
                    <span className={styles.switchTitle}>
                      {t("sourceSystemConfigPage.historyToolResultTitle", {
                        defaultValue: "历史工具结果压缩",
                      })}
                    </span>
                    <span className={styles.switchDescription}>
                      {t(
                        "sourceSystemConfigPage.historyToolResultDescription",
                        {
                          defaultValue:
                            "未保存系统覆盖时继承 Agent 配置；保存后当前系统下请求使用这些历史压缩阈值。",
                        },
                      )}
                    </span>
                  </div>
                </div>
                <div className={styles.switchRow}>
                  <div className={styles.switchCopy}>
                    <span className={styles.switchTitle}>
                      {t("sourceSystemConfigPage.toolResultEnabled", {
                        defaultValue: "启用工具结果压缩",
                      })}
                    </span>
                    <span className={styles.switchDescription}>
                      {t(
                        "sourceSystemConfigPage.toolResultEnabledDescription",
                        {
                          defaultValue:
                            "关闭后当前系统的历史工具结果不再压缩为 toolresult 文件。",
                        },
                      )}
                    </span>
                  </div>
                  <Switch
                    checked={toolResultCompactConfig.enabled}
                    disabled={formDisabled}
                    onChange={handleToolResultEnabledChange}
                  />
                </div>
                <div className={styles.numberGrid}>
                  {TOOL_RESULT_COMPACT_NUMBER_FIELDS.map((definition) => (
                    <label key={definition.key} className={styles.numberField}>
                      <span className={styles.numberLabel}>
                        {definition.title}
                      </span>
                      <InputNumber
                        min={definition.min}
                        max={definition.max}
                        step={definition.step}
                        value={toolResultCompactConfig[definition.key]}
                        disabled={formDisabled}
                        onChange={(value) =>
                          handleToolResultNumberChange(definition.key, value)
                        }
                      />
                    </label>
                  ))}
                </div>
              </section>
            </Card>

            <Modal
              cancelText="继续编辑"
              footer={(_, { CancelBtn }) => (
                <Space>
                  <CancelBtn />
                  <Button danger onClick={handleDiscardAndSwitchSource}>
                    放弃草稿并切换
                  </Button>
                  <Button
                    type="primary"
                    loading={saving}
                    onClick={() => void handleSave()}
                  >
                    保存并切换
                  </Button>
                </Space>
              )}
              open={pendingSourceId !== null}
              title="切换系统前保存修改？"
              onCancel={() => setPendingSourceId(null)}
            >
              当前系统的未保存修改会保留在编辑区。你可以先保存，再切换到目标系统。
            </Modal>

            <Modal
              cancelText="取消"
              confirmLoading={saving}
              okButtonProps={{ danger: true }}
              okText="恢复默认设置"
              open={restoreDefaultsPending}
              title="恢复当前系统的默认设置？"
              onCancel={() => {
                setRestoreDefaultsPending(false);
                setRestoreDefaultsSourceId(null);
              }}
              onOk={() => void handleDelete()}
            >
              这会移除当前系统已保存的全部自定义配置，并丢弃未保存的草稿修改，恢复继承默认值。此操作无法从此页撤销。
            </Modal>

            <Modal
              cancelText="保留防护"
              okButtonProps={{ danger: true }}
              okText="确认关闭"
              open={databaseGuardDisablePending}
              title="确认关闭数据库访问拦截"
              onCancel={() => setDatabaseGuardDisablePending(false)}
              onOk={confirmDatabaseGuardDisable}
            >
              关闭后，模型可通过 Python
              或命令行直连数据库。此修改会在统一保存后生效。
            </Modal>

            <div className={styles.actionRow}>
              {isDirty ? (
                <span className={styles.dirtySummary}>存在未保存修改</span>
              ) : null}
              {isDirty ? (
                <Button
                  onClick={() => {
                    setDraftConfig(record?.config ?? {});
                    setPromptSegments(
                      readSystemPromptInjections(record?.config ?? {}),
                    );
                  }}
                >
                  放弃修改
                </Button>
              ) : null}
              <span className={styles.restoreDefaultsHint}>
                清除自定义项并恢复继承默认值
              </span>
              <Button
                danger
                disabled={formDisabled || record?.is_default}
                onClick={() => {
                  setRestoreDefaultsSourceId(activeSourceId);
                  setRestoreDefaultsPending(true);
                }}
              >
                恢复默认设置
              </Button>
              <Button
                aria-label={t("common.save")}
                type="primary"
                loading={saving}
                disabled={formDisabled}
                onClick={handleSave}
              >
                保存全部修改
              </Button>
            </div>
          </>
        )}
      </div>
    </div>
  );
}
