import { Select, type SelectProps } from "antd";
import styles from "./TrackingSelect.module.less";

export function TrackingSelect<ValueType = number>(
  props: SelectProps<ValueType>,
) {
  return (
    <Select<ValueType>
      {...props}
      className={`${styles.select} ${props.className ?? ""}`}
      style={{ width: "100%", ...props.style }}
      getPopupContainer={(trigger) =>
        trigger.closest("dialog") ?? trigger.parentElement ?? document.body
      }
    />
  );
}
