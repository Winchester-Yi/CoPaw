/**
 * 智能财富工作台 —— 执行时刻选择器
 * 点击字段任意位置展开「时 / 分」两列下拉；样式独立于控制台 antd，
 * 仅数据模型（hour/minute → cron）与定时任务表单一致。
 */
import { useEffect, useRef, useState } from "react";
import cx from "classnames";
import styles from "../index.module.less";
import { Icon } from "./Icon";

const HOURS = Array.from({ length: 24 }, (_, i) => i);
const MINUTES = Array.from({ length: 60 }, (_, i) => i);
/** 选项行高（与 less 中 .timeOpt 的 height 保持一致），用于展开时滚动定位 */
const OPTION_HEIGHT = 26;
const PANEL_WIDTH = 158;

function pad2(value: number): string {
  return String(value).padStart(2, "0");
}

/** 展开时将列滚动到当前值附近（向上留两行余量） */
function scrollToValue(col: HTMLDivElement | null, value: number) {
  if (col) col.scrollTop = Math.max(0, (value - 2) * OPTION_HEIGHT);
}

export function TimeField({
  hour,
  minute,
  onChange,
  ariaLabel,
}: {
  hour: number;
  minute: number;
  onChange: (hour: number, minute: number) => void;
  ariaLabel: string;
}) {
  const [open, setOpen] = useState(false);
  const [pos, setPos] = useState({ top: 0, left: 0 });
  const rootRef = useRef<HTMLDivElement>(null);
  const hourColRef = useRef<HTMLDivElement>(null);
  const minuteColRef = useRef<HTMLDivElement>(null);

  // 展开：fixed 定位避免被 selectedList 横向滚动容器裁剪；收起：外部点击/Esc/滚动
  useEffect(() => {
    if (!open) return;
    const rect = rootRef.current?.getBoundingClientRect();
    if (rect) {
      setPos({
        top: rect.bottom + 4,
        left: Math.max(
          8,
          Math.min(rect.left, window.innerWidth - PANEL_WIDTH - 8),
        ),
      });
    }
    scrollToValue(hourColRef.current, hour);
    scrollToValue(minuteColRef.current, minute);
    // 页面可能有进行中的平滑滚动（如步骤导航/自动定位），展开后 400ms 内的滚动事件不收起；
    // 面板自身列内的滚动（如选择 late 选项时的定位滚动）也不收起
    const openedAt = Date.now();
    const onDown = (e: MouseEvent) => {
      if (!rootRef.current?.contains(e.target as Node)) setOpen(false);
    };
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") setOpen(false);
    };
    const close = (e: Event) => {
      if (Date.now() - openedAt <= 400) return;
      if (rootRef.current?.contains(e.target as Node)) return;
      setOpen(false);
    };
    document.addEventListener("mousedown", onDown);
    document.addEventListener("keydown", onKey);
    window.addEventListener("scroll", close, true);
    window.addEventListener("resize", close);
    return () => {
      document.removeEventListener("mousedown", onDown);
      document.removeEventListener("keydown", onKey);
      window.removeEventListener("scroll", close, true);
      window.removeEventListener("resize", close);
    };
  }, [open, hour, minute]);

  return (
    <div className={styles.timeField} ref={rootRef}>
      <button
        type="button"
        className={styles.timeValue}
        aria-label={ariaLabel}
        aria-expanded={open}
        onClick={() => setOpen((v) => !v)}
      >
        <span>
          {pad2(hour)}:{pad2(minute)}
        </span>
        <Icon name="clock" />
      </button>
      {open && (
        <div
          className={styles.timePanel}
          style={{ top: pos.top, left: pos.left }}
        >
          <div className={styles.timeCol} ref={hourColRef} aria-label="小时">
            {HOURS.map((h) => (
              <button
                key={h}
                type="button"
                className={cx(styles.timeOpt, h === hour && styles.active)}
                aria-pressed={h === hour}
                onClick={() => onChange(h, minute)}
              >
                {pad2(h)}
              </button>
            ))}
          </div>
          <div className={styles.timeCol} ref={minuteColRef} aria-label="分钟">
            {MINUTES.map((m) => (
              <button
                key={m}
                type="button"
                className={cx(styles.timeOpt, m === minute && styles.active)}
                aria-pressed={m === minute}
                onClick={() => {
                  onChange(hour, m);
                  setOpen(false);
                }}
              >
                {pad2(m)}
              </button>
            ))}
          </div>
        </div>
      )}
    </div>
  );
}
