import { Button, Form, Tooltip } from "@agentscope-ai/design";
import { SendOutlined } from "@ant-design/icons";
import { useTranslation } from "react-i18next";
import { useAgentConfig } from "./useAgentConfig.tsx";
import {
  ReactAgentCard,
  LlmRetryCard,
  QueryRetryCard,
  LlmRateLimiterCard,
  ContextCompactCard,
  MemorySummaryCard,
  DistributeModal,
} from "./components";
import { PageHeader } from "@/components/PageHeader";
import styles from "./index.module.less";

/** 配置组 key → 中文显示名称映射 */
const CONFIG_GROUP_LABELS: Record<string, string> = {
  react_agent: "React Agent 配置",
  llm_retry: "LLM 重试配置",
  query_retry: "Query 重试配置",
  llm_rate_limiter: "LLM 限流配置",
  context_compact: "上下文压缩配置",
  memory_summary: "记忆摘要配置",
};

function AgentConfigPage() {
  const { t } = useTranslation();
  const {
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
    syncContext,
    syncSubmission,
  } = useAgentConfig();

  const llmRetryEnabled = Form.useWatch("llm_retry_enabled", form) ?? true;
  const queryRetryEnabled =
    Form.useWatch(["query_retry", "enabled"], form) ?? false;
  const maxInputLength = Form.useWatch("max_input_length", form) ?? 0;

  if (loading) {
    return (
      <div className={styles.configPage}>
        <div className={styles.centerState}>
          <span className={styles.stateText}>{t("common.loading")}</span>
        </div>
      </div>
    );
  }

  if (error) {
    return (
      <div className={styles.configPage}>
        <div className={styles.centerState}>
          <span className={styles.stateTextError}>{error}</span>
          <Button size="small" onClick={fetchConfig} style={{ marginTop: 12 }}>
            {t("environments.retry")}
          </Button>
        </div>
      </div>
    );
  }

  return (
    <div className={styles.configPage}>
      <PageHeader
        parent={t("nav.runCenter")}
        current={t("agentConfig.title")}
      />
      <div className={styles.pageContent}>
        <div className={styles.formContainer}>
          <Form form={form} layout="vertical" className={styles.form}>
            <ReactAgentCard
              language={language}
              savingLang={savingLang}
              onLanguageChange={handleLanguageChange}
              timezone={timezone}
              savingTimezone={savingTimezone}
              onTimezoneChange={handleTimezoneChange}
              extra={
                canDistribute ? (
                  <Tooltip title={t("agentConfig.distributeTooltip")}>
                    <Button
                      type="text"
                      size="small"
                      icon={<SendOutlined />}
                      onClick={() =>
                        openDistributeModal(
                          "react_agent",
                          CONFIG_GROUP_LABELS.react_agent,
                        )
                      }
                    />
                  </Tooltip>
                ) : null
              }
            />

            <LlmRetryCard
              llmRetryEnabled={llmRetryEnabled}
              extra={
                canDistribute ? (
                  <Tooltip title={t("agentConfig.distributeTooltip")}>
                    <Button
                      type="text"
                      size="small"
                      icon={<SendOutlined />}
                      onClick={() =>
                        openDistributeModal(
                          "llm_retry",
                          CONFIG_GROUP_LABELS.llm_retry,
                        )
                      }
                    />
                  </Tooltip>
                ) : null
              }
            />

            <QueryRetryCard
              queryRetryEnabled={queryRetryEnabled}
              extra={
                canDistribute ? (
                  <Tooltip title={t("agentConfig.distributeTooltip")}>
                    <Button
                      type="text"
                      size="small"
                      icon={<SendOutlined />}
                      onClick={() =>
                        openDistributeModal(
                          "query_retry",
                          CONFIG_GROUP_LABELS.query_retry,
                        )
                      }
                    />
                  </Tooltip>
                ) : null
              }
            />

            <LlmRateLimiterCard
              extra={
                canDistribute ? (
                  <Tooltip title={t("agentConfig.distributeTooltip")}>
                    <Button
                      type="text"
                      size="small"
                      icon={<SendOutlined />}
                      onClick={() =>
                        openDistributeModal(
                          "llm_rate_limiter",
                          CONFIG_GROUP_LABELS.llm_rate_limiter,
                        )
                      }
                    />
                  </Tooltip>
                ) : null
              }
            />

            <ContextCompactCard
              maxInputLength={maxInputLength}
              extra={
                canDistribute ? (
                  <Tooltip title={t("agentConfig.distributeTooltip")}>
                    <Button
                      type="text"
                      size="small"
                      icon={<SendOutlined />}
                      onClick={() =>
                        openDistributeModal(
                          "context_compact",
                          CONFIG_GROUP_LABELS.context_compact,
                        )
                      }
                    />
                  </Tooltip>
                ) : null
              }
            />

            <MemorySummaryCard
              syncContext={syncContext}
              syncSubmission={syncSubmission}
              extra={
                canDistribute ? (
                  <Tooltip title={t("agentConfig.distributeTooltip")}>
                    <Button
                      type="text"
                      size="small"
                      icon={<SendOutlined />}
                      onClick={() =>
                        openDistributeModal(
                          "memory_summary",
                          CONFIG_GROUP_LABELS.memory_summary,
                        )
                      }
                    />
                  </Tooltip>
                ) : null
              }
            />
          </Form>
        </div>
      </div>

      <div className={styles.footerActions}>
        <Button
          onClick={fetchConfig}
          disabled={saving}
          style={{ marginRight: 8 }}
        >
          {t("common.reset")}
        </Button>
        <Button type="primary" onClick={handleSave} loading={saving}>
          {t("common.save")}
        </Button>
      </div>

      <DistributeModal
        open={distributeModalOpen}
        configGroup={currentConfigGroup}
        configGroupLabel={currentConfigGroupLabel}
        onClose={closeDistributeModal}
        onSuccess={fetchConfig}
      />
    </div>
  );
}

export default AgentConfigPage;
