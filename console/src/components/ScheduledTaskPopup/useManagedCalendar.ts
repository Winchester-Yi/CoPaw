import { useEffect, useMemo, useRef, useState } from "react";
import type {
  ManagedCronContext,
  ManagedCronSchedule,
} from "@/api/types/schedulerCron";
import { cronCalendarApi } from "@/api/modules/cronCalendar";
import { userTimezoneApi } from "@/api/modules/userTimezone";

/** 只有新模式读取日历；旧弹窗仍使用原来的展示与回调约定。 */
export function useManagedCalendar(
  context: ManagedCronContext | undefined,
  open: boolean,
  cron: string,
  lastWorkday: boolean,
  runOnNonWorkday: boolean,
  locked = false,
) {
  const actor = context?.actor_id;
  const tenant = context?.tenant_id;
  const source = context?.source_id;
  const agent = context?.agent_id;
  const snapshot = useMemo(
    () =>
      actor && tenant && source && agent
        ? {
            actor_id: actor,
            tenant_id: tenant,
            source_id: source,
            agent_id: agent,
          }
        : undefined,
    [actor, tenant, source, agent],
  );
  const identity = JSON.stringify(snapshot);
  const [zone, setZone] = useState<{
    key?: string;
    value?: string;
    error?: string;
  }>({});
  const [preview, setPreview] = useState<{
    key?: string;
    text?: string;
    error?: string;
  }>({});
  const savedZone = useRef(zone);
  savedZone.current = zone;
  const retryLocked = useRef(locked);
  retryLocked.current = locked;
  useEffect(() => {
    if (!snapshot || !open) return;
    // 未确认创建结果时，重开弹窗也不能读取新时区而改变重试内容。
    if (
      retryLocked.current &&
      savedZone.current.key === identity &&
      savedZone.current.value
    )
      return;
    let alive = true;
    setZone({ key: identity });
    void userTimezoneApi
      .getUserTimezone()
      .then((response) => {
        // 不以浏览器默认时区替代配置，未知时区明确拒绝创建。
        if (typeof response.timezone !== "string" || !response.timezone)
          throw new Error("missing timezone");
        new Intl.DateTimeFormat("zh-CN", { timeZone: response.timezone });
        if (alive) setZone({ key: identity, value: response.timezone });
      })
      .catch(() => {
        if (alive)
          setZone({
            key: identity,
            error: "排程时区读取失败，请关闭后重新打开",
          });
      });
    return () => {
      alive = false;
    };
  }, [identity, open, snapshot]);
  const timezone = lastWorkday
    ? "Asia/Shanghai"
    : zone.key === identity
    ? zone.value
    : undefined;
  const schedule = useMemo<ManagedCronSchedule | null>(
    () =>
      snapshot && open && timezone
        ? { type: "cron", cron, timezone, run_on_non_workday: runOnNonWorkday }
        : null,
    [snapshot, open, timezone, cron, runOnNonWorkday],
  );
  const key = JSON.stringify([identity, schedule]);
  useEffect(() => {
    if (!snapshot || !schedule) return;
    let alive = true;
    const controller = new AbortController();
    setPreview({ key });
    const timer = window.setTimeout(() => {
      void cronCalendarApi
        .preview(snapshot, schedule, controller.signal)
        .then((response) => {
          if (!Array.isArray(response.next_run_times))
            throw new Error("invalid preview");
          const first = response.next_run_times[0];
          const formatter = new Intl.DateTimeFormat("zh-CN", {
            timeZone: schedule.timezone,
            year: "numeric",
            month: "2-digit",
            day: "2-digit",
            hour: "2-digit",
            minute: "2-digit",
            hourCycle: "h23",
          });
          const label = first
            ? formatter.format(new Date(first))
            : "预览范围内暂无可执行时间";
          const fallback =
            (response.preview_source ?? response.source) === "fallback"
              ? "（含周末兜底判定）"
              : "";
          if (alive) setPreview({ key, text: `下次预计：${label}${fallback}` });
        })
        .catch(() => {
          if (alive)
            setPreview({
              key,
              error: "预览暂不可用，实际触发以服务端日历判定为准",
            });
        });
    }, 200);
    return () => {
      alive = false;
      controller.abort();
      window.clearTimeout(timer);
    };
  }, [key, snapshot, schedule]);
  return {
    schedule,
    timezone,
    zoneError: lastWorkday
      ? undefined
      : zone.key === identity
      ? zone.error
      : undefined,
    preview:
      preview.key === key
        ? preview.text ?? preview.error ?? "正在获取服务端预览…"
        : "正在获取服务端预览…",
  };
}
