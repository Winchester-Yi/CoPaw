import { useEffect, useMemo, useState } from "react";
import { SendOutlined } from "@ant-design/icons";
import { Button, Modal, Select } from "@agentscope-ai/design";
import api from "../../../../../api";
import { useTranslation } from "react-i18next";
import { useAppMessage } from "../../../../../hooks/useAppMessage";
import { useIframeStore } from "../../../../../stores/iframeStore";
import { getUserId } from "../../../../../utils/identity";
import { TenantSelector } from "../../../../../components/TenantSelector";
import { getRemoteProviders } from "../../modelManagement";
import { ModelRuntimeConfigModal } from "../modals/ModelRuntimeConfigModal";
import type { ActiveModelsInfo, ProviderInfo } from "../../../../../api/types";
import styles from "../../index.module.less";

interface ModelsSectionProps {
  providers: ProviderInfo[];
  activeModels: ActiveModelsInfo | null;
  onSaved?: () => void | Promise<void>;
}

export function ModelsSection({
  providers,
  activeModels,
  onSaved,
}: ModelsSectionProps) {
  const { t } = useTranslation();
  const manager = useIframeStore((state) => state.manager);
  const [distributionOpen, setDistributionOpen] = useState(false);
  const [distributionSubmitting, setDistributionSubmitting] = useState(false);
  const [selectedDistributionTenantIds, setSelectedDistributionTenantIds] =
    useState<string[]>([]);
  const [configOpen, setConfigOpen] = useState(false);
  const [activating, setActivating] = useState(false);
  const currentTenantId = getUserId();

  const { message } = useAppMessage();

  const currentSlot = activeModels?.active_llm;
  const [optimisticActiveModel, setOptimisticActiveModel] =
    useState(currentSlot);
  useEffect(() => {
    setOptimisticActiveModel(currentSlot);
  }, [currentSlot]);
  const selectedActiveModel = optimisticActiveModel ?? currentSlot;
  const eligibleProviders = useMemo(
    () =>
      getRemoteProviders(providers).filter((provider) => {
        const hasModels =
          (provider.models?.length ?? 0) +
            (provider.extra_models?.length ?? 0) >
          0;
        if (!hasModels) return false;
        if (provider.require_api_key === false) return !!provider.base_url;
        if (provider.is_custom) return !!provider.base_url;
        return !!provider.api_key;
      }),
    [providers],
  );
  const currentProvider = eligibleProviders.find(
    (p) =>
      p.id === selectedActiveModel?.provider_id &&
      [...(p.models ?? []), ...(p.extra_models ?? [])].some(
        (model) => model.id === selectedActiveModel?.model,
      ),
  );
  const activeRemoteModel = currentProvider ? selectedActiveModel : undefined;
  const modelOptions = useMemo(
    () =>
      eligibleProviders.map((provider) => ({
        label: provider.name,
        options: [
          ...(provider.models ?? []),
          ...(provider.extra_models ?? []),
        ].map((model) => ({
          value: `${provider.id}:${model.id}`,
          label: `${model.name} (${model.id})`,
        })),
      })),
    [eligibleProviders],
  );
  const selectedModelValue =
    activeRemoteModel?.provider_id && activeRemoteModel.model
      ? `${activeRemoteModel.provider_id}:${activeRemoteModel.model}`
      : undefined;
  const selectedModelLabel =
    currentProvider && activeRemoteModel?.model
      ? `${currentProvider.name} · ${
          [
            ...(currentProvider.models ?? []),
            ...(currentProvider.extra_models ?? []),
          ].find((model) => model.id === activeRemoteModel.model)?.name ||
          activeRemoteModel.model
        }`
      : undefined;
  const unavailableModelLabel =
    !currentProvider &&
    selectedActiveModel?.provider_id &&
    selectedActiveModel.model
      ? `${t("models.currentModelUnavailable", "当前模型不可用")}：${
          selectedActiveModel.provider_id
        } · ${selectedActiveModel.model}`
      : undefined;

  const handleModelChange = async (value: string) => {
    if (activating) return;
    const separator = value.indexOf(":");
    if (separator < 1) return;
    const providerId = value.slice(0, separator);
    const modelId = value.slice(separator + 1);
    if (
      providerId === selectedActiveModel?.provider_id &&
      modelId === selectedActiveModel?.model
    ) {
      return;
    }
    setActivating(true);
    try {
      await api.setActiveLlm({
        provider_id: providerId,
        model: modelId,
        scope: "global",
      });
      setOptimisticActiveModel({ provider_id: providerId, model: modelId });
      try {
        await onSaved?.();
      } catch (error) {
        console.error("ModelsSection: failed to refresh model data", error);
      }
    } catch (error) {
      message.error(
        error instanceof Error ? error.message : t("models.failedToSave"),
      );
    } finally {
      setActivating(false);
    }
  };

  const openDistributionModal = () => {
    if (!activeRemoteModel?.provider_id || !activeRemoteModel?.model) return;

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

  const canDistribute =
    manager && !!activeRemoteModel?.provider_id && !!activeRemoteModel?.model;

  return (
    <div className={styles.slotSection}>
      <div className={styles.defaultLlmControls}>
        <div className={styles.defaultLlmSelection}>
          <label>{t("models.model")}</label>
          <Select
            showSearch
            value={selectedModelValue}
            placeholder={t("models.selectModel")}
            options={modelOptions}
            optionFilterProp="label"
            loading={activating}
            disabled={modelOptions.length === 0 || activating}
            onChange={handleModelChange}
            optionRender={(option) => option.label}
          />
          <div className={styles.defaultLlmCurrentValue}>
            {selectedModelLabel ||
              unavailableModelLabel ||
              t("models.noAvailableRemoteModel")}
          </div>
        </div>
        <div className={styles.defaultLlmActions}>
          <Button
            disabled={
              !activeRemoteModel?.provider_id || !activeRemoteModel.model
            }
            onClick={() => setConfigOpen(true)}
          >
            {t("models.configure", "配置")}
          </Button>
          <Button
            disabled={!canDistribute}
            onClick={openDistributionModal}
            icon={<SendOutlined />}
          >
            {t("models.distribute")}
          </Button>
        </div>
      </div>

      {currentProvider && activeRemoteModel?.model && (
        <ModelRuntimeConfigModal
          provider={currentProvider}
          modelId={activeRemoteModel.model}
          open={configOpen}
          onClose={() => setConfigOpen(false)}
          onSaved={onSaved}
        />
      )}

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
              provider:
                currentProvider?.name || activeRemoteModel?.provider_id || "",
              model: activeRemoteModel?.model || "",
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
