import { useId, useLayoutEffect, useRef, useState } from "react";
import { Tooltip } from "antd";
import {
  EDIT_LABELS,
  FOLLOW_UP_RESULTS,
  followUpMethodLabel,
  type ContactRecord,
  type FollowUpRecord,
  type EditKind,
} from "./api";
import workbenchStyles from "../../index.module.less";
import styles from "./index.module.less";

type RecordEntry = ContactRecord | FollowUpRecord;
type RecordKind = Exclude<EditKind, "insurancePlan">;

function RecordText({
  entry,
  full,
  onEdit,
  disabled,
  customerName,
  kind,
}: {
  entry: RecordEntry;
  full?: boolean;
  onEdit: () => void;
  disabled: boolean;
  customerName: string;
  kind: RecordKind;
}) {
  if (!full) {
    const date = entry.date.replace("T", " ").slice(0, 16);
    return (
      <Tooltip
        title={`${entry.date.replace("T", " ")} · ${followUpMethodLabel(
          entry.method,
        )} · ${entry.content || "—"}`}
        trigger={["hover", "focus"]}
      >
        <div className={styles.recordLine} tabIndex={0}>
          <span className={styles.recordTime}>
            {date} · {followUpMethodLabel(entry.method)}
          </span>
          <span className={styles.recordSummary}>{entry.content || "—"}</span>
        </div>
      </Tooltip>
    );
  }
  return (
    <div className={styles.record}>
      <div className={styles.recordHead}>
        <p className={styles.fullContent}>{entry.content}</p>
        <Tooltip title="编辑此条" trigger={["hover", "focus"]}>
          <button
            type="button"
            className={`${workbenchStyles.btn} ${styles.edit}`}
            disabled={disabled}
            onClick={onEdit}
            aria-label={`编辑${customerName}的${EDIT_LABELS[kind]} ${entry.date}`}
          >
            编辑
          </button>
        </Tooltip>
      </div>
      <div className={styles.date}>
        <span>{entry.date.replace("T", " ")}</span>
        {" · "}
        <span>{followUpMethodLabel(entry.method)}</span>
        {"result" in entry
          ? ` · ${
              entry.result == null
                ? "触达结果未知"
                : FOLLOW_UP_RESULTS[entry.result]
            }`
          : ""}
      </div>
    </div>
  );
}

function RecordHistory({
  entries,
  kind,
  customerName,
  disabled,
  onEdit,
  onAdd,
  onClose,
}: {
  entries: RecordEntry[];
  kind: RecordKind;
  customerName: string;
  disabled: boolean;
  onEdit: (entry: RecordEntry) => void;
  onAdd: () => void;
  onClose: () => void;
}) {
  const ref = useRef<HTMLDialogElement>(null);
  const titleId = useId();
  useLayoutEffect(() => {
    const dialog = ref.current;
    dialog?.showModal();
    return () => {
      if (dialog?.open) dialog.close();
    };
  }, []);
  return (
    <dialog
      ref={ref}
      aria-labelledby={titleId}
      className={`${workbenchStyles.dialog} ${styles.history}`}
      onCancel={(event) => {
        event.preventDefault();
        onClose();
      }}
    >
      <div className={workbenchStyles.dialogHead}>
        <h2 id={titleId}>
          {EDIT_LABELS[kind]} · {customerName}
          <span className={styles.historyCount}>共 {entries.length} 条</span>
        </h2>
      </div>
      <button
        type="button"
        className={`${workbenchStyles.iconbtn} ${workbenchStyles.dialogClose}`}
        aria-label="关闭"
        onClick={onClose}
      >
        ×
      </button>
      <div className={`${workbenchStyles.dialogBody} ${styles.historyBody}`}>
        {entries.map((entry) => (
          <RecordText
            key={entry.id}
            entry={entry}
            full
            kind={kind}
            customerName={customerName}
            disabled={disabled}
            onEdit={() => {
              ref.current?.close();
              onClose();
              onEdit(entry);
            }}
          />
        ))}
      </div>
      <div className={workbenchStyles.dialogFooter}>
        <button type="button" className={workbenchStyles.btn} onClick={onClose}>
          关闭
        </button>
        <button
          type="button"
          className={`${workbenchStyles.btn} ${workbenchStyles.primary}`}
          disabled={disabled}
          onClick={() => {
            ref.current?.close();
            onClose();
            onAdd();
          }}
        >
          {kind === "contactPlan" ? "新增计划" : "新增记录"}
        </button>
      </div>
    </dialog>
  );
}

export function RecordCell({
  entries,
  kind,
  customerName,
  disabled,
  onEdit,
  onAdd,
}: {
  entries: RecordEntry[];
  kind: RecordKind;
  customerName: string;
  disabled: boolean;
  onEdit: (entry: RecordEntry) => void;
  onAdd: () => void;
}) {
  const [history, setHistory] = useState(false);
  return (
    <div className={styles.cell}>
      <div className={styles.records}>
        {entries.length ? (
          entries.slice(0, 1).map((entry) => (
            <div key={entry.id} className={styles.recordHead}>
              <RecordText
                entry={entry}
                kind={kind}
                customerName={customerName}
                disabled={disabled}
                onEdit={() => setHistory(true)}
              />
            </div>
          ))
        ) : (
          <p className={styles.secondary}>
            {kind === "contactPlan" ? "暂无接触计划" : "暂无跟进记录"}
          </p>
        )}
      </div>
      <div className={styles.recordActions}>
        <button
          type="button"
          className={`${workbenchStyles.btn} ${workbenchStyles.primary} ${styles.recordButton}`}
          disabled={disabled}
          aria-label={`新增${customerName}的${EDIT_LABELS[kind]}`}
          onClick={onAdd}
        >
          {kind === "contactPlan" ? "新增计划" : "新增记录"}
        </button>
        {entries.length > 0 && (
          <Tooltip
            title={`查看全部 ${entries.length} 条${
              kind === "contactPlan" ? "计划" : "记录"
            }，可逐条编辑`}
            trigger={["hover", "focus"]}
          >
            <button
              type="button"
              className={`${workbenchStyles.btn} ${styles.recordButton} ${styles.manageButton}`}
              aria-label={`查看${customerName}的全部${EDIT_LABELS[kind]}`}
              onClick={() => setHistory(true)}
            >
              {entries.length > 1 ? `编辑 (${entries.length})` : "编辑"}
            </button>
          </Tooltip>
        )}
      </div>
      {history && (
        <RecordHistory
          entries={entries}
          kind={kind}
          customerName={customerName}
          disabled={disabled}
          onEdit={onEdit}
          onAdd={onAdd}
          onClose={() => setHistory(false)}
        />
      )}
    </div>
  );
}
