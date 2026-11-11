import { useId, useLayoutEffect, useRef } from "react";
import { Form } from "antd";
import { v4 as uuid } from "uuid";
import { Icon } from "../../components/Icon";
import workbenchStyles from "../../index.module.less";
import { TrackingSelect } from "./TrackingSelect";
import styles from "./Editor.module.less";
import {
  EDIT_LABELS,
  FOLLOW_UP_RESULTS,
  FOLLOW_UP_METHODS,
  type FollowUpMethod,
  type EditKind,
  type FollowUpResult,
  type InsuranceEditorChange,
  type InsuranceCustomer,
  type ContactRecord,
  type FollowUpRecord,
} from "./api";
import { TrackingDatePicker } from "./TrackingDatePicker";
import { trackingNow } from "./trackingDate";
import { MAX_TEXT_LENGTH, TEXT_LIMIT_RULE } from "./textLimit";
import { FollowUpSceneField, type FollowUpScene } from "./FollowUpSceneField";

interface EditorValues {
  date: string;
  content: string;
  method: FollowUpMethod;
  insurancePlan: string;
  result: FollowUpResult;
  scene: FollowUpScene;
}

export function InsuranceEditor({
  customer,
  kind,
  draft,
  record,
  onSave,
  onClose,
}: {
  customer: InsuranceCustomer;
  kind: EditKind;
  draft?: InsuranceEditorChange;
  record?: ContactRecord | FollowUpRecord;
  onSave: (change: InsuranceEditorChange) => void;
  onClose: () => void;
}) {
  const [form] = Form.useForm<EditorValues>();
  const dialogRef = useRef<HTMLDialogElement>(null);
  const submittedRef = useRef(false);
  const titleId = useId();
  const isFollowUp = kind === "followUp";
  const methods = Object.entries(FOLLOW_UP_METHODS).map(([value, label]) => ({
    value: Number(value),
    label,
  }));
  const entry = draft && draft.kind !== "insurancePlan" ? draft.value : record;
  const recordId =
    record?.id ?? (draft && "recordId" in draft ? draft.recordId : undefined);
  const followUp = record as FollowUpRecord | undefined;
  const initialScene: FollowUpScene = {
    skillId:
      (draft?.kind === "followUp" ? draft.value.skillId : undefined) ??
      (recordId ? followUp?.skillId : customer.skillId) ??
      "",
    sceneName:
      (draft?.kind === "followUp" ? draft.value.sceneName : undefined) ??
      (recordId ? followUp?.sceneName : customer.sceneName) ??
      "",
  };
  const title = `${kind === "insurancePlan" || recordId ? "编辑" : "新增"}${
    EDIT_LABELS[kind]
  } · ${customer.name}`;
  const initialPlan =
    draft?.kind === "insurancePlan"
      ? draft.value
      : customer.insurancePlan ?? "";
  const textValue: string | undefined = Form.useWatch(
    kind === "insurancePlan" ? "insurancePlan" : "content",
    form,
  );
  const textLength = (
    textValue ?? (kind === "insurancePlan" ? initialPlan : entry?.content ?? "")
  ).length;
  const textHint = (
    <div
      className={`${styles.textHint} ${
        textLength > MAX_TEXT_LENGTH ? styles.textOverflow : ""
      }`}
    >
      <span role="status">
        {textLength > MAX_TEXT_LENGTH
          ? `内容超出${MAX_TEXT_LENGTH}字上限，请删减`
          : textLength === MAX_TEXT_LENGTH
          ? `已达${MAX_TEXT_LENGTH}字上限`
          : ""}
      </span>
      <span>
        {textLength} / {MAX_TEXT_LENGTH}
      </span>
    </div>
  );

  useLayoutEffect(() => {
    const dialog = dialogRef.current;
    if (!dialog) return;
    dialog.showModal();
    return () => {
      if (dialog.open) dialog.close();
    };
  }, []);

  const submit = (values: EditorValues) => {
    if (submittedRef.current) return;
    submittedRef.current = true;
    let change: InsuranceEditorChange;
    if (kind === "insurancePlan")
      change = { kind, value: values.insurancePlan.trim() };
    else {
      const value = {
        date: values.date,
        content: values.content.trim(),
      };
      const target = { recordId, requestId: uuid() };
      change = isFollowUp
        ? {
            kind: "followUp",
            value: {
              ...value,
              method: values.method,
              result: values.result,
              ...values.scene,
            },
            ...target,
          }
        : {
            kind: "contactPlan",
            value: { ...value, method: values.method },
            ...target,
          };
    }
    onSave(change);
  };

  return (
    <dialog
      ref={dialogRef}
      className={`${workbenchStyles.dialog} ${styles.editor}`}
      aria-labelledby={titleId}
      onCancel={(event) => {
        event.preventDefault();
        onClose();
      }}
    >
      <button
        type="button"
        className={`${workbenchStyles.iconbtn} ${workbenchStyles.dialogClose}`}
        aria-label="关闭"
        onClick={onClose}
      >
        <Icon name="close" />
      </button>
      <Form
        form={form}
        layout="vertical"
        requiredMark={false}
        onFinish={submit}
        noValidate
        initialValues={{
          insurancePlan: initialPlan,
          scene: initialScene,
          date: entry?.date || trackingNow(isFollowUp),
          method: entry ? entry.method : methods[0].value,
          content: entry?.content ?? "",
          result:
            draft?.kind === "followUp"
              ? draft.value.result
              : record && "result" in record
              ? record.result
              : 1,
        }}
      >
        <div className={workbenchStyles.dialogHead}>
          <h2 id={titleId}>{title}</h2>
        </div>
        <div className={workbenchStyles.dialogBody}>
          {isFollowUp && (
            <>
              <div
                className={`${workbenchStyles.detailGrid} ${styles.customerInfo}`}
                aria-label="跟进客户信息"
              >
                <div className={styles.customerName}>
                  <small>客户姓名</small>
                  <strong>{customer.name}</strong>
                </div>
                <FollowUpSceneField defaultScene={initialScene} />
              </div>
            </>
          )}
          {kind === "insurancePlan" ? (
            <Form.Item
              className={styles.field}
              name="insurancePlan"
              label="保险计划"
              rules={[
                { required: true, whitespace: true, message: "请输入保险计划" },
                TEXT_LIMIT_RULE,
              ]}
              extra={textHint}
            >
              <textarea
                className={workbenchStyles.input}
                rows={4}
                maxLength={MAX_TEXT_LENGTH}
                aria-required="true"
              />
            </Form.Item>
          ) : (
            <>
              <Form.Item
                className={styles.field}
                name="date"
                label={isFollowUp ? "触达时间" : "接触日期"}
                rules={[
                  {
                    required: true,
                    message: isFollowUp ? "请选择触达时间" : "请选择接触日期",
                  },
                ]}
              >
                <TrackingDatePicker
                  withTime={isFollowUp}
                  aria-required="true"
                />
              </Form.Item>
              <Form.Item
                className={styles.field}
                name="method"
                label="触达方式"
                rules={[
                  {
                    required: true,
                    message: "请选择触达方式",
                  },
                ]}
              >
                <TrackingSelect
                  aria-required="true"
                  placeholder="请选择触达方式"
                  options={methods}
                />
              </Form.Item>
              {isFollowUp && (
                <Form.Item
                  className={styles.field}
                  name="result"
                  label="触达结果"
                  rules={[{ required: true, message: "请选择触达结果" }]}
                >
                  <TrackingSelect
                    aria-required="true"
                    placeholder="请选择"
                    options={Object.entries(FOLLOW_UP_RESULTS).map(
                      ([value, label]) => ({ value: Number(value), label }),
                    )}
                  />
                </Form.Item>
              )}
              <Form.Item
                className={styles.field}
                name="content"
                label={
                  isFollowUp ? (
                    <span>
                      沟通记录
                      <span className={styles.required} aria-hidden="true">
                        {" "}
                        *
                      </span>
                    </span>
                  ) : (
                    "接触安排"
                  )
                }
                rules={[
                  {
                    required: true,
                    whitespace: true,
                    message: isFollowUp ? "请输入沟通记录" : "请输入接触安排",
                  },
                  TEXT_LIMIT_RULE,
                ]}
                extra={textHint}
              >
                <textarea
                  className={workbenchStyles.input}
                  rows={4}
                  maxLength={MAX_TEXT_LENGTH}
                  aria-required="true"
                  placeholder={
                    isFollowUp
                      ? "记录客户需求、沟通结果与后续安排"
                      : "填写接触目的与具体安排"
                  }
                />
              </Form.Item>
            </>
          )}
        </div>
        <div className={workbenchStyles.dialogFooter}>
          <button
            type="button"
            className={workbenchStyles.btn}
            onClick={onClose}
          >
            取消
          </button>
          <button
            type="submit"
            className={`${workbenchStyles.btn} ${workbenchStyles.primary}`}
          >
            {isFollowUp ? "保存记录" : "保存"}
          </button>
        </div>
      </Form>
    </dialog>
  );
}
