import { useEffect, useRef, useState } from "react";

export function useResource<T>(
  key: string,
  load: (signal: AbortSignal) => Promise<T>,
) {
  const loader = useRef(load);
  loader.current = load;
  const [value, setValue] = useState<{
    key: string;
    data?: T;
    error?: string;
    loading: boolean;
  }>({ key: "", loading: false });
  useEffect(() => {
    if (!key) return;
    const controller = new AbortController();
    setValue({ key, loading: true });
    loader.current(controller.signal).then(
      (data) => {
        if (!controller.signal.aborted) setValue({ key, data, loading: false });
      },
      (error: unknown) => {
        if (!controller.signal.aborted)
          setValue({
            key,
            error: error instanceof Error ? error.message : "加载失败",
            loading: false,
          });
      },
    );
    return () => controller.abort();
  }, [key]);
  return value.key === key
    ? value
    : { key, loading: Boolean(key), data: undefined, error: undefined };
}
