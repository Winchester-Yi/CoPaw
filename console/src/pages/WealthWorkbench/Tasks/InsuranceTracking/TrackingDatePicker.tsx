import { useEffect, useRef, useState } from "react";
import { DatePicker, type DatePickerProps, type GetRef } from "antd";
import zhCN from "antd/es/date-picker/locale/zh_CN";
import dayjs from "dayjs";
import "dayjs/locale/zh-cn";
import workbenchStyles from "../../index.module.less";
import styles from "./TrackingDatePicker.module.less";

import { DATE_FORMAT, TIME_VALUE_FORMAT } from "./trackingDate";

type Props = Pick<
  DatePickerProps,
  | "id"
  | "disabled"
  | "aria-label"
  | "aria-required"
  | "aria-describedby"
  | "aria-invalid"
> & {
  value?: string;
  onChange?: (value: string) => void;
  withTime?: boolean;
};

export function TrackingDatePicker({
  value,
  onChange,
  withTime = false,
  ...props
}: Props) {
  const pickerRef = useRef<GetRef<typeof DatePicker>>(null);
  const [open, setOpen] = useState(false);
  const [popupWidth, setPopupWidth] = useState<number>();
  useEffect(() => {
    const element = pickerRef.current?.nativeElement;
    if (!element) return;
    const resize = () => setPopupWidth(element.getBoundingClientRect().width);
    resize();
    const observer = new ResizeObserver(resize);
    observer.observe(element);
    return () => observer.disconnect();
  }, []);
  useEffect(() => {
    if (!open) return;
    const dialog = pickerRef.current?.nativeElement.closest("dialog");
    const closeCalendar = (event: KeyboardEvent) => {
      if (event.key !== "Escape") return;
      event.preventDefault();
      event.stopPropagation();
      setOpen(false);
    };
    dialog?.addEventListener("keydown", closeCalendar, true);
    return () => dialog?.removeEventListener("keydown", closeCalendar, true);
  }, [open]);
  const parsed = value ? dayjs(value) : null;
  return (
    <DatePicker
      {...props}
      ref={pickerRef}
      open={open}
      onOpenChange={setOpen}
      className={`${workbenchStyles.taskDatePicker} ${styles.picker}`}
      classNames={{
        popup: { root: `${styles.popup} ${withTime ? styles.timePopup : ""}` },
      }}
      styles={{ popup: { root: { width: popupWidth } } }}
      locale={zhCN}
      inputReadOnly
      value={parsed?.isValid() ? parsed.locale("zh-cn") : null}
      format={withTime ? "YYYY-MM-DD HH:mm" : DATE_FORMAT}
      showTime={withTime ? { format: "HH:mm" } : false}
      placeholder={withTime ? "请选择触达时间" : "请选择接触日期"}
      onChange={(date) =>
        onChange?.(
          date?.format(withTime ? TIME_VALUE_FORMAT : DATE_FORMAT) ?? "",
        )
      }
      getPopupContainer={(trigger) =>
        trigger.closest("dialog") ?? trigger.parentElement ?? document.body
      }
    />
  );
}
