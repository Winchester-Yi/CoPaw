import { useEffect, useCallback, useRef, useMemo, useState } from "react";
import { Button, Dropdown, Spin, Switch, Tooltip } from "antd";
import { useAppMessage } from "../../../hooks/useAppMessage";
import {
  CheckOutlined,
  LoadingOutlined,
} from "@ant-design/icons";
import { SparkDownLine } from "@agentscope-ai/icons";
import { useLocation } from "react-router-dom";
import { useTranslation } from "react-i18next";
import { providerApi } from "../../../api/modules/provider";
import { useProviderModelStore } from "../../../stores/providerModelStore";
import { providerIcon } from "../../Settings/Models/components/providerIcon";
import type {
  ModelRuntimeConfig,
  ProviderInfo,
  ReasoningEffort,
} from "../../../api/types";
import styles from "./index.module.less";

interface EligibleModel {
  providerId: string;
  providerName: string;
  model: ProviderInfo["models"][number];
}

const REASONING_EFFORT_LABELS: Record<ReasoningEffort, string> = {
  low: "低",
  high: "高",
  max: "极高",
};

const REASONING_EFFORT_HINTS: Record<ReasoningEffort, string> = {
  low: "更快响应",
  high: "平衡推理效果与速度",
  max: "优先推理质量",
};

const REASONING_EFFORT_LABEL_KEYS: Record<ReasoningEffort, string> = {
  low: "modelSelector.reasoningLow",
  high: "modelSelector.reasoningHigh",
  max: "modelSelector.reasoningMax",
};

const REASONING_EFFORT_HINT_KEYS: Record<ReasoningEffort, string> = {
  low: "modelSelector.reasoningLowHint",
  high: "modelSelector.reasoningHighHint",
  max: "modelSelector.reasoningMaxHint",
};

