import { useState } from "react";
import { SendOutlined } from "@ant-design/icons";
import { Button, Modal } from "@agentscope-ai/design";
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
    is_local?: boolean;
  }>;
  activeModels: {
    active_llm?: {
      provider_id?: string;
      model?: string;
    };
  } | null;
}

export function ModelsSection({ providers, activeModels }: ModelsSectionProps) {
  const { t } = useTranslation();
  const manager = useIframeStore((state) => state.manager);
  const [distributionOpen, setDistributionOpen] = useState(false);
  const [distributionSubmitting, setDistributionSubmitting] = useState(false);
  const [selectedDistributionTenantIds, setSelectedDistributionTenantIds] =
    useState<string[]>([]);
  const currentTenantId = getUserId();

  const { message } = useAppMessage();

  const currentSlot = activeModels?.active_llm;
  const currentProvider = getRemoteProviders(providers).find(
    (p) => p.id === currentSlot?.provider_id,
  );
  const activeRemoteModel = currentProvider ? currentSlot : undefined;

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
      <div className={styles.slotActions}>
        <Button
          disabled={!canDistribute}
          onClick={openDistributionModal}
          icon={<SendOutlined />}
        >
          {t("models.distribute")}
        </Button>
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
