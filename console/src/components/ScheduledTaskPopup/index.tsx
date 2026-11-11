import { useState, useCallback, useEffect, useMemo, useRef } from "react";
import { Checkbox, Modal, Tooltip } from "antd";
import type {
  ManagedCronContext,
  ManagedCronSchedule,
} from "@/api/types/schedulerCron";
import { useManagedCalendar } from "./useManagedCalendar";
import { useAppMessage } from "../../hooks/useAppMessage";
import {
  FrequencyType,
  ScheduleConfig,
  WEEKDAY_LABELS,
  validateHour,
  validateMinute,
  formatTimeValue,
  formatNextRunPreview,
  generateCronExpression,
  hasDateBoundaryWarning,
  toNotificationDelayMinutes,
  type NotificationDelayUnit,
} from "../../utils/cron";
import type { ModelSlotConfig } from "@/api/types";
import {
  DEFAULT_EXECUTION_MODEL_KEY,
  parseExecutionModelKey,
  useExecutionModelOptions,
} from "@/hooks/useExecutionModelOptions";
import styles from "./index.module.less";

export interface ScheduledTaskPopupProps {
  open: boolean;
  onClose: () => void;
  onConfirm: (
    cronExpression: string,
    config: ScheduleConfig,
    modelSlot?: ModelSlotConfig,
    notificationDelayMinutes?: number,
    managedSchedule?: ManagedCronSchedule,
  ) => Promise<void>;
  onSuccess?: () => void;
  caseValue?: string;
  managedContext?: ManagedCronContext;
  settingsLocked?: boolean;
  /** 快捷入口可隐藏日历选项，并固定非工作日不执行。 */
  showCalendarOptions?: boolean;
}

interface TimeState {
  hour: number;
  minute: number;
}

const DEFAULT_TIME: TimeState = { hour: 9, minute: 0 };
const DEFAULT_WEEKDAYS = [1];
const DEFAULT_DATES = [1];

