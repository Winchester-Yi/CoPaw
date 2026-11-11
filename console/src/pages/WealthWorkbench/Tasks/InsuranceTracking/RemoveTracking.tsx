import { useId, useLayoutEffect, useRef, useState } from "react";
import { removeInsuranceTracking, type InsuranceCustomer } from "./api";
import workbenchStyles from "../../index.module.less";
import editorStyles from "./Editor.module.less";

export function RemoveTracking({
  customer,
  onRemoved,
  onClose,
}: {
  customer: InsuranceCustomer;
  onRemoved: () => void;
  onClose: () => void;
}) {
  const dialogRef = useRef<HTMLDialogElement>(null);
  const requestRef = useRef<AbortController | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const titleId = useId();
  useLayoutEffect(() => {
    const dialog = dialogRef.current;
    dialog?.showModal();
    return () => {
      requestRef.current?.abort();
      if (dialog?.open) dialog.close();
    };
  }, []);

  const confirm = async () => {
    if (requestRef.current) return;
    const controller = new AbortController();
    requestRef.current = controller;
    setBusy(true);
    setError("");
    try {
      await removeInsuranceTracking(customer, controller.signal);
      if (!controller.signal.aborted) onRemoved();
    } catch (cause) {
      if (!controller.signal.aborted)
        setError(cause instanceof Error ? cause.message : "移除失败，请重试");
    } finally {
      requestRef.current = null;
      if (!controller.signal.aborted) setBusy(false);
    }
  };

  return (
    <dialog
      ref={dialogRef}
      className={`${workbenchStyles.dialog} ${editorStyles.editor}`}
      aria-labelledby={titleId}
      aria-busy={busy}
      onCancel={(event) => {
        event.preventDefault();
        if (!requestRef.current) onClose();
      }}
    >
      <div className={workbenchStyles.dialogHead}>
        <h2 id={titleId}>移除跟踪</h2>
      </div>
      <div className={workbenchStyles.dialogBody}>
        <p>确认将“{customer.name}”移出保险核心跟踪？</p>
        {error && <p role="alert">移除失败：{error}</p>}
      </div>
      <div className={workbenchStyles.dialogFooter}>
        <button
          type="button"
          className={workbenchStyles.btn}
          disabled={busy}
          autoFocus
          onClick={onClose}
        >
          取消
        </button>
        <button
          type="button"
          className={`${workbenchStyles.btn} ${workbenchStyles.danger}`}
          disabled={busy}
          onClick={() => void confirm()}
        >
          {busy ? "正在移除…" : "确认移除"}
        </button>
      </div>
    </dialog>
  );
}