export default function ModelSelector() {
  const { t } = useTranslation();
  const providers = useProviderModelStore((state) => state.providers);
  const activeModels = useProviderModelStore((state) => state.activeModels);
  const loading = useProviderModelStore((state) => state.loading);
  const loadModelData = useProviderModelStore((state) => state.loadModelData);
  const setModelRuntimeConfig = useProviderModelStore(
    (state) => state.setModelRuntimeConfig,
  );
  const [saving, setSaving] = useState(false);
  const [open, setOpen] = useState(false);
  const [runtimeConfig, setRuntimeConfig] = useState<ModelRuntimeConfig | null>(
    null,
  );
  const savingRef = useRef(false);
  const location = useLocation();
  const { message } = useAppMessage();

  const fetchData = useCallback(async () => {
    try {
      // Use tenant-level scope (agent scope deprecated)
      await loadModelData({ scope: "effective" });
    } catch (err) {
      console.error("ModelSelector: failed to load data", err);
    }
  }, [loadModelData]);

  useEffect(() => {
    fetchData();
  }, [fetchData]);

  // Re-sync active model whenever the route switches back to /chat
  const prevPathRef = useRef(location.pathname);
  useEffect(() => {
    const prev = prevPathRef.current;
    const curr = location.pathname;
    prevPathRef.current = curr;
    const comingToChat = curr.startsWith("/chat") && !prev.startsWith("/chat");
    if (comingToChat) {
      // Use tenant-level scope (agent scope deprecated)
      loadModelData({ scope: "effective" }).catch(() => {});
    }
  }, [loadModelData, location.pathname]);

  const eligibleModels: EligibleModel[] = useMemo(
    () =>
      providers
        .filter((p) => {
          const hasModels =
            (p.models?.length ?? 0) + (p.extra_models?.length ?? 0) > 0;
          if (!hasModels) return false;
          if (p.require_api_key === false) return !!p.base_url;
          if (p.is_custom) return !!p.base_url;
          if (p.require_api_key ?? true) return !!p.api_key;
          return true;
        })
        .flatMap((provider) =>
          [...(provider.models ?? []), ...(provider.extra_models ?? [])].map(
            (model) => ({
              providerId: provider.id,
              providerName: provider.name,
              model,
            }),
          ),
        ),
    [providers],
  );

  const activeProviderId = activeModels?.active_llm?.provider_id;
  const activeModelId = activeModels?.active_llm?.model;

  useEffect(() => {
    const provider = providers.find((item) => item.id === activeProviderId);
    setRuntimeConfig(provider?.model_configs?.[activeModelId || ""] ?? null);
  }, [providers, activeProviderId, activeModelId]);

  const updateRuntimeConfig = useCallback(
    async (updates: Partial<ModelRuntimeConfig>) => {
      if (
        savingRef.current ||
        !activeProviderId ||
        !activeModelId ||
        !runtimeConfig
      ) {
        return;
      }
      const previous = runtimeConfig;
      savingRef.current = true;
      setSaving(true);
      setRuntimeConfig({ ...runtimeConfig, ...updates });
      try {
        const saved = await providerApi.updateModelRuntimeConfig(
          activeProviderId,
          activeModelId,
          updates,
        );
        setModelRuntimeConfig(activeProviderId, activeModelId, saved);
        setRuntimeConfig(saved);
      } catch (err) {
        setRuntimeConfig(previous);
        message.error(
          err instanceof Error ? err.message : t("models.failedToSaveConfig"),
        );
      } finally {
        setSaving(false);
        savingRef.current = false;
      }
    },
    [
      activeProviderId,
      activeModelId,
      message,
      runtimeConfig,
      setModelRuntimeConfig,
      t,
    ],
  );

  // Display label for trigger button
  const activeModelName = (() => {
    if (!activeProviderId || !activeModelId)
      return t("modelSelector.selectModel");
    const activeModel = eligibleModels.find(
      (item) =>
        item.providerId === activeProviderId && item.model.id === activeModelId,
    );
    if (activeModel) return activeModel.model.name || activeModel.model.id;
    return activeModelId;
  })();

  const handleOpenChange = useCallback(
    async (next: boolean) => {
      setOpen(next);
      if (next) {
        // Re-fetch active model every time the dropdown opens
        // Use tenant-level scope (agent scope deprecated)
        try {
          await loadModelData({ scope: "effective" });
        } catch {
          // ignore
        }
      }
    },
    [loadModelData],
  );

  const handleSelect = async (providerId: string, modelId: string) => {
    if (savingRef.current) return;
    if (providerId === activeProviderId && modelId === activeModelId) {
      setOpen(false);
      return;
    }
    savingRef.current = true;
    setSaving(true);
    setOpen(false);
    try {
      // Use 'global' scope - tenant-level active model (agent scope deprecated)
      await providerApi.setActiveLlm({
        provider_id: providerId,
        model: modelId,
        scope: "global",
      });
      try {
        await loadModelData({ scope: "effective" });
      } catch (err) {
        // Activation already succeeded; a stale refresh must not report a failed switch.
        console.error("ModelSelector: failed to refresh model data", err);
      }
      // Notify ChatPage to refresh multimodal capabilities
      window.dispatchEvent(new CustomEvent("model-switched"));
    } catch (err) {
      const msg =
        err instanceof Error ? err.message : t("modelSelector.switchFailed");
      message.error(msg);
    } finally {
      setSaving(false);
      savingRef.current = false;
    }
  };

  const hasThinkingConfiguration =
    runtimeConfig !== null &&
    (runtimeConfig.supports_enable_thinking ||
      runtimeConfig.supported_reasoning_efforts.length > 0);
  const reasoningEffortDisabled =
    saving ||
    !!(
      runtimeConfig?.supports_enable_thinking && !runtimeConfig.enable_thinking
    );
  const reasoningHint = runtimeConfig?.reasoning_effort
    ? t(
        REASONING_EFFORT_HINT_KEYS[runtimeConfig.reasoning_effort],
        REASONING_EFFORT_HINTS[runtimeConfig.reasoning_effort],
      )
    : t("modelSelector.selectThinkingEffort", "请选择思考强度");

  const dropdownContent = (
    <div
      className={`${styles.panel} ${
        hasThinkingConfiguration ? styles.panelWithConfig : ""
      }`}
    >
      {loading ? (
        <div className={styles.spinWrapper}>
          <Spin size="small" />
        </div>
      ) : eligibleModels.length === 0 ? (
        <div className={styles.emptyTip}>
          {t("modelSelector.noConfiguredModels")}
        </div>
      ) : (
        <div className={styles.modelList}>
          {eligibleModels.map(({ providerId, providerName, model }) => {
            const isActive =
              providerId === activeProviderId && model.id === activeModelId;
            return (
              <button
                key={`${providerId}:${model.id}`}
                type="button"
                className={`${styles.modelCard} ${
                  isActive ? styles.modelCardActive : ""
                }`}
                aria-pressed={isActive}
                disabled={saving}
                onClick={() => handleSelect(providerId, model.id)}
              >
                <img
                  className={styles.modelIcon}
                  src={providerIcon(providerId)}
                  alt=""
                />
                <span className={styles.modelIdentity}>
                  <span className={styles.modelId}>{model.id}</span>
                  <span className={styles.modelMetadata}>
                    {providerName} · {model.name}
                  </span>
                </span>
                {isActive && <CheckOutlined className={styles.checkIcon} />}
              </button>
            );
          })}
        </div>
      )}
      {hasThinkingConfiguration && runtimeConfig && (
        <div
          className={styles.runtimeConfig}
          onClick={(event) => event.stopPropagation()}
        >
          <h3 className={styles.runtimeConfigTitle}>
            {t("modelSelector.settings", "模型配置")}
          </h3>
          {runtimeConfig.supports_enable_thinking && (
            <div className={styles.runtimeConfigRow}>
              <span>{t("modelSelector.thinkingMode", "思考模式")}</span>
              <Switch
                size="small"
                checked={runtimeConfig.enable_thinking}
                disabled={saving}
                onChange={(checked) =>
                  updateRuntimeConfig({ enable_thinking: checked })
                }
              />
            </div>
          )}
          {runtimeConfig.supported_reasoning_efforts.length > 0 && (
            <div className={styles.reasoningSection}>
              <span className={styles.reasoningLabel}>
                {t("modelSelector.thinkingEffort", "思考强度")}
              </span>
              <div className={styles.reasoningOptions}>
                {runtimeConfig.supported_reasoning_efforts.map((effort) => (
                  <Button
                    key={effort}
                    type={
                      runtimeConfig.reasoning_effort === effort
                        ? "primary"
                        : "default"
                    }
                    aria-pressed={runtimeConfig.reasoning_effort === effort}
                    disabled={reasoningEffortDisabled}
                    className={styles.reasoningOption}
                    onClick={() =>
                      updateRuntimeConfig({ reasoning_effort: effort })
                    }
                  >
                    {t(
                      REASONING_EFFORT_LABEL_KEYS[effort],
                      REASONING_EFFORT_LABELS[effort],
                    )}
                  </Button>
                ))}
              </div>
              <span className={styles.reasoningHint}>{reasoningHint}</span>
            </div>
          )}
        </div>
      )}
    </div>
  );

  return (
    <Dropdown
      open={open}
      onOpenChange={handleOpenChange}
      dropdownRender={() => dropdownContent}
      trigger={["click"]}
      placement="bottomLeft"
    >
      <Tooltip title={t("chat.modelSelectTooltip")} mouseEnterDelay={0.5}>
        <div
          className={[styles.trigger, open ? styles.triggerActive : ""].join(
            " ",
          )}
        >
          {saving && (
            <LoadingOutlined style={{ fontSize: 11, color: "#3769FC" }} />
          )}
          <span className={styles.triggerName}>{activeModelName}</span>
          <SparkDownLine
            className={[
              styles.triggerArrow,
              open ? styles.triggerArrowOpen : "",
            ].join(" ")}
          />
        </div>
      </Tooltip>
    </Dropdown>
  );
}
