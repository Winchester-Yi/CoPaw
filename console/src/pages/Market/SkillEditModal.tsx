import { useEffect, useState } from "react";
import { Alert, Form, Input, Modal, Select, message } from "antd";
import { marketApi, type Category, type MarketSkill } from "../../api/modules/market";
import { BBK_ID_MAP } from "../../constants/bbk";

interface SkillEditModalProps {
  open: boolean;
  sourceId: string;
  skill: MarketSkill | null;
  categories: Category[];
  onClose: () => void;
  onSuccess: () => Promise<void> | void;
}

interface SkillEditValues {
  chinese_name: string;
  category_id: number;
  bbk_ids: string[];
}

function getErrorMessage(error: unknown): string {
  if (!(error instanceof Error)) return "保存失败";
  try {
    const payload = JSON.parse(error.message);
    return typeof payload?.detail === "string" ? payload.detail : error.message;
  } catch {
    return error.message || "保存失败";
  }
}

export function SkillEditModal({
  open,
  sourceId,
  skill,
  categories,
  onClose,
  onSuccess,
}: SkillEditModalProps) {
  const [form] = Form.useForm<SkillEditValues>();
  const [saving, setSaving] = useState(false);

  useEffect(() => {
    if (!open || !skill) return;
    form.setFieldsValue({
      chinese_name: skill.chinese_name || skill.name,
      category_id: skill.category_id ?? undefined,
      bbk_ids: skill.bbk_ids?.length ? skill.bbk_ids : [BBK_ID_MAP[0]?.value],
    });
  }, [form, open, skill]);

  const save = async (values: SkillEditValues) => {
    if (!skill) return;
    const nameChanged = values.chinese_name.trim() !== (skill.chinese_name || "");
    let syncToUsers = false;
    if (nameChanged) {
      syncToUsers = await new Promise<boolean>((resolve) => {
        Modal.confirm({
          title: "同步技能名称？",
          content: "是否将新的中文名称同步到已分发用户？分类变更会自动同步，分行归属变更不会撤回已分发技能。",
          okText: "同步名称",
          cancelText: "仅更新市场",
          onOk: () => resolve(true),
          onCancel: () => resolve(false),
        });
      });
    }

    setSaving(true);
    try {
      await marketApi.updateSkillCnName(sourceId, skill.item_id, {
        skill_id: skill.skill_id || "",
        chinese_name: values.chinese_name.trim(),
        category_id: values.category_id,
        bbk_ids: values.bbk_ids,
        sync_to_users: syncToUsers,
      });
      message.success("技能信息已更新");
      onClose();
      await onSuccess();
    } catch (error) {
      message.error(getErrorMessage(error));
    } finally {
      setSaving(false);
    }
  };

  return (
    <Modal
      title="编辑技能"
      open={open}
      onCancel={onClose}
      onOk={() => form.submit()}
      okText="保存"
      cancelText="取消"
      confirmLoading={saving}
      width={560}
      destroyOnHidden
    >
      <Alert
        type="info"
        showIcon
        message="调整归属分行只影响后续市场展示，不会撤回已分发技能，也不会修改用户侧分行信息。"
        style={{ marginBottom: 20 }}
      />
      <Form form={form} layout="vertical" onFinish={(values) => void save(values)}>
        <Form.Item
          name="chinese_name"
          label="中文名称"
          rules={[
            { required: true, message: "请输入中文名称" },
            { max: 50, message: "中文名称不能超过 50 个字符" },
          ]}
        >
          <Input showCount maxLength={50} placeholder="请输入技能中文展示名" />
        </Form.Item>
        <Form.Item
          name="category_id"
          label="分类"
          rules={[{ required: true, message: "请选择分类" }]}
        >
          <Select
            showSearch
            optionFilterProp="label"
            placeholder="请选择技能分类"
            options={categories.map((category) => ({
              label: category.name,
              value: category.id,
            }))}
          />
        </Form.Item>
        <Form.Item
          name="bbk_ids"
          label="归属分行"
          rules={[
            {
              validator: async (_, value: string[]) => {
                if (!value?.length) {
                  throw new Error("至少选择一个归属分行");
                }
              },
            },
          ]}
        >
          <Select
            mode="multiple"
            allowClear
            showSearch
            optionFilterProp="label"
            placeholder="请选择至少一个分行"
            options={BBK_ID_MAP.map((item) => ({
              label: item.label,
              value: item.value,
            }))}
          />
        </Form.Item>
      </Form>
    </Modal>
  );
}
