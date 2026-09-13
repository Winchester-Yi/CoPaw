import { useState, useEffect, useMemo } from "react";
import { SaveOutlined, SendOutlined } from "@ant-design/icons";
import { Select, Button, Modal } from "@agentscope-ai/design";
import type { ModelSlotRequest } from "../../../../../api/types";
import api from "../../../../../api";
import { useTranslation } from "react-i18next";
import { useAppMessage } from "../../../../../hooks/useAppMessage";
import { useIframeStore } from "../../../../../stores/iframeStore";
import { getUserId } from "../../../../../utils/identity";
import { TenantSelector } from "../../../../../components/TenantSelector";
import { getRemoteProviders } from "../../modelManagement";
import styles from "../../index.module.less";

interface ModelsSectionProps {
  providers: Array<{
    id: string;
    name: string;
    models?: Array<{ id: string; name: string }>;
    extra_models?: Array<{ id: string; name: string }>;
    base_url?: string;
    api_key?: string;
    is_custom: boolean;
    is_local?: boolean;
    require_api_key?: boolean;
  }>;
  activeModels: {
    active_llm?: {
      provider_id?: string;
      model?: string;
    };
  } | null;
  onSaved: () => void;
}

export function ModelsSection({
  providers,
  activeModels,
  onSaved,
}: ModelsSectionProps) {
  const { t } = useTranslation();
  const manager = useIframeStore((state) => state.manager);
  const [saving, setSaving] = useState(false);
  const [selectedProviderId, setSelectedProviderId] = useState<
    string | undefined
  >(undefined);
  const [selectedModel, setSelectedModel] = useState<string | undefined>(
    undefined,
  );
  const [dirty, setDirty] = useState(false);
  const [distributionOpen, setDistributionOpen] = useState(false);
  const [distributionSubmitting, setDistributionSubmitting] = useState(false);
  const [selectedDistributionTenantIds, setSelectedDistributionTenantIds] =
    useState<string[]>([]);
  const currentTenantId = getUserId();

  const { message } = useAppMessage();

  const currentSlot = activeModels?.active_llm;

  const eligible = useMemo(
    () =>
      getRemoteProviders(providers).filter((p) => {
        const hasModels =
          (p.models?.length ?? 0) + (p.extra_models?.length ?? 0) > 0;
        if (!hasModels) return false;
        if (p.require_api_key === false) return !!p.base_url;
        if (p.is_custom) return !!p.base_url;
        if (p.require_api_key ?? true) return !!p.api_key;
        return true;
      }),
    [providers],
  );

  useEffect(() => {
    if (currentSlot) {
      setSelectedProviderId(currentSlot.provider_id || undefined);
      setSelectedModel(currentSlot.model || undefined);
    }
    setDirty(false);
  }, [currentSlot]);

  const chosenProvider = providers.find((p) => p.id === selectedProviderId);
  const currentProvider = providers.find(
    (p) => p.id === currentSlot?.provider_id,
  );
  const modelOptions = [
    ...(chosenProvider?.models ?? []),
    ...(chosenProvider?.extra_models ?? []),
  ];
  const hasModels = modelOptions.length > 0;

  const handleProviderChange = (pid: string) => {
    setSelectedProviderId(pid);
    setSelectedModel(undefined);
    setDirty(true);
  };

  const handleModelChange = (model: string) => {
    setSelectedModel(model);
    setDirty(true);
  };

  const handleSave = async () => {
    if (!selectedProviderId || !selectedModel) return;

    const body: ModelSlotRequest = {
      provider_id: selectedProviderId,
      model: selectedModel,
      scope: "global",
    };

    setSaving(true);
    try {
      await api.setActiveLlm(body);
      message.success(t("models.llmModelUpdated"));
      setDirty(false);
      onSaved();
    } catch (error) {
      const errMsg =
        error instanceof Error ? error.message : t("models.failedToSave");
      message.error(errMsg);
    } finally {
      setSaving(false);
    }
  };

  const openDistributionModal = () => {
    if (!currentSlot?.provider_id || !currentSlot?.model) return;

    if (dirty) {
      Modal.warning({
        title: t("models.distributeDirtyTitle"),
        content: t("models.distributeDirtyHint"),
        okText: t("common.confirm"),
      });
      return;
    }

    setDistributionOpen(true);
    setSelectedDistributionTenantIds([]);
  };

  const closeDistributionModal = () => {
    if (distributionSubmitting) return;
    setDistributionOpen(false);
    setSelectedDistributionTenantIds([]);
  };

  const handleDistributeActiveModel = async () => {
    if (!selectedDistributionTenantIds.length) return;

    setDistributionSubmitting(true);
    try {
      const result = await api.distributeActiveLlm({
        target_tenant_ids: selectedDistributionTenantIds,
        overwrite: true,
      });
      message.success(`模型分发任务已提交：${result.task_id}`);
      setDistributionOpen(false);
      setSelectedDistributionTenantIds([]);
    } catch (error) {
      const errMsg =
        error instanceof Error ? error.message : t("models.distributeFailed");
      message.error(errMsg);
    } finally {
      setDistributionSubmitting(false);
    }
  };

  const isActive =
    currentSlot &&
    currentSlot.provider_id === selectedProviderId &&
    currentSlot.model === selectedModel;
  const canSave = dirty && !!selectedProviderId && !!selectedModel;
  const canDistribute =
    manager && !!currentSlot?.provider_id && !!currentSlot?.model;

  return (
    <div className={styles.slotSection}>
      <div className={styles.slotForm}>
        <div className={styles.slotField}>
          <label className={styles.slotLabel}>{t("models.provider")}</label>
          <Select
            style={{ width: "100%" }}
            popupClassName={styles.managementSelectDropdown}
            placeholder={t("models.selectProvider")}
            value={selectedProviderId}
            onChange={handleProviderChange}
            options={eligible.map((p) => ({
              value: p.id,
              label: p.name,
            }))}
          />
        </div>

        <div className={styles.slotField}>
          <label className={styles.slotLabel}>{t("models.model")}</label>
          <Select
            style={{ width: "100%" }}
            popupClassName={styles.managementSelectDropdown}
            placeholder={
              hasModels ? t("models.selectModel") : t("models.addModelFirst")
            }
            disabled={!hasModels}
            showSearch
            optionFilterProp="label"
            value={selectedModel}
            onChange={handleModelChange}
            options={modelOptions.map((m) => ({
              value: m.id,
              label: `${m.name} (${m.id})`,
            }))}
          />
        </div>

        <div className={`${styles.slotField} ${styles.slotActions}`}>
          <label
            className={`${styles.slotLabel} ${styles.visuallyHiddenLabel}`}
          >
            {t("models.actions")}
          </label>
          <div className={styles.slotActionButtons}>
            <Button
              type="primary"
              loading={saving}
              disabled={!canSave}
              onClick={handleSave}
              block
              icon={<SaveOutlined />}
            >
              {isActive ? t("models.saved") : t("models.save")}
            </Button>
            <Button
              disabled={!canDistribute}
              onClick={openDistributionModal}
              block
              icon={<SendOutlined />}
            >
              {t("models.distribute")}
            </Button>
          </div>
        </div>
      </div>

      <Modal
        rootClassName="console-management-modal"
        open={distributionOpen}
        title={t("models.distributeTitle")}
        onCancel={closeDistributionModal}
        onOk={handleDistributeActiveModel}
        okButtonProps={{
          disabled: !selectedDistributionTenantIds.length,
          loading: distributionSubmitting,
        }}
      >
        <div className={styles.modalStack}>
          <div className={styles.modalHint}>{t("models.distributeHint")}</div>
          <div className={styles.modalCurrentValue}>
            {t("models.distributeCurrentSource", {
              provider: currentProvider?.name || currentSlot?.provider_id || "",
              model: currentSlot?.model || "",
            })}
          </div>
          <div className={styles.warningNotice}>
            {t("models.distributeOverwriteWarning")}
          </div>
          <TenantSelector
            selectedTenantIds={selectedDistributionTenantIds}
            onChange={setSelectedDistributionTenantIds}
            excludeTenantId={currentTenantId}
          />
        </div>
      </Modal>
    </div>
  );
}
