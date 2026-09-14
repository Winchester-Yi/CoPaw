/**
 * 智能财富工作台 —— 角色预览器（mock 期专用）
 * 对应原型 <details class="account-switcher">；仅用于开发/验收期查看各角色的页面权限，
 * 不代表切换登录账号。生产环境身份由父系统经 iframeStore.positionId 决定，
 * 本组件在接口接真后随 IS_MOCK=false 隐藏（见 Topbar）。
 */
import { useEffect, useRef, useState } from "react";
import cx from "classnames";
import styles from "../index.module.less";
import { selectCurrentAccount, useWealthStore } from "../store";
import { Icon } from "./Icon";

export function AccountSwitcher() {
  const accounts = useWealthStore((s) => s.accounts);
  const account = useWealthStore(selectCurrentAccount);
  const previewRole = useWealthStore((s) => s.previewRole);
  const [open, setOpen] = useState(false);
  const rootRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!open) return;
    const onDocClick = (e: MouseEvent) => {
      if (!rootRef.current?.contains(e.target as Node)) setOpen(false);
    };
    const onKeyDown = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        e.preventDefault();
        setOpen(false);
      }
    };
    const onFocusIn = (e: FocusEvent) => {
      if (!rootRef.current?.contains(e.target as Node)) setOpen(false);
    };
    document.addEventListener("click", onDocClick);
    document.addEventListener("keydown", onKeyDown);
    document.addEventListener("focusin", onFocusIn);
    return () => {
      document.removeEventListener("click", onDocClick);
      document.removeEventListener("keydown", onKeyDown);
      document.removeEventListener("focusin", onFocusIn);
    };
  }, [open]);

  if (!account) return null;

  return (
    <div
      ref={rootRef}
      className={cx(styles.accountSwitcher, open && styles.open)}
    >
      <button
        className={styles.switcherTrigger}
        aria-label="角色预览"
        aria-controls="wealthAccountOptions"
        aria-expanded={open}
        onClick={() => setOpen((v) => !v)}
      >
        <span className={cx(styles.switcherAvatar, styles[account.id])}>
          {account.name.slice(0, 1)}
        </span>
        <span className={styles.switcherIdentity}>
          <strong>{account.name}</strong>
          <span>{account.role}</span>
        </span>
        <Icon name="down" className={styles.switcherChevron} />
      </button>
      {open && (
        <div className={styles.accountPopover} id="wealthAccountOptions">
          <div className={styles.accountPopoverHeading}>
            角色预览<span>{accounts.length} 个角色</span>
          </div>
          <div>
            {accounts.map((x) => (
              <button
                key={x.id}
                className={cx(
                  styles.accountOption,
                  x.id === account.id && styles.selected,
                )}
                aria-pressed={x.id === account.id}
                onClick={() => {
                  setOpen(false);
                  void previewRole(x.id);
                }}
              >
                <span className={cx(styles.switcherAvatar, styles[x.id])}>
                  {x.name.slice(0, 1)}
                </span>
                <span className={styles.accountOptionInfo}>
                  <span className={styles.accountOptionLine}>
                    <strong>{x.name}</strong>
                    <span className={styles.accountRoleTag}>{x.role}</span>
                  </span>
                  <small>{x.department}</small>
                </span>
                <span className={styles.accountOptionCheck} aria-hidden="true">
                  {x.id === account.id ? "✓" : ""}
                </span>
              </button>
            ))}
          </div>
          <p className={styles.pageNote}>
            演示身份，仅用于查看各角色页面权限；真实身份由父系统传入。
          </p>
        </div>
      )}
    </div>
  );
}
