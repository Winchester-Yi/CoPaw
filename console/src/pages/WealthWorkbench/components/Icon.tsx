/**
 * 智能财富工作台 —— SVG 图标（sprite 方式移植自原型内联 symbol 定义）
 */
import cx from "classnames";
import styles from "../index.module.less";

export type IconName =
  | "home"
  | "plus"
  | "list"
  | "user"
  | "users"
  | "check"
  | "bell"
  | "down"
  | "up"
  | "right"
  | "left"
  | "calendar"
  | "clock"
  | "layer"
  | "shield"
  | "safe"
  | "chart"
  | "pie"
  | "globe"
  | "bank"
  | "phone"
  | "chat"
  | "megaphone"
  | "menu"
  | "trash"
  | "close"
  | "building"
  | "money";

export function Icon({
  name,
  className,
}: {
  name: IconName | string;
  className?: string;
}) {
  return (
    <svg className={cx(styles.icon, className)} aria-hidden="true">
      <use href={`#i-${name}`} />
    </svg>
  );
}

/** 页面内一次性渲染的 symbol 定义块 */
export function IconDefs() {
  return (
    <svg
      style={{ position: "absolute", width: 0, height: 0, overflow: "hidden" }}
      aria-hidden="true"
    >
      <defs>
        <symbol id="i-home" viewBox="0 0 24 24">
          <path d="m3 10 9-7 9 7v10H3Z" />
          <path d="M9 20v-7h6v7" />
        </symbol>
        <symbol id="i-plus" viewBox="0 0 24 24">
          <rect x="3" y="3" width="18" height="18" rx="3" />
          <path d="M12 7v10M7 12h10" />
        </symbol>
        <symbol id="i-list" viewBox="0 0 24 24">
          <rect x="5" y="4" width="14" height="17" rx="2" />
          <path d="M9 3h6v3H9ZM9 10h6M9 14h6M9 18h4" />
        </symbol>
        <symbol id="i-user" viewBox="0 0 24 24">
          <circle cx="12" cy="7" r="4" />
          <path d="M4 21v-3a8 8 0 0 1 16 0v3Z" />
        </symbol>
        <symbol id="i-users" viewBox="0 0 24 24">
          <circle cx="9" cy="7" r="3" />
          <path d="M2 21v-4a7 7 0 0 1 14 0v4ZM16 4a3 3 0 0 1 0 6M19 21v-5a5 5 0 0 0-2-4" />
        </symbol>
        <symbol id="i-check" viewBox="0 0 24 24">
          <rect x="4" y="4" width="16" height="17" rx="2" />
          <path d="M9 3h6M8 12l3 3 5-6" />
        </symbol>
        <symbol id="i-bell" viewBox="0 0 24 24">
          <path d="M5 10a7 7 0 0 1 14 0v6l2 3H3l2-3ZM10 22h4M12 1v2" />
        </symbol>
        <symbol id="i-down" viewBox="0 0 24 24">
          <path d="m6 9 6 6 6-6" />
        </symbol>
        <symbol id="i-up" viewBox="0 0 24 24">
          <path d="m6 15 6-6 6 6" />
        </symbol>
        <symbol id="i-right" viewBox="0 0 24 24">
          <path d="m9 5 7 7-7 7" />
        </symbol>
        <symbol id="i-left" viewBox="0 0 24 24">
          <path d="m15 5-7 7 7 7" />
        </symbol>
        <symbol id="i-calendar" viewBox="0 0 24 24">
          <rect x="3" y="5" width="18" height="16" rx="2" />
          <path d="M7 3v4M17 3v4M3 10h18M7 14h2M13 14h2M7 17h2M13 17h2" />
        </symbol>
        <symbol id="i-clock" viewBox="0 0 24 24">
          <circle cx="12" cy="12" r="9" />
          <path d="M12 7v5l3 3" />
        </symbol>
        <symbol id="i-layer" viewBox="0 0 24 24">
          <path d="m12 2 10 6-10 6L2 8Zm-9 11 9 5 9-5M3 17l9 5 9-5" />
        </symbol>
        <symbol id="i-shield" viewBox="0 0 24 24">
          <path d="m12 2 9 4v6c0 5-9 10-9 10S3 17 3 12V6Z" />
          <path d="m8 11 3 3 5-6" />
        </symbol>
        <symbol id="i-safe" viewBox="0 0 24 24">
          <path d="m12 2 9 4v6c0 5-9 10-9 10S3 17 3 12V6Z" />
          <path d="M12 7v9M8 11h8" />
        </symbol>
        <symbol id="i-chart" viewBox="0 0 24 24">
          <path d="M3 21V12h4v9ZM10 21V7h4v14ZM17 21V2h4v19Z" />
        </symbol>
        <symbol id="i-pie" viewBox="0 0 24 24">
          <path d="M12 3a9 9 0 1 0 9 9h-9ZM15 2v7h7a9 9 0 0 0-7-7Z" />
        </symbol>
        <symbol id="i-globe" viewBox="0 0 24 24">
          <circle cx="12" cy="12" r="10" />
          <ellipse cx="12" cy="12" rx="4" ry="10" />
          <path d="M3 8h18M3 16h18" />
        </symbol>
        <symbol id="i-bank" viewBox="0 0 24 24">
          <path d="m2 7 10-5 10 5ZM3 21h18M4 18h16M5 10v6M10 10v6M14 10v6M19 10v6" />
        </symbol>
        <symbol id="i-phone" viewBox="0 0 24 24">
          <path d="m6 3 4 5-3 3a16 16 0 0 0 6 6l3-3 5 4-2 3C10 23 1 13 3 5Z" />
        </symbol>
        <symbol id="i-chat" viewBox="0 0 24 24">
          <path d="M14 13a8 8 0 1 0-9 2l-1 4 5-2" />
          <path d="M21 16a6 6 0 1 0-3 5l4 1-1-4Z" />
          <path d="M6 8h.1M11 8h.1M13 16h.1M18 16h.1" />
        </symbol>
        <symbol id="i-megaphone" viewBox="0 0 24 24">
          <path d="M3 9h5l10-5v16L8 15H3ZM8 15l2 7H6l-2-7M21 9l2-1M21 15l2 1" />
        </symbol>
        <symbol id="i-menu" viewBox="0 0 24 24">
          <path d="M8 5h13M8 12h13M8 19h13M3 5h.1M3 12h.1M3 19h.1" />
        </symbol>
        <symbol id="i-trash" viewBox="0 0 24 24">
          <path d="M3 6h18M9 6V3h6v3M6 6l1 15h10l1-15M10 10v7M14 10v7" />
        </symbol>
        <symbol id="i-close" viewBox="0 0 24 24">
          <path d="m6 6 12 12M18 6 6 18" />
        </symbol>
        <symbol id="i-building" viewBox="0 0 24 24">
          <path d="M3 22V5l10-3v20M13 9h7v13M1 22h22M7 7h2M7 11h2M7 15h2M7 19h2M16 12h1M16 16h1" />
        </symbol>
        <symbol id="i-money" viewBox="0 0 24 24">
          <path d="m8 3 4 4 4-4ZM8 7C5 11 3 13 3 18a3 3 0 0 0 3 3h12a3 3 0 0 0 3-3c0-5-2-7-5-11Z" />
          <path d="M15 11h-5a2 2 0 0 0 0 4h3a2 2 0 0 1 0 4H9M12 10v10" />
        </symbol>
      </defs>
    </svg>
  );
}
