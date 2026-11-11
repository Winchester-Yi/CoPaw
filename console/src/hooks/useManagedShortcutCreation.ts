import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { RequestIdentity } from "@/api/requestIdentity";
import { schedulerCronApi } from "@/api/modules/schedulerCron";
import type {
  ManagedCronContext,
  ManagedCronSpec,
  ManagedJobDetail,
  ManagedOperation,
} from "@/api/types/schedulerCron";
import { getCurrentCronContext } from "./useCronContext";

const isPending = (operation: ManagedOperation) =>
  operation.status === "pending" || operation.status === "running";
const abortError = () =>
  new DOMException("创建所属工作区已切换或关闭", "AbortError");

function delay(signal: AbortSignal) {
  return new Promise<void>((resolve, reject) => {
    if (signal.aborted) {
      reject(abortError());
      return;
    }
    const onAbort = () => {
      window.clearTimeout(timer);
      reject(abortError());
    };
    const timer = window.setTimeout(() => {
      signal.removeEventListener("abort", onAbort);
      resolve();
    }, 1000);
    signal.addEventListener("abort", onAbort, { once: true });
  });
}

function sameContext(left: ManagedCronContext, right: ManagedCronContext) {
  return (
    left.actor_id === right.actor_id &&
    left.tenant_id === right.tenant_id &&
    left.source_id === right.source_id &&
    left.agent_id === right.agent_id
  );
}

/** 一个快捷创建意图只使用一份内容和请求号；已受理不等于资源准备成功。 */
export function useManagedShortcutCreation(
  context: ManagedCronContext,
  resource: string,
) {
  const key = JSON.stringify([
    context.actor_id,
    context.tenant_id,
    context.source_id,
    context.agent_id,
    resource,
  ]);
  const attempt = useMemo(
    () => ({
      key,
      request: new RequestIdentity(),
      operation: null as ManagedOperation | null,
      busy: false,
      live: true,
      controller: new AbortController(),
    }),
    [key],
  );
  const latest = useRef(attempt);
  latest.current = attempt;
  const [progress, setProgress] = useState({
    key,
    loading: false,
    locked: false,
    operation: null as ManagedOperation | null,
  });
  useEffect(() => {
    attempt.live = true;
    if (attempt.controller.signal.aborted)
      attempt.controller = new AbortController();
    return () => {
      attempt.live = false;
      attempt.controller.abort();
    };
  }, [attempt]);
  const create = useCallback(
    async (spec: ManagedCronSpec): Promise<ManagedJobDetail> => {
      const active = () =>
        attempt.live &&
        latest.current === attempt &&
        !attempt.controller.signal.aborted &&
        sameContext(context, getCurrentCronContext());
      const guard = () => {
        if (!active()) throw abortError();
      };
      guard();
      if (attempt.busy) throw new Error("正在创建，请等待当前请求结束");
      const requestId = attempt.request.for(spec);
      attempt.busy = true;
      setProgress({
        key,
        loading: true,
        locked: true,
        operation: attempt.operation,
      });
      const publish = (operation: ManagedOperation) => {
        guard();
        attempt.operation = operation;
        setProgress({ key, loading: true, locked: true, operation });
      };
      try {
        let operation = attempt.operation;
        if (!operation)
          operation = (await schedulerCronApi.create(context, spec, requestId))
            .operation;
        else if (operation.status === "failed")
          operation = (
            await schedulerCronApi.retry(
              context,
              operation.job_id,
              operation.operation_id,
            )
          ).operation;
        else if (isPending(operation))
          operation = await schedulerCronApi.operation(
            context,
            operation.job_id,
            operation.operation_id,
          );
        publish(operation);
        for (let count = 0; isPending(operation) && count < 30; count += 1) {
          await delay(attempt.controller.signal);
          guard();
          operation = await schedulerCronApi.operation(
            context,
            operation.job_id,
            operation.operation_id,
          );
          publish(operation);
        }
        if (isPending(operation))
          throw new Error(
            "任务已受理，仍在处理中；请在定时任务（新）查看进度，或重试查询",
          );
        if (operation.status !== "completed")
          throw new Error(
            `任务管理未完成：${
              operation.error_code || operation.status
            }。可重试同一管理操作`,
          );
        const detail = await schedulerCronApi.detail(context, operation.job_id);
        guard();
        if (
          detail.management_owner !== "scheduler" ||
          detail.management_status !== "ready" ||
          detail.agent_id !== context.agent_id ||
          detail.spec.id !== operation.job_id ||
          detail.spec.tenant_id !== context.tenant_id ||
          detail.spec.source_id !== context.source_id
        )
          throw new Error(
            "创建结果尚未就绪或身份不一致，请到定时任务（新）核对进度",
          );
        attempt.request.reset();
        attempt.operation = null;
        setProgress({ key, loading: false, locked: false, operation: null });
        return detail;
      } finally {
        attempt.busy = false;
        if (active())
          setProgress((value) =>
            value.key === key ? { ...value, loading: false } : value,
          );
      }
    },
    [attempt, context, key],
  );
  return {
    create,
    loading: progress.key === key && progress.loading,
    locked: progress.key === key && progress.locked,
    operation: progress.key === key ? progress.operation : null,
  };
}
