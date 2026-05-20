import { useEffect, useMemo, useState } from "react";
import {
  Alert,
  Button,
  Card,
  Input,
  InputNumber,
  Space,
  Spin,
  Switch,
  Typography,
} from "antd";
import { useTranslation } from "react-i18next";
import { PageHeader } from "@/components/PageHeader";
import { sourceSystemConfigApi } from "@/api/modules/sourceSystemConfig";
import { DEFAULT_SOURCE_ID } from "@/constants/identity";
import { useAppMessage } from "@/hooks/useAppMessage";
import { useIframeStore } from "@/stores/iframeStore";
import { useSourceSystemConfigStore } from "@/stores/sourceSystemConfigStore";
import {
  DEFAULT_FOLLOW_UP_TIMEOUT_SECONDS,
  readFollowUpSuggestionsConfig,
  writeFollowUpSuggestionsConfig,
} from "./followUpConfig";
import styles from "./index.module.less";

const { Paragraph, Text } = Typography;

export default function SystemConfigPage() {
  const { t } = useTranslation();
  const { message } = useAppMessage();
  const activeSourceId =
    useIframeStore((state) => state.source) || DEFAULT_SOURCE_ID;
  const effectiveConfig = useSourceSystemConfigStore((state) => state.config);
  const loadEffectiveConfig = useSourceSystemConfigStore(
    (state) => state.loadEffectiveConfig,
  );
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [enabled, setEnabled] = useState(true);
  const [promptTemplate, setPromptTemplate] = useState("");
  const [timeoutSeconds, setTimeoutSeconds] = useState(
    DEFAULT_FOLLOW_UP_TIMEOUT_SECONDS,
  );

  const rawConfig = effectiveConfig?.config ?? {};
  const renderedConfig = useMemo(
    () => JSON.stringify(rawConfig, null, 2),
    [rawConfig],
  );

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    loadEffectiveConfig(activeSourceId).finally(() => {
      if (!cancelled) {
        setLoading(false);
      }
    });
    return () => {
      cancelled = true;
    };
  }, [activeSourceId, loadEffectiveConfig]);

  useEffect(() => {
    const followUp = readFollowUpSuggestionsConfig(rawConfig);
    setEnabled(followUp.enabled);
    setPromptTemplate(followUp.prompt_template);
    setTimeoutSeconds(followUp.timeout_seconds);
  }, [rawConfig]);

  const handleReset = () => {
    const followUp = readFollowUpSuggestionsConfig(rawConfig);
    setEnabled(followUp.enabled);
    setPromptTemplate(followUp.prompt_template);
    setTimeoutSeconds(followUp.timeout_seconds);
  };

  const handleSave = async () => {
    setSaving(true);
    try {
      const nextConfig = writeFollowUpSuggestionsConfig(rawConfig, {
        enabled,
        prompt_template: promptTemplate,
        timeout_seconds: timeoutSeconds,
      });
      await sourceSystemConfigApi.upsertSource(activeSourceId, {
        config: nextConfig,
      });
      await loadEffectiveConfig(activeSourceId);
      message.success(t("systemConfig.saveSuccess"));
    } catch (error) {
      console.error("Failed to save source system config:", error);
      message.error(
        error instanceof Error
          ? error.message
          : t("systemConfig.saveFailed"),
      );
    } finally {
      setSaving(false);
    }
  };

  if (loading) {
    return (
      <div className={styles.systemConfigPage}>
        <div className={styles.centerState}>
          <Spin />
        </div>
      </div>
    );
  }

  return (
    <div className={styles.systemConfigPage}>
      <PageHeader
        items={[
          { title: t("nav.settings") },
          { title: t("nav.systemConfig") },
        ]}
      />

      {effectiveConfig?.stale && (
        <Alert
          className={styles.alert}
          type="warning"
          showIcon
          message={t("systemConfig.staleTitle")}
          description={effectiveConfig.last_error || undefined}
        />
      )}

      <div className={styles.content}>
        <Card className={styles.panel}>
          <div className={styles.panelHeader}>
            <div>
              <h3>{t("systemConfig.followUpTitle")}</h3>
              <Paragraph type="secondary">
                {t("systemConfig.followUpDescription")}
              </Paragraph>
            </div>
            <Switch
              checked={enabled}
              checkedChildren={t("common.enabled")}
              unCheckedChildren={t("common.disabled")}
              onChange={setEnabled}
            />
          </div>

          <div className={styles.fieldBlock}>
            <Text strong>{t("systemConfig.timeoutSeconds")}</Text>
            <InputNumber
              min={1}
              max={15}
              step={0.5}
              value={timeoutSeconds}
              onChange={(value) =>
                setTimeoutSeconds(
                  typeof value === "number"
                    ? value
                    : DEFAULT_FOLLOW_UP_TIMEOUT_SECONDS,
                )
              }
              style={{ width: "100%" }}
            />
            <Paragraph type="secondary" className={styles.hint}>
              {t("systemConfig.timeoutHint")}
            </Paragraph>
          </div>

          <div className={styles.fieldBlock}>
            <Text strong>{t("systemConfig.promptTemplate")}</Text>
            <Input.TextArea
              value={promptTemplate}
              onChange={(event) => setPromptTemplate(event.target.value)}
              rows={10}
              spellCheck={false}
              className={styles.promptEditor}
            />
            <Paragraph type="secondary" className={styles.hint}>
              {t("systemConfig.promptHint")}
            </Paragraph>
          </div>

          <Space className={styles.actions}>
            <Button type="primary" loading={saving} onClick={handleSave}>
              {t("common.save")}
            </Button>
            <Button disabled={saving} onClick={handleReset}>
              {t("common.reset")}
            </Button>
          </Space>
        </Card>

        <Card className={styles.panel}>
          <div className={styles.metaGrid}>
            <div>
              <Text type="secondary">{t("systemConfig.sourceId")}</Text>
              <div className={styles.metaValue}>{activeSourceId}</div>
            </div>
            <div>
              <Text type="secondary">{t("systemConfig.version")}</Text>
              <div className={styles.metaValue}>
                {effectiveConfig?.version ?? 0}
              </div>
            </div>
            <div>
              <Text type="secondary">{t("systemConfig.defaultConfig")}</Text>
              <div className={styles.metaValue}>
                {effectiveConfig?.is_default
                  ? t("common.enabled")
                  : t("common.disabled")}
              </div>
            </div>
          </div>

          <Text strong>{t("systemConfig.currentConfig")}</Text>
          <pre className={styles.jsonBlock}>{renderedConfig}</pre>
        </Card>
      </div>
    </div>
  );
}