export default function ScheduledTaskPopup({
  open,
  onClose,
  onConfirm,
  onSuccess,
  managedContext,
  settingsLocked = false,
  showCalendarOptions = true,
}: ScheduledTaskPopupProps) {
  const { message } = useAppMessage();
  const isManaged = Boolean(managedContext);
  const retryLocked = useRef(settingsLocked);
  retryLocked.current = settingsLocked;
  const [frequency, setFrequency] = useState<FrequencyType | "lastWorkday">(
    "daily",
  );
  const [runOnNonWorkday, setRunOnNonWorkday] = useState(false);
  const live = useRef(true);
  const current = useRef({ open, identity: JSON.stringify(managedContext) });
  current.current = { open, identity: JSON.stringify(managedContext) };
  useEffect(() => {
    live.current = true;
    return () => {
      live.current = false;
    };
  }, []);
  const [time, setTime] = useState<TimeState>(DEFAULT_TIME);
  const [weekdays, setWeekdays] = useState<number[]>(DEFAULT_WEEKDAYS);
  const [dates, setDates] = useState<number[]>(DEFAULT_DATES);
  const [loading, setLoading] = useState(false);
  const [executionModelKey, setExecutionModelKey] = useState(
    DEFAULT_EXECUTION_MODEL_KEY,
  );
  const [notificationDelayValue, setNotificationDelayValue] = useState("0");
  const [notificationDelayUnit, setNotificationDelayUnit] =
    useState<NotificationDelayUnit>("minutes");
  // 临时输入值，允许用户自由编辑
  const [hourInput, setHourInput] = useState<string>(
    formatTimeValue(DEFAULT_TIME.hour),
  );
  const [minuteInput, setMinuteInput] = useState<string>(
    formatTimeValue(DEFAULT_TIME.minute),
  );
  const {
    loading: executionModelLoading,
    options: executionModelOptions,
    tenantDefaultLabel,
  } = useExecutionModelOptions(open);

  useEffect(() => {
    if (open && !(isManaged && retryLocked.current)) {
      setExecutionModelKey(DEFAULT_EXECUTION_MODEL_KEY);
      setNotificationDelayValue("0");
      setNotificationDelayUnit("minutes");
    }
  }, [open, isManaged]);

  const handleFrequencyChange = useCallback(
    (newFrequency: FrequencyType | "lastWorkday") => {
      setFrequency(newFrequency);
      if (!isManaged) {
        setTime(DEFAULT_TIME);
        setHourInput(formatTimeValue(DEFAULT_TIME.hour));
        setMinuteInput(formatTimeValue(DEFAULT_TIME.minute));
      }
      if (newFrequency === "weekly") {
        setWeekdays(DEFAULT_WEEKDAYS);
      } else if (newFrequency === "monthly") {
        setDates(DEFAULT_DATES);
      }
    },
    [isManaged],
  );

  const handleHourInputChange = useCallback((value: string) => {
    // 允许用户自由输入，只过滤非数字字符
    const filtered = value.replace(/[^\d]/g, "");
    setHourInput(filtered);
  }, []);

  const handleMinuteInputChange = useCallback((value: string) => {
    // 允许用户自由输入，只过滤非数字字符
    const filtered = value.replace(/[^\d]/g, "");
    setMinuteInput(filtered);
  }, []);

  const handleHourInputBlur = useCallback(() => {
    // blur 时校验并格式化
    const hour = validateHour(hourInput);
    setTime((prev) => ({ ...prev, hour }));
    setHourInput(formatTimeValue(hour));
  }, [hourInput]);

  const handleMinuteInputBlur = useCallback(() => {
    // blur 时校验并格式化
    const minute = validateMinute(minuteInput);
    setTime((prev) => ({ ...prev, minute }));
    setMinuteInput(formatTimeValue(minute));
  }, [minuteInput]);

  const handleNotificationDelayInputChange = useCallback((value: string) => {
    setNotificationDelayValue(value.replace(/[^\d]/g, ""));
  }, []);

  const toggleWeekday = useCallback((day: number) => {
    setWeekdays((prev) => {
      if (prev.includes(day)) {
        if (prev.length === 1) return prev;
        return prev.filter((d) => d !== day);
      }
      return [...prev, day].sort((a, b) => a - b);
    });
  }, []);

  const toggleDate = useCallback((date: number) => {
    setDates((prev) => {
      if (prev.includes(date)) {
        if (prev.length === 1) return prev;
        return prev.filter((d) => d !== date);
      }
      return [...prev, date].sort((a, b) => a - b);
    });
  }, []);

  const scheduleConfig: ScheduleConfig = useMemo(() => {
    return {
      frequency: frequency === "lastWorkday" ? "monthly" : frequency,
      hour: time.hour,
      minute: time.minute,
      weekdays: frequency === "weekly" ? weekdays : undefined,
      dates:
        frequency === "monthly"
          ? dates
          : frequency === "lastWorkday"
          ? [1]
          : undefined,
    };
  }, [frequency, time, weekdays, dates]);

  const cronExpression =
    frequency === "lastWorkday"
      ? `${formatTimeValue(time.minute)} ${formatTimeValue(time.hour)} LW * *`
      : generateCronExpression(scheduleConfig);
  const calendar = useManagedCalendar(
    managedContext,
    open,
    cronExpression,
    frequency === "lastWorkday",
    showCalendarOptions && runOnNonWorkday,
    settingsLocked,
  );

  const previewText = useMemo(() => {
    try {
      return formatNextRunPreview(scheduleConfig);
    } catch {
      return "";
    }
  }, [scheduleConfig]);

  const isConfirmDisabled = useMemo(() => {
    if (frequency === "weekly" && weekdays.length === 0) return true;
    if (frequency === "monthly" && dates.length === 0) return true;
    return false;
  }, [frequency, weekdays, dates]);

  const showDateWarning = useMemo(() => {
    return frequency === "monthly" && hasDateBoundaryWarning(dates);
  }, [frequency, dates]);

  const handleConfirm = useCallback(async () => {
    if (isConfirmDisabled || (managedContext && !calendar.schedule)) return;

    setLoading(true);
    const identity = JSON.stringify(managedContext);
    const active = () =>
      !managedContext ||
      (live.current &&
        current.current.open &&
        current.current.identity === identity);
    try {
      const parameters: Parameters<ScheduledTaskPopupProps["onConfirm"]> = [
        cronExpression,
        scheduleConfig,
        parseExecutionModelKey(executionModelKey),
        toNotificationDelayMinutes(
          Number.parseInt(notificationDelayValue || "0", 10),
          notificationDelayUnit,
        ),
      ];
      if (managedContext) parameters.push(calendar.schedule!);
      await onConfirm(...parameters);
      if (!active()) return;
      message.success("定时任务创建成功");
      onClose();
      onSuccess?.();
    } catch (error) {
      if (
        active() &&
        !(error instanceof DOMException && error.name === "AbortError")
      )
        message.error(
          managedContext && error instanceof Error
            ? error.message
            : "创建失败，请重试",
        );
    } finally {
      if (live.current) setLoading(false);
    }
  }, [
    executionModelKey,
    isConfirmDisabled,
    scheduleConfig,
    onConfirm,
    message,
    notificationDelayUnit,
    notificationDelayValue,
    onClose,
    onSuccess,
    cronExpression,
    calendar.schedule,
    managedContext,
  ]);

  const handleClose = useCallback(() => {
    if (isManaged && loading) return;
    onClose();
  }, [onClose, isManaged, loading]);

  return (
    <Modal
      open={open}
      maskClosable={!managedContext || !loading}
      keyboard={!managedContext || !loading}
      onCancel={handleClose}
      footer={null}
      width={400}
      centered
      closable={false}
      className={styles.modal}
      styles={{
        body: { padding: 0 },
      }}
    >
      <div className={styles.header}>
        <span className={styles.title}>定时设置</span>
        <button
          className={styles.closeBtn}
          onClick={handleClose}
          type="button"
          disabled={Boolean(managedContext) && loading}
        >
          ✕
        </button>
      </div>

      <div className={styles.body}>
        <fieldset
          className={styles.formFields}
          disabled={Boolean(managedContext) && (settingsLocked || loading)}
        >
          <div className={styles.section}>
            <label className={styles.label}>执行频次</label>
            <div
              className={`${styles.tabs} ${
                isManaged ? styles.managedTabs : ""
              }`}
            >
              <button
                className={`${styles.tab} ${
                  frequency === "daily" ? styles.tabActive : ""
                }`}
                onClick={() => handleFrequencyChange("daily")}
                type="button"
              >
                每天
              </button>
              <button
                className={`${styles.tab} ${
                  frequency === "weekly" ? styles.tabActive : ""
                }`}
                onClick={() => handleFrequencyChange("weekly")}
                type="button"
              >
                每周
              </button>
              <button
                className={`${styles.tab} ${
                  frequency === "monthly" ? styles.tabActive : ""
                }`}
                onClick={() => handleFrequencyChange("monthly")}
                type="button"
              >
                每月
              </button>
              {managedContext && showCalendarOptions && (
                <button
                  className={`${styles.tab} ${
                    frequency === "lastWorkday" ? styles.tabActive : ""
                  }`}
                  onClick={() => handleFrequencyChange("lastWorkday")}
                  type="button"
                >
                  月末工作日
                </button>
              )}
            </div>
          </div>

          {frequency === "weekly" && (
            <div className={styles.section}>
              <label className={styles.label}>选择星期</label>
              <div className={styles.weekdayGrid}>
                {WEEKDAY_LABELS.map((label, index) => {
                  const dayNum = index === 6 ? 0 : index + 1;
                  const isSelected = weekdays.includes(dayNum);
                  return (
                    <button
                      key={label}
                      className={`${styles.weekdayItem} ${
                        isSelected ? styles.weekdaySelected : ""
                      }`}
                      onClick={() => toggleWeekday(dayNum)}
                      type="button"
                    >
                      {label}
                    </button>
                  );
                })}
              </div>
            </div>
          )}

          {frequency === "monthly" && (
            <div className={styles.section}>
              <label className={styles.label}>选择日期</label>
              <div className={styles.dateGrid}>
                {Array.from({ length: 31 }, (_, i) => i + 1).map((date) => {
                  const isSelected = dates.includes(date);
                  return (
                    <button
                      key={date}
                      className={`${styles.dateItem} ${
                        isSelected ? styles.dateSelected : ""
                      }`}
                      onClick={() => toggleDate(date)}
                      type="button"
                    >
                      {date}
                    </button>
                  );
                })}
              </div>
              {showDateWarning && (
                <Tooltip
                  title={
                    isManaged
                      ? "部分月份不存在该日期，这些月份将跳过，不会自动顺延"
                      : "部分月份不存在该日期，将顺延至当月最后一天执行"
                  }
                >
                  <span className={styles.dateWarning}>
                    ⚠️ 29/30/31日可能不存在
                  </span>
                </Tooltip>
              )}
            </div>
          )}

          <div className={styles.section}>
            <label className={styles.label}>选择时间</label>
            <div className={styles.timePicker}>
              <input
                className={styles.timeInput}
                type="text"
                value={hourInput}
                onChange={(e) => handleHourInputChange(e.target.value)}
                onBlur={handleHourInputBlur}
                maxLength={2}
              />
              <span className={styles.timeUnit}>时</span>
              <span className={styles.timeSeparator}>:</span>
              <input
                className={styles.timeInput}
                type="text"
                value={minuteInput}
                onChange={(e) => handleMinuteInputChange(e.target.value)}
                onBlur={handleMinuteInputBlur}
                maxLength={2}
              />
              <span className={styles.timeUnit}>分</span>
            </div>
          </div>

          <div className={styles.section}>
            <label className={styles.label} htmlFor="execution-model-select">
              执行模型
            </label>
            <select
              id="execution-model-select"
              className={styles.modelSelect}
              value={executionModelKey}
              onChange={(event) => setExecutionModelKey(event.target.value)}
              disabled={executionModelLoading}
            >
              <option value={DEFAULT_EXECUTION_MODEL_KEY}>
                {tenantDefaultLabel}
              </option>
              {executionModelOptions.map((option) => (
                <option key={option.key} value={option.key}>
                  {option.label}
                </option>
              ))}
            </select>
          </div>

          <div className={styles.section}>
            <label className={styles.label} htmlFor="notification-delay-value">
              通知延迟
            </label>
            <div className={styles.delayPicker}>
              <input
                id="notification-delay-value"
                aria-label="通知延迟数值"
                className={styles.delayInput}
                type="number"
                min={0}
                value={notificationDelayValue}
                onChange={(event) =>
                  handleNotificationDelayInputChange(event.target.value)
                }
              />
              <select
                aria-label="通知延迟单位"
                className={styles.delayUnitSelect}
                value={notificationDelayUnit}
                onChange={(event) =>
                  setNotificationDelayUnit(
                    event.target.value as NotificationDelayUnit,
                  )
                }
              >
                <option value="minutes">分钟</option>
                <option value="hours">小时</option>
              </select>
            </div>
          </div>

          {managedContext && (
            <div className={styles.section}>
              {showCalendarOptions && (
                <Checkbox
                  checked={runOnNonWorkday}
                  onChange={(event) => setRunOnNonWorkday(event.target.checked)}
                >
                  允许非工作日执行
                </Checkbox>
              )}
              <div className={styles.label} style={{ marginTop: 8 }}>
                排程时区：
                {calendar.timezone || calendar.zoneError || "正在读取…"}
              </div>
            </div>
          )}
        </fieldset>
        {(managedContext || previewText) && (
          <div className={styles.preview}>
            <p className={styles.previewText}>
              📌{" "}
              <span className={styles.previewHighlight}>
                {managedContext ? calendar.preview : previewText}
              </span>
            </p>
          </div>
        )}
      </div>

      <div className={styles.footer}>
        <button
          className={styles.btnCancel}
          onClick={handleClose}
          type="button"
          disabled={loading}
        >
          取消
        </button>
        <button
          className={styles.btnConfirm}
          onClick={handleConfirm}
          type="button"
          disabled={
            isConfirmDisabled ||
            loading ||
            (Boolean(managedContext) && !calendar.schedule)
          }
        >
          {loading
            ? "创建中..."
            : settingsLocked && managedContext
            ? "重试"
            : "确认"}
        </button>
      </div>
    </Modal>
  );
}
