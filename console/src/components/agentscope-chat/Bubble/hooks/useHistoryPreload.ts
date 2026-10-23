import { useCallback, useLayoutEffect, useRef, useState } from "react";

export const HISTORY_PRELOAD_DISTANCE = 240;
const MAX_HISTORY_ADVANCE_STEPS = 3;

interface UseHistoryPreloadOptions {
  scrollElement: HTMLElement | null;
  /** True only when the local window or archive cursor advanced. */
  onNearStart: () => Promise<boolean>;
  onRetry?: () => Promise<boolean>;
  disabled?: boolean;
  resetKey?: string | null;
  isRestoringAnchor?: () => boolean;
  isAnchorRecoveryPending?: () => boolean;
}

function getVisualStart(element: HTMLElement): number {
  return Math.min(0, element.clientHeight - element.scrollHeight);
}

function isNearVisualStart(element: HTMLElement): boolean {
  return (
    element.scrollTop - getVisualStart(element) <= HISTORY_PRELOAD_DISTANCE
  );
}

function nextHistoryFrame(signal: AbortSignal): Promise<boolean> {
  return new Promise((resolve) => {
    if (signal.aborted) return resolve(false);
    const cancel = () => {
      cancelAnimationFrame(frameId);
      resolve(false);
    };
    const frameId = requestAnimationFrame(() => {
      signal.removeEventListener("abort", cancel);
      resolve(true);
    });
    signal.addEventListener("abort", cancel, { once: true });
  });
}

/** Progress is bounded per user action; layout changes never require a down/up gesture. */
export default function useHistoryPreload(options: UseHistoryPreloadOptions) {
  const { scrollElement, resetKey } = options;
  const latest = useRef(options);
  const startRef = useRef<(retrying?: boolean) => void>(() => {});
  const [isAdvancing, setIsAdvancing] = useState(false);

  useLayoutEffect(() => {
    latest.current = options;
  });

  useLayoutEffect(() => {
    setIsAdvancing(false);
    if (!scrollElement) return;
    const controller = new AbortController();
    let requesting = false;
    let readingOlder = false;
    let previousTop = scrollElement.scrollTop;

    const canContinue = () =>
      !controller.signal.aborted &&
      !latest.current.disabled &&
      readingOlder &&
      isNearVisualStart(scrollElement);

    const advance = async (retrying: boolean) => {
      requesting = true;
      readingOlder = true;
      setIsAdvancing(true);
      try {
        for (let step = 0; step < MAX_HISTORY_ADVANCE_STEPS; step += 1) {
          const progressed = await (step === 0 && retrying
            ? latest.current.onRetry!
            : latest.current.onNearStart)();
          if (controller.signal.aborted) return;
          if (!progressed) break;
          // Hold the lock through React's commit and the anchor-restoration frame.
          if (!(await nextHistoryFrame(controller.signal))) return;
          if (!(await nextHistoryFrame(controller.signal))) return;
          while (latest.current.isAnchorRecoveryPending?.()) {
            if (!(await nextHistoryFrame(controller.signal))) return;
          }
          previousTop = scrollElement.scrollTop;
          if (!canContinue()) break;
        }
      } finally {
        if (!controller.signal.aborted) {
          requesting = false;
          setIsAdvancing(false);
        }
      }
    };
    const start = (retrying = false) => {
      if (
        requesting ||
        (retrying ? !latest.current.onRetry : latest.current.disabled)
      )
        return;
      // The caller presents request errors; stop this automatic progression too.
      void advance(retrying).catch(() => {});
    };
    startRef.current = start;

    const handleScroll = () => {
      const top = scrollElement.scrollTop;
      const delta = top - previousTop;
      previousTop = top;
      if (latest.current.isRestoringAnchor?.()) return;
      if (delta > 0) readingOlder = false;
      if (delta < 0) {
        readingOlder = true;
        if (isNearVisualStart(scrollElement)) start();
      }
    };
    const handleWheel = (event: WheelEvent) => {
      if (event.deltaY > 0) readingOlder = false;
    };
    scrollElement.addEventListener("scroll", handleScroll, { passive: true });
    scrollElement.addEventListener("wheel", handleWheel, { passive: true });
    return () => {
      controller.abort();
      startRef.current = () => {};
      scrollElement.removeEventListener("scroll", handleScroll);
      scrollElement.removeEventListener("wheel", handleWheel);
    };
  }, [resetKey, scrollElement]);

  const loadMore = useCallback(() => startRef.current(), []);
  const retry = useCallback(() => startRef.current(true), []);
  return { isAdvancing, loadMore, retry };
}
