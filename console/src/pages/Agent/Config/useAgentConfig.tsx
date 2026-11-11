import { useState, useEffect, useCallback } from "react";
import { Form, Modal } from "@agentscope-ai/design";
import { useTranslation } from "react-i18next";
import api from "../../../api";
import type { AgentsRunningConfig } from "../../../api/types";
import { useAppMessage } from "../../../hooks/useAppMessage";
import { getUserId } from "../../../utils/identity";
import { useIframeStore } from "../../../stores/iframeStore";
import { getCurrentCronContext, useCronContext } from "@/hooks/useCronContext";
import type { SystemSyncSubmission } from "@/api/types/systemCron";

export function useAgentConfig() {
  const { t } = useTranslation();
  const { message } = useAppMessage();
  const syncContext = useCronContext();
  const scopeKey = JSON.stringify(syncContext);
  const [syncReceipt, setSyncReceipt] = useState<{
    scope: string;
    value: SystemSyncSubmission;
  } | null>(null);
  const [form] = Form.useForm();
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [language, setLanguage] = useState<string>("zh");
  const [savingLang, setSavingLang] = useState(false);
  const [timezone, setTimezone] = useState<string>("UTC");
  const [savingTimezone, setSavingTimezone] = useState(false);

  const fetchConfig = useCallback(async () => {
    setLoading(true);
    setSaving(false);
    setError(null);
    try {
      const [config, langResp, tzResp] = await Promise.all([
        api.getAgentRunningConfig(),
        api.getAgentLanguage(),
        api.getUserTimezone(),
      ]);
      if (JSON.stringify(getCurrentCronContext()) !== scopeKey) return;
      form.setFieldsValue(config);
      setLanguage(langResp.language);
      setTimezone(tzResp.timezone || "UTC");
    } catch (err) {
      if (JSON.stringify(getCurrentCronContext()) !== scopeKey) return;
      const errMsg =
        err instanceof Error ? err.message : t("agentConfig.loadFailed");
      setError(errMsg);
    } finally {
      if (JSON.stringify(getCurrentCronContext()) === scopeKey)
        setLoading(false);
    }
  }, [form, t, scopeKey]);

  useEffect(() => {
    fetchConfig();
  }, [fetchConfig]);

  const handleSave = useCallback(async () => {
    try {
      const values = await form.validateFields();
      setSaving(true);
      const saved = await api.updateAgentRunningConfig(
        values as AgentsRunningConfig,
      );
      if (JSON.stringify(getCurrentCronContext()) !== scopeKey) return;
      const submission = saved?.scheduler_sync ?? {
        submission_status: "failed" as const,
        error_code: "system_sync_receipt_unavailable",
      };
      setSyncReceipt({ scope: scopeKey, value: submission });
      if (submission.submission_status === "accepted")
        message.success("配置已保存，平台同步已受理");
      else message.warning?.("配置已保存，但平台同步未受理，请查看同步状态");
    } catch (err) {
      if (JSON.stringify(getCurrentCronContext()) !== scopeKey) return;
      if (err instanceof Error && "errorFields" in err) return;
      const errMsg =
        err instanceof Error ? err.message : t("agentConfig.saveFailed");
      message.error(errMsg);
    } finally {
      if (JSON.stringify(getCurrentCronContext()) === scopeKey)
        setSaving(false);
    }
  }, [form, t, scopeKey, message]);

  const handleLanguageChange = useCallback(
    (value: string): void => {
      if (value === language) return;
      Modal.confirm({
        title: t("agentConfig.languageConfirmTitle"),
        content: (
          <span style={{ whiteSpace: "pre-line" }}>
            {t("agentConfig.languageConfirmContent")}
          </span>
        ),
        okText: t("agentConfig.languageConfirmOk"),
        cancelText: t("common.cancel"),
        onOk: async () => {
          setSavingLang(true);
          try {
            const resp = await api.updateAgentLanguage(value);
            setLanguage(resp.language);
            if (resp.copied_files && resp.copied_files.length > 0) {
              message.success(
                t("agentConfig.languageSaveSuccessWithFiles", {
                  count: resp.copied_files.length,
                }),
              );
            } else {
              message.success(t("agentConfig.languageSaveSuccess"));
            }
          } catch (err) {
            const errMsg =
              err instanceof Error
                ? err.message
                : t("agentConfig.languageSaveFailed");
            message.error(errMsg);
          } finally {
            setSavingLang(false);
          }
        },
      });
    },
    [language, t],
  );

  const handleTimezoneChange = useCallback(
    async (value: string) => {
      if (value === timezone) return;
      setSavingTimezone(true);
      try {
        await api.updateUserTimezone(value);
        setTimezone(value);
        message.success(t("agentConfig.timezoneSaveSuccess"));
      } catch (err) {
        const errMsg =
          err instanceof Error
            ? err.message
            : t("agentConfig.timezoneSaveFailed");
        message.error(errMsg);
      } finally {
        setSavingTimezone(false);
      }
    },
    [timezone, t],
  );

  // 分发相关状态
  const [distributeModalOpen, setDistributeModalOpen] = useState(false);
  const [currentConfigGroup, setCurrentConfigGroup] = useState("");
  const [currentConfigGroupLabel, setCurrentConfigGroupLabel] = useState("");
  const currentTenantId = getUserId();
  const { manager } = useIframeStore();

  const openDistributeModal = useCallback(
    (configGroup: string, label: string) => {
      setCurrentConfigGroup(configGroup);
      setCurrentConfigGroupLabel(label);
      setDistributeModalOpen(true);
    },
    [],
  );

  const closeDistributeModal = useCallback(() => {
    setDistributeModalOpen(false);
    setCurrentConfigGroup("");
    setCurrentConfigGroupLabel("");
  }, []);

  const canDistribute = manager || currentTenantId === "default";

  return {
    syncContext,
    syncSubmission:
      syncReceipt?.scope === scopeKey ? syncReceipt.value : undefined,
    form,
    loading,
    saving,
    error,
    language,
    savingLang,
    timezone,
    savingTimezone,
    fetchConfig,
    handleSave,
    handleLanguageChange,
    handleTimezoneChange,
    distributeModalOpen,
    currentConfigGroup,
    currentConfigGroupLabel,
    openDistributeModal,
    closeDistributeModal,
    canDistribute,
  };
}
