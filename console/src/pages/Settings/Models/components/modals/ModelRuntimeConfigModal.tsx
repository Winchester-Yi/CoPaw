import { useEffect, useState } from "react";
import {
  Checkbox,
  Form,
  InputNumber,
  Modal,
  Switch,
} from "@agentscope-ai/design";
import type {
  ModelRuntimeConfig,
  ProviderInfo,
} from "../../../../../api/types";
import api from "../../../../../api";
import { useTranslation } from "react-i18next";
import { useAppMessage } from "../../../../../hooks/useAppMessage";
import styles from "../../index.module.less";

interface ModelRuntimeConfigModalProps {
  provider: ProviderInfo;
  modelId: string;
  open: boolean;
  onClose: () => void;
  onSaved?: () => void | Promise<void>;
}

export function ModelRuntimeConfigModal({
  provider,
  modelId,
  open,
  onClose,
  onSaved,
}: ModelRuntimeConfigModalProps) {
  const { t } = useTranslation();
  const { message } = useAppMessage();
  const [saving, setSaving] = useState(false);
  const [loading, setLoading] = useState(false);
  const [form] = Form.useForm<ModelRuntimeConfig>();

  useEffect(() => {
    if (!open) return;
    setLoading(true);
    api
      .getModelRuntimeConfig(provider.id, modelId)
      .then((config) =>
        form.setFieldsValue({
          ...config,
          supported_reasoning_efforts: config.supported_reasoning_efforts ?? [],
        }),
      )
      .catch((error) => {
        message.error(
          error instanceof Error
            ? error.message
            : t("models.failedToSaveConfig"),
        );
        onClose();
      })
      .finally(() => setLoading(false));
  }, [form, message, modelId, onClose, open, provider.id, t]);

  const handleSave = async () => {
    try {
      const values = await form.validateFields();
      setSaving(true);
      await api.updateModelRuntimeConfig(provider.id, modelId, values);
      message.success(t("models.configurationSaved", { name: modelId }));
      onClose();
      try {
        await onSaved?.();
      } catch (error) {
        console.error(
          "ModelRuntimeConfigModal: failed to refresh model data",
          error,
        );
      }
    } catch (error) {
      if (error && typeof error === "object" && "errorFields" in error) return;
      message.error(
        error instanceof Error ? error.message : t("models.failedToSaveConfig"),
      );
    } finally {
      setSaving(false);
    }
  };

  return (
    <Modal
      rootClassName={`console-management-modal ${styles.runtimeConfigModal}`}
      title={
        <div className={styles.runtimeConfigTitle}>
          <span>{t("models.modelRuntimeConfig", "模型运行配置")}</span>
          <span className={styles.runtimeConfigModelId}>{modelId}</span>
        </div>
      }
      open={open}
      onCancel={onClose}
      onOk={handleSave}
      confirmLoading={saving || loading}
      okText={t("common.confirm", "确定")}
      cancelText={t("models.cancel")}
      width={720}
      destroyOnHidden
    >
      <Form form={form} layout="vertical" className={styles.runtimeConfigForm}>
        <div className={styles.runtimeConfigGrid}>
          <div className={styles.runtimeConfigColumn}>
            <Form.Item name="temperature" label="Temperature">
              <InputNumber min={0} step={0.1} />
            </Form.Item>
            <Form.Item name="top_p" label="Top P">
              <InputNumber min={0} max={1} step={0.01} />
            </Form.Item>
            <Form.Item name="top_k" label="Top K">
              <InputNumber min={0} precision={0} />
            </Form.Item>
          </div>
          <div className={styles.runtimeConfigColumn}>
            <Form.Item
              name="max_input_length"
              label={t("models.maxInputLength", "最大输入长度")}
            >
              <InputNumber min={1} precision={0} />
            </Form.Item>
            <Form.Item
              name="max_output_length"
              label={t("models.maxOutputLength", "最大输出长度")}
            >
              <InputNumber min={1} precision={0} />
            </Form.Item>
          </div>
        </div>
        <div className={styles.runtimeConfigAdvancedGrid}>
          <div className={styles.runtimeConfigThinkingRow}>
            <div>
              <div className={styles.runtimeConfigSectionLabel}>
                {t("models.supportsThinkingSwitch", "支持思考模式开关")}
              </div>
              <div className={styles.runtimeConfigHint}>
                开启后，聊天界面会显示思考模式选择按钮
              </div>
            </div>
            <Form.Item
              name="supports_enable_thinking"
              valuePropName="checked"
              noStyle
            >
              <Switch />
            </Form.Item>
          </div>
          <Form.Item
            name="supported_reasoning_efforts"
            label={t("models.reasoningEfforts", "支持的思考强度")}
          >
            <Checkbox.Group>
              {(["low", "high", "max"] as const).map((effort) => (
                <Checkbox key={effort} value={effort}>
                  {effort}
                </Checkbox>
              ))}
            </Checkbox.Group>
          </Form.Item>
        </div>
      </Form>
    </Modal>
  );
}
