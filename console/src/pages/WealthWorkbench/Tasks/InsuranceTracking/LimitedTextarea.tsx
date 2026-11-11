import { useLayoutEffect, useRef, type TextareaHTMLAttributes } from "react";
import { MAX_TEXT_LENGTH } from "./textLimit";
import workbenchStyles from "../../index.module.less";
import styles from "./LimitedTextarea.module.less";

export function LimitedTextarea({
  autoSize = false,
  showLimitHint = false,
  ...props
}: TextareaHTMLAttributes<HTMLTextAreaElement> & {
  autoSize?: boolean;
  showLimitHint?: boolean;
}) {
  const ref = useRef<HTMLTextAreaElement>(null);
  useLayoutEffect(() => {
    const element = ref.current;
    if (!autoSize || !element) return;
    const resize = () => {
      element.style.height = "auto";
      element.style.height = `${Math.min(
        Math.max(element.scrollHeight + 2, 36),
        120,
      )}px`;
    };
    resize();
    if (typeof ResizeObserver === "undefined") return;
    let width = element.clientWidth;
    const observer = new ResizeObserver(() => {
      if (width !== element.clientWidth) {
        width = element.clientWidth;
        resize();
      }
    });
    observer.observe(element);
    return () => observer.disconnect();
  }, [autoSize, props.value]);
  const length = String(props.value ?? "").length;
  return (
    <div>
      <textarea
        ref={ref}
        {...props}
        className={workbenchStyles.input}
        maxLength={MAX_TEXT_LENGTH}
      />
      <div className={styles.hint}>
        {(showLimitHint || length >= MAX_TEXT_LENGTH) && (
          <span role="status">
            {length > MAX_TEXT_LENGTH
              ? `内容超出${MAX_TEXT_LENGTH}字上限，请删减`
              : length === MAX_TEXT_LENGTH
              ? `已达${MAX_TEXT_LENGTH}字上限`
              : `最多可输入${MAX_TEXT_LENGTH}字`}
          </span>
        )}
        <span className={styles.count}>
          {length} / {MAX_TEXT_LENGTH}
        </span>
      </div>
    </div>
  );
}
