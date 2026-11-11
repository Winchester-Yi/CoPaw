import { useLayoutEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { Form } from "antd";
import { X } from "lucide-react";
import {
  joinInsuranceTracking,
  customerAssets,
  INTENTION_LABELS,
  type JoinTrackingValues,
} from "./api";
import { ContactPlansEditor } from "./ContactPlansEditor";
import { LimitedTextarea } from "./LimitedTextarea";
import { TEXT_LIMIT_RULE } from "./textLimit";
import type { Customer } from "../../types";
import workbenchStyles from "../../index.module.less";
import { TrackingSelect } from "./TrackingSelect";
import styles from "./JoinTracking.module.less";

function JoinConfirmation({
  customer,
  onAdded,
  onClose,
}: {
  customer: Customer;
  onAdded: (custUid: string, alreadyTracked?: boolean) => void;
  onClose: () => void;
}) {
  const [form] = Form.useForm<JoinTrackingValues>();
  const dialogRef = useRef<HTMLDialogElement>(null);
  const requestRef = useRef<AbortController | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  useLayoutEffect(() => {
    const dialog = dialogRef.current;
    dialog?.showModal();
    return () => {
      requestRef.current?.abort();
      if (dialog?.open) dialog.close();
    };
  }, []);

  const confirm = async (values: JoinTrackingValues) => {
    if (requestRef.current) return;
    const controller = new AbortController();
    requestRef.current = controller;
    setBusy(true);
    setError("");
    try {
      const payload = {
        insurancePlan: values.insurancePlan.trim(),
        contactPlans: (values.contactPlans ?? []).map((plan) => ({
          ...plan,
          content: plan.content.trim(),
        })),
        intention: values.intention,
      };
      const result = await joinInsuranceTracking(
        customer,
        payload,
        controller.signal,
      );
      if (controller.signal.aborted) return;
      if (result.created) onAdded(customer.custUid);
      else onAdded(customer.custUid, true);
      onClose();
    } catch (cause) {
      if (!controller.signal.aborted) {
        setError(
          cause instanceof Error ? cause.message : "加入跟踪失败，请重试",
        );
      }
    } finally {
      requestRef.current = null;
      if (!controller.signal.aborted) setBusy(false);
    }
  };

  const assets = customerAssets(customer) ?? "—";
  return (
    <dialog
      ref={dialogRef}
      className={`${workbenchStyles.dialog} ${styles.dialog}`}
      aria-label="加入保险核心跟踪"
      onCancel={(event) => {
        event.preventDefault();
        if (!requestRef.current) onClose();
      }}
    >
      <button
        type="button"
        className={`${workbenchStyles.iconbtn} ${workbenchStyles.dialogClose}`}
        aria-label="关闭加入跟踪"
        disabled={busy}
        onClick={onClose}
      >
        <X size={16} aria-hidden />
      </button>
      <Form
        form={form}
        layout="vertical"
        requiredMark={false}
        noValidate
        scrollToFirstError
        initialValues={{ insurancePlan: "", contactPlans: [], intention: 0 }}
        onFinish={(values) => {
          void confirm(values);
        }}
      >
        <div className={workbenchStyles.dialogHead}>
          <h2 className={styles.title}>加入保险跟踪 · {customer.name}</h2>
        </div>
        <div className={workbenchStyles.dialogBody}>
          <p>确认将该客户纳入保险核心跟踪，请填写跟踪计划。</p>
          <div
            className={`${workbenchStyles.detailGrid} ${styles.customerInfo}`}
          >
            <div className={styles.summaryItem}>
              <small>客户姓名</small>
              <strong title={customer.name}>{customer.name}</strong>
            </div>
            <div className={styles.summaryItem}>
              <small>资产</small>
              <strong title={String(assets)}>{assets}</strong>
            </div>
          </div>
          <fieldset disabled={busy} className={styles.fields}>
            <Form.Item
              name="insurancePlan"
              label={
                <span>
                  保险计划<span className={styles.required}> *</span>
                </span>
              }
              rules={[
                { required: true, whitespace: true, message: "请输入保险计划" },
                TEXT_LIMIT_RULE,
              ]}
            >
              <LimitedTextarea
                showLimitHint={false}
                rows={3}
                placeholder="填写客户保险需求与保障安排"
                aria-required="true"
              />
            </Form.Item>
            <ContactPlansEditor disabled={busy} />
            <Form.Item
              name="intention"
              label="客户意向度"
              rules={[{ required: true, message: "请选择客户意向度" }]}
            >
              <TrackingSelect
                disabled={busy}
                aria-required="true"
                placeholder="请选择"
                options={Object.entries(INTENTION_LABELS).map(
                  ([value, label]) => ({ value: Number(value), label }),
                )}
              />
            </Form.Item>
          </fieldset>
        </div>
        {error && (
          <div className={styles.error} role="alert">
            加入失败：{error}，可重试。
          </div>
        )}
        <div className={workbenchStyles.dialogFooter}>
          <button
            type="button"
            className={workbenchStyles.btn}
            disabled={busy}
            onClick={onClose}
          >
            取消
          </button>
          <button
            type="submit"
            className={`${workbenchStyles.btn} ${workbenchStyles.primary}`}
            disabled={busy}
          >
            {busy ? "正在加入…" : "确认添加"}
          </button>
        </div>
      </Form>
    </dialog>
  );
}

export function JoinTracking({
  customer,
  onAdded,
}: {
  customer: Customer;
  onAdded: (custUid: string, alreadyTracked?: boolean) => void;
}) {
  const [open, setOpen] = useState(false);
  const buttonRef = useRef<HTMLButtonElement>(null);
  // 弹窗放在工作台根节点，避免执行列的紧凑按钮样式影响表单和底栏。
  const portalHost =
    buttonRef.current?.closest(`.${workbenchStyles.root}`) ??
    buttonRef.current?.parentElement;
  return (
    <>
      <button
        ref={buttonRef}
        type="button"
        className={`${workbenchStyles.btn} ${styles.button}`}
        disabled={customer.traced !== 0}
        title={
          customer.traced == null ? "暂未返回跟踪状态，请刷新后重试" : undefined
        }
        onClick={() => setOpen(true)}
      >
        {customer.traced === 1 ? "已加入跟踪" : "加入跟踪"}
      </button>
      {open &&
        portalHost &&
        createPortal(
          <JoinConfirmation
            customer={customer}
            onAdded={onAdded}
            onClose={() => setOpen(false)}
          />,
          portalHost,
        )}
    </>
  );
}
