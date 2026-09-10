import { useEffect, useState } from "react";
import { Alert, Button, Select, Space } from "antd";
import { ArrowUp, ArrowDown, X } from "lucide-react";
import { BBK_ID_MAP } from "../../../../constants/bbk";
import {
  batchOperations,
  type BatchPriority,
} from "../../../../api/modules/batchOperations";

function readBatchPriority(value: unknown): BatchPriority {
  const raw =
    value && typeof value === "object" ? (value as Partial<BatchPriority>) : {};
  return {
    user_ids: Array.isArray(raw.user_ids)
      ? raw.user_ids.filter((x): x is string => typeof x === "string")
      : [],
    branch_ids: Array.isArray(raw.branch_ids)
      ? raw.branch_ids.filter((x): x is string => typeof x === "string")
      : [],
  };
}

export default function BatchPriorityEditor({
  jobId,
  initial,
  disabled = false,
  onSaved,
  onDirtyChange,
}: {
  jobId: string;
  initial: unknown;
  disabled?: boolean;
  onSaved?: () => void;
  onDirtyChange?: (dirty: boolean) => void;
}) {
  const [priority, setPriority] = useState(() => readBatchPriority(initial));
  const [savedPriority, setSavedPriority] = useState(() =>
    JSON.stringify(readBatchPriority(initial)),
  );
  const dirty = JSON.stringify(priority) !== savedPriority;
  const [saving, setSaving] = useState(false);
  useEffect(() => {
    onDirtyChange?.(dirty || saving);
    return () => onDirtyChange?.(false);
  }, [dirty, saving, onDirtyChange]);
  const [notice, setNotice] = useState<{
    type: "success" | "error";
    text: string;
  } | null>(null);
  const busy = disabled || saving;
  const change = (field: keyof BatchPriority, values: string[]) => {
    setNotice(null);
    setPriority((old) => ({ ...old, [field]: Array.from(new Set(values)) }));
  };
  const move = (field: keyof BatchPriority, index: number, offset: number) => {
    const values = [...priority[field]];
    [values[index], values[index + offset]] = [
      values[index + offset],
      values[index],
    ];
    change(field, values);
  };
  const save = async () => {
    setSaving(true);
    setNotice(null);
    try {
      await batchOperations.savePriority(jobId, priority);
      setSavedPriority(JSON.stringify(priority));
      setNotice({ type: "success", text: "优先策略已保存，从新批次生效" });
      onSaved?.();
    } catch (error) {
      setNotice({
        type: "error",
        text: error instanceof Error ? error.message : "保存失败",
      });
    } finally {
      setSaving(false);
    }
  };
  return (
    <Space direction="vertical" style={{ width: "100%" }}>
      <strong>批次内优先策略</strong>
      <span>用户优先于一级分行，同级沿用现有排序。名单不增加接收人。</span>
      {(["user_ids", "branch_ids"] as const).map((field) => {
        const label = field === "user_ids" ? "优先用户" : "优先一级分行";
        return (
          <Space key={field} direction="vertical" style={{ width: "100%" }}>
            <label>{label}（从上到下优先）</label>
            <Select
              aria-label={label}
              style={{ width: "100%" }}
              mode={field === "user_ids" ? "tags" : "multiple"}
              placeholder={
                field === "user_ids"
                  ? "输入用户 ID，按回车添加"
                  : "选择一级分行"
              }
              options={field === "branch_ids" ? BBK_ID_MAP : undefined}
              value={priority[field]}
              onChange={(values) => change(field, values)}
              disabled={busy}
            />
            {priority[field].map((id, index) => (
              <Space key={id} wrap>
                <span>
                  {index + 1}.{" "}
                  {field === "branch_ids"
                    ? BBK_ID_MAP.find((b) => b.value === id)?.label || id
                    : id}
                </span>
                <Button
                  size="small"
                  aria-label={`${label} ${id} 上移`}
                  icon={<ArrowUp size={14} />}
                  disabled={busy || index === 0}
                  onClick={() => move(field, index, -1)}
                />
                <Button
                  size="small"
                  aria-label={`${label} ${id} 下移`}
                  icon={<ArrowDown size={14} />}
                  disabled={busy || index === priority[field].length - 1}
                  onClick={() => move(field, index, 1)}
                />
                <Button
                  size="small"
                  aria-label={`${label} ${id} 移除`}
                  icon={<X size={14} />}
                  disabled={busy}
                  onClick={() =>
                    change(
                      field,
                      priority[field].filter((v) => v !== id),
                    )
                  }
                />
              </Space>
            ))}
          </Space>
        );
      })}
      <Button onClick={save} loading={saving} disabled={busy}>
        保存优先策略
      </Button>
      {dirty && <span>优先名单有未保存改动，请先保存优先策略。</span>}
      {notice && <Alert type={notice.type} message={notice.text} showIcon />}
    </Space>
  );
}
