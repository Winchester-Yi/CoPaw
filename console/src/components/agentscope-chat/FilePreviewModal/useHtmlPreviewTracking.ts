import { useEffect, useRef } from "react";
import {
  attachHtmlPreviewClickTracker,
  attachHtmlPreviewExposureTracker,
  attachHtmlPreviewLoadTracker,
  type HtmlPreviewClickMetadata,
  type HtmlPreviewClickReporter,
  type HtmlPreviewListSnapshotReporter,
  type NestedHtmlPreviewRequest,
} from "./htmlPreviewClickTracking";

export interface HtmlPreviewClickOptions {
  reporter: HtmlPreviewClickReporter;
  listSnapshotReporter?: HtmlPreviewListSnapshotReporter;
  onOpenNestedPreview?: (preview: NestedHtmlPreviewRequest) => void;
  getTemplateName?: (templateId: number) => string | undefined;
}

export interface HtmlPreviewExposureOptions {
  reporter: HtmlPreviewClickReporter;
}

export interface UseHtmlPreviewTrackingOptions {
  click?: HtmlPreviewClickOptions | null;
  exposure?: HtmlPreviewExposureOptions | null;
  load?: HtmlPreviewClickReporter | null;
  metaData: HtmlPreviewClickMetadata;
}

function attachAllTrackers(
  iframe: HTMLIFrameElement,
  options: UseHtmlPreviewTrackingOptions,
  // loadFlagRef: React.MutableRefObject<boolean>,
): { cleanupClick: (() => void) | null; cleanupExposure: (() => void) | null } {
  let cleanupClick: (() => void) | null = null;
  let cleanupExposure: (() => void) | null = null;

  try {
    if (options.click) {
      cleanupClick = attachHtmlPreviewClickTracker({
        iframe,
        ...options.click,
        metadata: options.metaData,
      });
    }

    if (options.exposure) {
      cleanupExposure = attachHtmlPreviewExposureTracker({
        iframe,
        metadata: options.metaData,
        reporter: options.exposure.reporter,
      });
    }
    if (options.load && options.metaData.fileName && options.metaData.templateId) {
      attachHtmlPreviewLoadTracker({
        iframe,
        metadata: options.metaData,
        reporter: options.load,
      });
    }
  } catch (err) {
    console.warn("Failed to attach HTML preview trackers:", err);
  }

  return { cleanupClick, cleanupExposure };
}

export function useIframeHtmlPreviewTracking(
  iframeRef: React.RefObject<HTMLIFrameElement | null>,
  options: UseHtmlPreviewTrackingOptions,
  deps: unknown[] = [],
  interactionKey?: unknown,
) {
  const cleanupClickRef = useRef<(() => void) | null>(null);
  const cleanupExposureRef = useRef<(() => void) | null>(null);
  // const loadFlagRef = useRef(false);
  const optionsRef = useRef(options);
  const interactionEffectMountedRef = useRef(false);
  optionsRef.current = options;

  const doAttach = () => {
    cleanupClickRef.current?.();
    cleanupClickRef.current = null;
    cleanupExposureRef.current?.();
    cleanupExposureRef.current = null;

    const iframe = iframeRef.current;
    if (!iframe) return;

    const { cleanupClick, cleanupExposure } = attachAllTrackers(
      iframe,
      optionsRef.current,
      // loadFlagRef,
    );
    cleanupClickRef.current = cleanupClick;
    cleanupExposureRef.current = cleanupExposure;
  };

  // deps 变化时重新 attach（如开关状态变化）
  useEffect(() => {
    doAttach();

    return () => {
      cleanupClickRef.current?.();
      cleanupClickRef.current = null;
      cleanupExposureRef.current?.();
      cleanupExposureRef.current = null;
    };
  }, deps);

  useEffect(() => {
    if (!interactionEffectMountedRef.current) {
      interactionEffectMountedRef.current = true;
      return;
    }
    cleanupClickRef.current?.();
    cleanupClickRef.current = null;
    const iframe = iframeRef.current;
    const clickOptions = optionsRef.current.click;
    if (iframe && clickOptions) {
      try {
        cleanupClickRef.current = attachHtmlPreviewClickTracker({
          iframe,
          ...clickOptions,
          metadata: optionsRef.current.metaData,
        });
      } catch (err) {
        console.warn("Failed to attach HTML preview click tracker:", err);
      }
    }
    return () => {
      cleanupClickRef.current?.();
      cleanupClickRef.current = null;
    };
  }, [iframeRef, interactionKey]);

  // 返回 reattach 函数，由外部 handleIframeLoad 调用
  const cleanup = () => {
    cleanupClickRef.current?.();
    cleanupClickRef.current = null;
    cleanupExposureRef.current?.();
    cleanupExposureRef.current = null;
  };

  return { cleanup, reattach: doAttach };
}
