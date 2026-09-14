/**
 * 智能财富工作台 —— 全局弹窗宿主
 * 对应原型的 <dialog id="dialog">：store 驱动，支持宽弹窗与遮罩点击关闭。
 */
import { useEffect, useRef } from "react";
import cx from "classnames";
import styles from "../index.module.less";
import { useWealthStore } from "../store";
import { Icon } from "./Icon";

export function DialogHost() {
  const dialog = useWealthStore((s) => s.dialog);
  const closeDialog = useWealthStore((s) => s.closeDialog);
  const ref = useRef<HTMLDialogElement>(null);

  useEffect(() => {
    const el = ref.current;
    if (!el) return;
    if (dialog) {
      if (!el.open) el.showModal();
    } else if (el.open) {
      el.close();
    }
  }, [dialog]);

  // 遮罩点击关闭（与原型一致：点击 dialog 元素自身且坐标在内容盒之外）
  const onClick = (e: React.MouseEvent<HTMLDialogElement>) => {
    if (e.target !== ref.current) return;
    const r = e.currentTarget.getBoundingClientRect();
    if (
      e.clientX < r.left ||
      e.clientX > r.right ||
      e.clientY < r.top ||
      e.clientY > r.bottom
    ) {
      closeDialog();
    }
  };

  return (
    <dialog
      ref={ref}
      className={cx(styles.dialog, dialog?.wide && styles.calendarDialog)}
      onClick={onClick}
      onClose={closeDialog}
    >
      {dialog && (
        <>
          <div className={styles.dialogHead}>
            <h2>{dialog.title}</h2>
            <button
              className={styles.iconbtn}
              onClick={closeDialog}
              aria-label="关闭"
            >
              <Icon name="close" />
            </button>
          </div>
          <div className={styles.dialogBody}>{dialog.body}</div>
          <div className={styles.dialogFooter}>
            {dialog.buttons.map((b) => (
              <button
                key={b.label}
                className={cx(
                  styles.btn,
                  b.primary && styles.primary,
                  b.danger && styles.danger,
                )}
                onClick={() => (b.onClick ? b.onClick() : closeDialog())}
              >
                {b.label}
              </button>
            ))}
          </div>
        </>
      )}
    </dialog>
  );
}
