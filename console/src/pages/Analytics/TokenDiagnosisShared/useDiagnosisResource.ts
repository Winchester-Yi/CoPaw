import { useCallback, useEffect, useRef, useState } from "react";
import { useIframeStore } from "../../../stores/iframeStore";
import { DEFAULT_SOURCE_ID } from "../../../constants/identity";
import { getUserId } from "../../../utils/identity";

export type DiagnosisResourceState =
  | "idle"
  | "loading"
  | "error"
  | "unavailable"
  | "not_found"
  | "ready";
interface ResourceValue<T> {
  key: string;
  state: DiagnosisResourceState;
  data?: T;
  error?: string;
  status?: number;
}
export function useDiagnosisResource<T>(
  key: string,
  load: (signal: AbortSignal) => Promise<T>,
  enabled = true,
) {
  const source = useIframeStore((store) => store.source) || DEFAULT_SOURCE_ID;
  const user = useIframeStore((store) => store.userId) || getUserId();
  const [revision, setRevision] = useState(0);
  const identity =
    enabled && key ? JSON.stringify([source, user, key, revision]) : "";
  const loader = useRef(load);
  loader.current = load;
  const [value, setValue] = useState<ResourceValue<T>>({
    key: "",
    state: "idle",
  });
  const reload = useCallback(() => setRevision((previous) => previous + 1), []);
  useEffect(() => {
    if (!identity) return;
    const controller = new AbortController();
    setValue({ key: identity, state: "loading" });
    async function fetchResource() {
      try {
        const data = await loader.current(controller.signal);
        if (!controller.signal.aborted)
          setValue({ key: identity, state: "ready", data });
      } catch (error: unknown) {
        if (controller.signal.aborted) return;
        const status =
          error &&
          typeof error === "object" &&
          "status" in error &&
          typeof error.status === "number"
            ? error.status
            : undefined;
        const state =
          status === 503
            ? "unavailable"
            : status === 404
            ? "not_found"
            : "error";
        setValue({
          key: identity,
          state,
          status,
          error:
            status === 503
              ? "Token 诊断数据服务暂不可用"
              : status === 404
              ? "当前作用域未找到证据，可能已过期"
              : error instanceof Error
              ? error.message
              : "诊断数据加载失败",
        });
      }
    }
    void fetchResource();
    return () => controller.abort();
  }, [identity]);
  const current: ResourceValue<T> =
    value.key === identity
      ? value
      : { key: identity, state: identity ? "loading" : "idle" };
  return { ...current, loading: current.state === "loading", reload };
}
