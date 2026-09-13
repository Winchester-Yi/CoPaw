import { useState } from "react";
import { Card, Button, Dropdown, Modal } from "@agentscope-ai/design";
import {
  AppstoreOutlined,
  DeleteOutlined,
  MoreOutlined,
  SettingOutlined,
} from "@ant-design/icons";
import type { ProviderInfo, ActiveModelsInfo } from "../../../../../api/types";
import { ProviderConfigModal } from "../modals/ProviderConfigModal";
import { RemoteModelManageModal } from "../modals/RemoteModelManageModal";
import api from "../../../../../api";
import { useTranslation } from "react-i18next";
import { useAppMessage } from "../../../../../hooks/useAppMessage";
import styles from "../../index.module.less";
import { providerIcon } from "../providerIcon";

// export const PROVIDER_IMG_MAP = {

// }

interface RemoteProviderCardProps {
  provider: ProviderInfo;
  activeModels: ActiveModelsInfo | null;
  onSaved: () => void;
}

export function RemoteProviderCard({
  provider,
  activeModels,
  onSaved,
}: RemoteProviderCardProps) {
  const { t } = useTranslation();
  const { message } = useAppMessage();
  const [modalOpen, setModalOpen] = useState(false);
  const [modelManageOpen, setModelManageOpen] = useState(false);

  const handleDeleteProvider = () => {
    Modal.confirm({
      title: t("models.deleteProvider"),
      content: t("models.deleteProviderConfirm", { name: provider.name }),
      okText: t("common.delete"),
      okButtonProps: { danger: true },
      cancelText: t("models.cancel"),
      onOk: async () => {
        try {
          await api.deleteCustomProvider(provider.id);
          message.success(t("models.providerDeleted", { name: provider.name }));
          onSaved();
        } catch (error) {
          const errMsg =
            error instanceof Error
              ? error.message
              : t("models.providerDeleteFailed");
          message.error(errMsg);
        }
      },
    });
  };

  const totalCount = provider.models.length + provider.extra_models.length;

  let isConfigured = false;

  if (provider.is_custom && provider.base_url) {
    isConfigured = true;
  } else if (provider.require_api_key === false) {
    isConfigured = true;
  } else if (provider.require_api_key && provider.api_key) {
    isConfigured = true;
  }

  const hasModels = totalCount > 0;
  const isAvailable = isConfigured && hasModels;

  const providerTag = provider.is_custom ? (
    <span className={styles.customTag}>{t("models.custom")}</span>
  ) : (
    <span className={styles.builtinTag}>{t("models.builtin")}</span>
  );

  const statusLabel = isAvailable
    ? t("models.providerAvailable")
    : isConfigured
    ? t("models.providerNoModels")
    : t("models.providerNotConfigured");
  const statusType = isAvailable
    ? "enabled"
    : isConfigured
    ? "partial"
    : "disabled";
  const statusDotColor = isAvailable
    ? "rgba(20, 184, 166, 1)"
    : isConfigured
    ? "#faad14"
    : "#d9d9d9";
  const statusDotShadow = isAvailable
    ? "0 0 0 2px rgba(82, 196, 26, 0.2)"
    : isConfigured
    ? "0 0 0 2px rgba(250, 173, 20, 0.2)"
    : "none";

  return (
    <Card
      className={`${styles.providerCard} ${
        isAvailable ? styles.enabledCard : ""
      }`}
    >
      <div className={styles.cardIdentity}>
        <div className={styles.providerBrand}>
          <div className={styles.providerIconFrame}>
            <img
              src={providerIcon(provider.id)}
              alt={provider.name}
              className={styles.providerIcon}
            />
          </div>
          <div className={styles.providerTitleText}>
            <div className={styles.providerNameLine} title={provider.name}>
              <span className={styles.cardName}>{provider.name}</span>
              {providerTag}
            </div>
            <div className={styles.providerIdLine} title={provider.id}>
              <span className={styles.providerFieldLabel}>ID</span>
              <span className={styles.providerIdText}>{provider.id}</span>
            </div>
          </div>
        </div>
        <div className={styles.cardStatusHeader}>
          <span
            className={styles.statusDot}
            style={{
              backgroundColor: statusDotColor,
              boxShadow: statusDotShadow,
            }}
          />
          <span
            className={`${styles.statusText} ${
              statusType === "enabled"
                ? styles.enabled
                : statusType === "partial"
                ? styles.partial
                : styles.disabled
            }`}
          >
            {statusLabel}
          </span>
        </div>
      </div>

      <div className={styles.cardInfo}>
        <div className={styles.infoRow}>
          <span className={styles.infoLabel}>Base URL:</span>
          {provider.base_url ? (
            <span className={styles.infoValue} title={provider.base_url}>
              {provider.base_url}
            </span>
          ) : (
            <span className={styles.infoEmpty}>{t("models.notSet")}</span>
          )}
        </div>
        <div className={styles.infoRow}>
          <span className={styles.infoLabel}>API Key:</span>
          {provider.api_key ? (
            <span className={styles.infoValue}>{provider.api_key}</span>
          ) : (
            <span className={styles.infoEmpty}>{t("models.notSet")}</span>
          )}
        </div>
        <div className={styles.infoRow}>
          <span className={styles.infoLabel}>{t("models.model")}:</span>
          <span className={styles.infoValue}>
            {totalCount > 0
              ? t("models.modelsCount", { count: totalCount })
              : t("models.noModels")}
          </span>
        </div>
      </div>

      <div className={styles.cardActions}>
        <Button
          type="text"
          size="small"
          icon={<AppstoreOutlined />}
          onClick={() => setModelManageOpen(true)}
          className={styles.actionBtn}
        >
          {t("models.models")}
        </Button>
        <Button
          type="text"
          size="small"
          icon={<SettingOutlined />}
          onClick={() => setModalOpen(true)}
          className={styles.actionBtn}
        >
          {t("models.settings")}
        </Button>
        {provider.is_custom && (
          <Dropdown
            trigger={["click"]}
            menu={{
              items: [
                {
                  key: "delete",
                  danger: true,
                  icon: <DeleteOutlined />,
                  label: t("common.delete"),
                  onClick: handleDeleteProvider,
                },
              ],
            }}
          >
            <Button
              type="text"
              size="small"
              icon={<MoreOutlined />}
              aria-label={t("models.moreActions", "更多操作")}
              className={styles.moreActionBtn}
            />
          </Dropdown>
        )}
      </div>

      <ProviderConfigModal
        provider={provider}
        activeModels={activeModels}
        open={modalOpen}
        onClose={() => setModalOpen(false)}
        onSaved={onSaved}
      />
      <RemoteModelManageModal
        provider={provider}
        activeModels={activeModels}
        open={modelManageOpen}
        onClose={() => setModelManageOpen(false)}
        onSaved={onSaved}
      />
    </Card>
  );
}
