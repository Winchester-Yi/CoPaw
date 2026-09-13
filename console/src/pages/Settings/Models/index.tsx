import { useCallback, useMemo, useState } from "react";
import { Button, ConfigProvider, Input, Modal } from "@agentscope-ai/design";
import {
  RightOutlined,
  SettingOutlined,
  PlusOutlined,
  SearchOutlined,
  SendOutlined,
} from "@ant-design/icons";
import { useProviders } from "./useProviders";
import {
  LoadingState,
  ProviderCard,
  CustomProviderModal,
  ModelsSection,
} from "./components";
import { useTranslation } from "react-i18next";
import { useAppMessage } from "@/hooks/useAppMessage";
import { useIframeStore } from "@/stores/iframeStore";
import { getUserId } from "@/utils/identity";
import { TenantSelector } from "@/components/TenantSelector";
import api from "@/api";
import { CONSOLE_MANAGEMENT_TOKENS } from "@/config/consoleDesignTokens";
import type { ProviderInfo } from "../../../api/types/provider";
import styles from "./index.module.less";

/* ------------------------------------------------------------------ */
/* Main Page                                                           */
/* ------------------------------------------------------------------ */

function ModelsPage() {
  const { t } = useTranslation();
  const { message } = useAppMessage();
  const manager = useIframeStore((state) => state.manager);
  const { providers, activeModels, loading, error, fetchAll } = useProviders();
  const [addProviderOpen, setAddProviderOpen] = useState(false);
  const [searchQuery, setSearchQuery] = useState("");

  // 供应商全量分发状态
  const [providersDistOpen, setProvidersDistOpen] = useState(false);
  const [providersDistSubmitting, setProvidersDistSubmitting] = useState(false);
  const [selectedProvidersDistTenantIds, setSelectedProvidersDistTenantIds] =
    useState<string[]>([]);
  const currentTenantId = getUserId();

  const refreshProvidersSilently = useCallback(() => {
    void fetchAll(false);
  }, [fetchAll]);

  const { regularProviders } = useMemo(() => {
    const regular: ProviderInfo[] = providers.filter((p) => !p.is_local);
    // Fuzzy search filter: match provider name (case-insensitive)
    const query = searchQuery.trim().toLowerCase();
    if (!query) {
      return { regularProviders: regular };
    }
    return {
      regularProviders: regular.filter((p) =>
        p.name.toLowerCase().includes(query),
      ),
    };
  }, [providers, searchQuery]);

  // ===== 供应商全量分发 =====

  const openProvidersDistModal = () => {
    setProvidersDistOpen(true);
    setSelectedProvidersDistTenantIds([]);
  };

  const closeProvidersDistModal = () => {
    if (providersDistSubmitting) return;
    setProvidersDistOpen(false);
    setSelectedProvidersDistTenantIds([]);
  };

  const handleDistributeProviders = async () => {
    if (!selectedProvidersDistTenantIds.length) return;

    setProvidersDistSubmitting(true);
    try {
      const result = await api.distributeProviders({
        target_tenant_ids: selectedProvidersDistTenantIds,
        overwrite: true,
      });
      message.success(`供应商分发任务已提交：${result.task_id}`);
      setProvidersDistOpen(false);
      setSelectedProvidersDistTenantIds([]);
    } catch (error) {
      const errMsg =
        error instanceof Error ? error.message : t("models.distributeFailed");
      message.error(errMsg);
    } finally {
      setProvidersDistSubmitting(false);
    }
  };

  const renderProviderCards = (list: ProviderInfo[]) =>
    list.map((provider) => (
      <ProviderCard
        key={provider.id}
        provider={provider}
        activeModels={activeModels}
        onSaved={refreshProvidersSilently}
      />
    ));

  const visibleProviderCount = regularProviders.length;

  return (
    <ConfigProvider
      theme={{
        token: {
          colorPrimary: CONSOLE_MANAGEMENT_TOKENS.colorPrimary,
          colorLink: CONSOLE_MANAGEMENT_TOKENS.colorPrimaryHover,
          colorText: CONSOLE_MANAGEMENT_TOKENS.colorText,
          colorTextSecondary: CONSOLE_MANAGEMENT_TOKENS.colorTextSecondary,
          colorBorder: CONSOLE_MANAGEMENT_TOKENS.colorBorder,
          colorBgLayout: CONSOLE_MANAGEMENT_TOKENS.colorCanvas,
          colorBgContainer: CONSOLE_MANAGEMENT_TOKENS.colorSurface,
          colorBgElevated: CONSOLE_MANAGEMENT_TOKENS.colorSurface,
          fontFamily: CONSOLE_MANAGEMENT_TOKENS.fontUi,
          borderRadius: 8,
        },
        components: {
          Select: {
            optionActiveBg: CONSOLE_MANAGEMENT_TOKENS.colorSurfaceSubtle,
            optionSelectedBg: CONSOLE_MANAGEMENT_TOKENS.colorPrimarySoft,
            optionSelectedColor: CONSOLE_MANAGEMENT_TOKENS.colorText,
          },
        },
      }}
    >
      <div className={`${styles.settingsPage} console-management-theme`}>
        {loading ? (
          <LoadingState message={t("models.loading")} />
        ) : error ? (
          <LoadingState message={error} error onRetry={fetchAll} />
        ) : (
          <>
            <div className={styles.pageHeading}>
              <div className={styles.pageHeadingIcon} aria-hidden="true">
                <SettingOutlined />
              </div>
              <nav
                className={styles.breadcrumbTrail}
                aria-label={t("models.breadcrumbLabel")}
              >
                <span className={styles.pageEyebrow}>
                  {t("nav.systemSettings")}
                </span>
                <RightOutlined className={styles.breadcrumbChevron} />
                <span className={styles.breadcrumbCurrent} aria-current="page">
                  {t("nav.models")}
                </span>
              </nav>
              <h1 className={styles.visuallyHiddenHeading}>
                {t("nav.models")}
              </h1>
            </div>
            <div className={styles.content}>
              <div className={styles.contentInner}>
                <section className={styles.managementSection}>
                  <div className={styles.managementSectionHeader}>
                    <div>
                      <h2 className={styles.managementSectionTitle}>
                        {t("models.llmTitle")}
                      </h2>
                      <p className={styles.managementSectionDescription}>
                        {t("models.llmDescription")}
                      </p>
                    </div>
                  </div>
                  <ModelsSection
                    providers={providers}
                    activeModels={activeModels}
                    onSaved={fetchAll}
                  />
                </section>

                <section
                  className={`${styles.managementSection} ${styles.providersBlock}`}
                >
                  <div className={styles.sectionHeaderRow}>
                    <div>
                      <h2 className={styles.managementSectionTitle}>
                        {t("models.providersTitle")}
                      </h2>
                      <p className={styles.managementSectionDescription}>
                        {t("models.providersDescription")}
                      </p>
                    </div>
                    <div className={styles.headerRight}>
                      <div className={styles.searchRow}>
                        <Input
                          placeholder={t("models.searchPlaceholder")}
                          value={searchQuery}
                          onChange={(e) => setSearchQuery(e.target.value)}
                          onPressEnter={() => {}}
                          className={styles.searchInput}
                          prefix={<SearchOutlined />}
                          allowClear
                        />
                        <Button
                          type="primary"
                          icon={<SearchOutlined />}
                          onClick={() => fetchAll()}
                          className={styles.searchBtn}
                        >
                          {t("models.search")}
                        </Button>
                      </div>
                      <Button
                        type="primary"
                        icon={<PlusOutlined />}
                        onClick={() => setAddProviderOpen(true)}
                        className={styles.addProviderBtn}
                      >
                        {t("models.addProvider")}
                      </Button>
                      <Button
                        icon={<SendOutlined />}
                        onClick={openProvidersDistModal}
                        className={styles.addProviderBtn}
                        disabled={!manager}
                      >
                        {t("models.distributeProviders")}
                      </Button>
                    </div>
                  </div>

                  {visibleProviderCount === 0 ? (
                    <div className={styles.emptyState}>
                      <div className={styles.emptyStateTitle}>
                        {t("models.noProviders", "没有匹配的提供商")}
                      </div>
                      <div className={styles.emptyStateDescription}>
                        {t(
                          "models.noProvidersHint",
                          "请调整搜索条件，或添加一个自定义提供商。",
                        )}
                      </div>
                    </div>
                  ) : null}

                  {regularProviders.length > 0 && (
                    <div className={styles.providerGroup}>
                      <div className={styles.providerCards}>
                        {renderProviderCards(regularProviders)}
                      </div>
                    </div>
                  )}
                </section>
              </div>

              <CustomProviderModal
                open={addProviderOpen}
                onClose={() => setAddProviderOpen(false)}
                onSaved={fetchAll}
              />

              {/* 供应商全量分发 Modal */}
              <Modal
                rootClassName="console-management-modal"
                open={providersDistOpen}
                title={t("models.distributeProvidersTitle")}
                onCancel={closeProvidersDistModal}
                onOk={handleDistributeProviders}
                okButtonProps={{
                  disabled: !selectedProvidersDistTenantIds.length,
                  loading: providersDistSubmitting,
                }}
              >
                <div className={styles.modalStack}>
                  <div className={styles.modalHint}>
                    {t("models.distributeProvidersHint")}
                  </div>
                  <div className={styles.dangerNotice}>
                    {t("models.distributeProvidersWarning")}
                  </div>
                  <TenantSelector
                    selectedTenantIds={selectedProvidersDistTenantIds}
                    onChange={setSelectedProvidersDistTenantIds}
                    excludeTenantId={currentTenantId}
                  />
                </div>
              </Modal>
            </div>
          </>
        )}
      </div>
    </ConfigProvider>
  );
}

export default ModelsPage;
