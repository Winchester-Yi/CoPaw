/**
 * 智能财富工作台 —— Toast 提示
 * 对应原型 #toast：显示 3.2s 后自动消失。
 */
import { useEffect, useState } from "react";
import cx from "classnames";
import styles from "../index.module.less";
import { useWealthStore } from "../store";

export function Toast() {
  const toastText = useWealthStore((s) => s.toastText);
  const toastSeq = useWealthStore((s) => s.toastSeq);
  const [visible, setVisible] = useState(false);

  useEffect(() => {
    if (!toastSeq) return;
    setVisible(true);
    const timer = setTimeout(() => setVisible(false), 3200);
    return () => clearTimeout(timer);
  }, [toastSeq]);

  return (
    <div
      className={cx(styles.toast, visible && styles.visible)}
      role="status"
      aria-live="polite"
    >
      {toastText}
    </div>
  );
}
