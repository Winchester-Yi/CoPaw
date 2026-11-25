import { useId, useRef, useState } from "react";
import { Input, Modal, type InputRef } from "antd";

interface RenameHistorySessionModalProps {
  name: string;
  onSave: (name: string) => Promise<void>;
  onClose: () => void;
}

export default function RenameHistorySessionModal({
  name,
  onSave,
  onClose,
}: RenameHistorySessionModalProps) {
  const [open, setOpen] = useState(true);
  const [value, setValue] = useState(name);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");
  const inputRef = useRef<InputRef>(null);
  const savingRef = useRef(false);
  const errorId = useId();

  const submit = async () => {
    if (savingRef.current) return;
    const nextName = value.trim();
    if (nextName === name.trim() && nextName) {
      setOpen(false);
      return;
    }
    if (!nextName || Array.from(nextName).length > 100) {
      setError("请输入 1–100 个字符");
      return;
    }

    savingRef.current = true;
    setSaving(true);
    setError("");
    try {
      await onSave(nextName);
      setOpen(false);
    } catch (cause) {
      const status = (cause as { status?: number } | null)?.status;
      const message =
        status === 409 ? "会话已更新，请重试" : "标题保存失败，请重试";
      setError(message);
    } finally {
      savingRef.current = false;
      setSaving(false);
    }
  };

  return (
    <Modal
      open={open}
      title="修改名称"
      centered
      width={460}
      okText="确定"
      cancelText="取消"
      confirmLoading={saving}
      cancelButtonProps={{ disabled: saving }}
      closable={!saving}
      keyboard={!saving}
      maskClosable={false}
      focusTriggerAfterClose={false}
      onOk={() => void submit()}
      onCancel={() => setOpen(false)}
      afterOpenChange={(visible) => {
        if (visible) inputRef.current?.focus();
      }}
      afterClose={onClose}
    >
      <Input
        ref={inputRef}
        autoFocus
        size="large"
        aria-label="会话标题"
        aria-invalid={Boolean(error)}
        aria-describedby={error ? errorId : undefined}
        status={error ? "error" : undefined}
        readOnly={saving}
        value={value}
        onChange={(event) => {
          setValue(event.target.value);
          setError("");
        }}
        onPressEnter={(event) => {
          if (!event.nativeEvent.isComposing && event.keyCode !== 229) {
            void submit();
          }
        }}
      />
      {error && (
        <div id={errorId} role="alert" className="chat-history-rename-error">
          {error}
        </div>
      )}
    </Modal>
  );
}
