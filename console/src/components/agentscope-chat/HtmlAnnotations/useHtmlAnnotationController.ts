import {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
  type RefObject,
} from "react";
import {
  buildAnnotationTarget,
  computeHtmlSha256,
  resolveMeaningfulElement,
} from "./domEvidence";
import { useHtmlAnnotations } from "./context";
import {
  HTML_ANNOTATION_MAX_EXCERPT_CHARS,
  HTML_ANNOTATION_MAX_COUNT,
  HTML_ANNOTATION_MAX_TEXT_QUOTE_CHARS,
  type HtmlAnnotation,
  type HtmlAnnotationTarget,
} from "./types";

export interface AnnotationRect {
  left: number;
  top: number;
  width: number;
  height: number;
}

interface DraftAnnotation extends HtmlAnnotation {
  rect: AnnotationRect | null;
}

interface PendingTarget {
  target: HtmlAnnotationTarget;
  rect: AnnotationRect | null;
  editId?: string;
}

const rectFor = (element: Element): AnnotationRect => {
  const rect = element.getBoundingClientRect();
  return {
    left: rect.left,
    top: rect.top,
    width: rect.width,
    height: rect.height,
  };
};

const sameRect = (left: AnnotationRect | null, right: AnnotationRect | null) =>
  left === right ||
  (Boolean(left) &&
    Boolean(right) &&
    left!.left === right!.left &&
    left!.top === right!.top &&
    left!.width === right!.width &&
    left!.height === right!.height);

export const resolveTargetElement = (
  target: HtmlAnnotationTarget,
  liveDocument: Document,
): Element | null => {
  const matchesTarget = (element: Element | null): element is Element => {
    if (!element) return false;
    if (
      target.tag_name &&
      element.tagName.toLowerCase() !== target.tag_name.toLowerCase()
    ) {
      return false;
    }
    const attributes = Object.entries(target.stable_attributes || {});
    if (
      attributes.length > 0 &&
      attributes.some(([name, value]) => element.getAttribute(name) !== value)
    ) {
      return false;
    }
    if (attributes.length > 0) return true;
    const expectedText = target.text_quote?.exact;
    if (expectedText) {
      const actualText = (element.textContent || "")
        .replace(/\s+/g, " ")
        .trim();
      return expectedText.length === HTML_ANNOTATION_MAX_TEXT_QUOTE_CHARS
        ? actualText.startsWith(expectedText)
        : actualText === expectedText;
    }
    const expectedHtml = target.rendered_html;
    if (!expectedHtml) return false;
    return expectedHtml.length === HTML_ANNOTATION_MAX_EXCERPT_CHARS
      ? element.outerHTML.startsWith(expectedHtml)
      : element.outerHTML === expectedHtml;
  };

  const id = target.stable_attributes?.id;
  const idMatch = id ? liveDocument.getElementById(id) : null;
  if (matchesTarget(idMatch)) return idMatch;
  if (target.selector) {
    try {
      const match = liveDocument.querySelector(target.selector);
      if (matchesTarget(match)) return match;
    } catch {
      return null;
    }
  }
  return null;
};

export function useHtmlAnnotationController(options: {
  iframeRef: RefObject<HTMLIFrameElement>;
  enabled: boolean;
  canonicalHtml: string | null;
  sourceKey: string;
  fileName: string;
  loadKey: number;
}) {
  const annotationContext = useHtmlAnnotations();
  const pendingBundle = annotationContext.pendingBundle;
  const removeBundle = annotationContext.removeBundle;
  const [annotationMode, setAnnotationMode] = useState(false);
  const [sourceSha256, setSourceSha256] = useState("");
  const [annotations, setAnnotations] = useState<DraftAnnotation[]>([]);
  const [hoverRect, setHoverRect] = useState<AnnotationRect | null>(null);
  const [pendingTarget, setPendingTarget] = useState<PendingTarget | null>(
    null,
  );
  const [comment, setComment] = useState("");
  const [sourceConflict, setSourceConflict] = useState(false);
  const [unsupportedReason, setUnsupportedReason] = useState("");
  const digestRef = useRef("");

  const exitAnnotationMode = useCallback(() => {
    setPendingTarget(null);
    setHoverRect(null);
    setComment("");
    setAnnotationMode(false);
  }, []);

  useEffect(() => {
    let cancelled = false;
    if (!options.enabled || !options.canonicalHtml) {
      setSourceSha256("");
      return;
    }
    void computeHtmlSha256(options.canonicalHtml)
      .then((digest) => {
        if (cancelled) return;
        if (digestRef.current && digestRef.current !== digest) {
          if (
            pendingBundle?.sourceKey === options.sourceKey &&
            pendingBundle.sourceSha256 === digestRef.current
          ) {
            removeBundle();
          }
          setAnnotations([]);
          setPendingTarget(null);
          setAnnotationMode(false);
          setSourceConflict(true);
        }
        digestRef.current = digest;
        setSourceSha256(digest);
      })
      .catch(() => {
        if (cancelled) return;
        setSourceSha256("");
        setUnsupportedReason("当前环境无法校验 HTML 源码，暂时无法完成批注。");
      });
    return () => {
      cancelled = true;
    };
  }, [
    options.canonicalHtml,
    options.enabled,
    options.sourceKey,
    pendingBundle,
    removeBundle,
  ]);

  useEffect(() => {
    if (!annotationMode || !options.enabled) return;
    const iframe = options.iframeRef.current;
    let liveDocument: Document | null | undefined;
    try {
      liveDocument = iframe?.contentDocument;
    } catch {
      setUnsupportedReason("该页面来自不可访问的跨域内容，暂时无法标注。");
      setAnnotationMode(false);
      return;
    }
    if (!liveDocument || !options.canonicalHtml) return;

    const refreshAnnotationRects = () => {
      setAnnotations((current) => {
        let changed = false;
        const next = current.map((annotation) => {
          const element = resolveTargetElement(
            annotation.target,
            liveDocument!,
          );
          const rect = element ? rectFor(element) : null;
          if (sameRect(annotation.rect, rect)) return annotation;
          changed = true;
          return { ...annotation, rect };
        });
        return changed ? next : current;
      });
    };
    const view = liveDocument.defaultView;
    let scheduledFrame: number | null = null;
    let scheduledTimer: ReturnType<typeof setTimeout> | null = null;
    const scheduleRectRefresh = () => {
      if (scheduledFrame !== null || scheduledTimer !== null) return;
      if (view?.requestAnimationFrame) {
        scheduledFrame = view.requestAnimationFrame(() => {
          scheduledFrame = null;
          refreshAnnotationRects();
        });
      } else {
        scheduledTimer = setTimeout(() => {
          scheduledTimer = null;
          refreshAnnotationRects();
        }, 0);
      }
    };

    const resolveEventElement = (event: Event) => {
      const raw = event.target;
      if (!raw || (raw as Node).nodeType !== Node.ELEMENT_NODE) return null;
      return resolveMeaningfulElement(raw as Element);
    };
    const onHover = (event: Event) => {
      const element = resolveEventElement(event);
      setHoverRect(element ? rectFor(element) : null);
    };
    const onLeave = () => setHoverRect(null);
    const onSelect = (event: Event) => {
      const raw = event.target;
      if (!raw || (raw as Node).nodeType !== Node.ELEMENT_NODE) return;
      const rawElement = raw as Element;
      event.preventDefault();
      event.stopPropagation();
      if ("stopImmediatePropagation" in event) event.stopImmediatePropagation();
      if (["canvas", "iframe"].includes(rawElement.tagName.toLowerCase())) {
        setUnsupportedReason("该位置没有可寻址的 DOM 内容，无法创建可靠批注。");
        return;
      }
      const element = resolveMeaningfulElement(rawElement);
      setUnsupportedReason("");
      setPendingTarget({
        target: buildAnnotationTarget(element, {
          canonicalHtml: options.canonicalHtml!,
          liveDocument,
        }),
        rect: rectFor(element),
      });
      setComment("");
    };
    const onPreActivation = (event: Event) => {
      event.stopPropagation();
      if ("stopImmediatePropagation" in event) event.stopImmediatePropagation();
    };
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        event.preventDefault();
        event.stopPropagation();
        exitAnnotationMode();
      }
    };
    const preActivationEvents = [
      "pointerdown",
      "pointerup",
      "mousedown",
      "mouseup",
      "touchstart",
      "touchend",
    ] as const;
    liveDocument.addEventListener("mouseover", onHover, true);
    liveDocument.addEventListener("mouseout", onLeave, true);
    preActivationEvents.forEach((eventName) => {
      liveDocument.addEventListener(eventName, onPreActivation, true);
    });
    liveDocument.addEventListener("click", onSelect, true);
    liveDocument.addEventListener("keydown", onKeyDown, true);
    liveDocument.addEventListener("scroll", scheduleRectRefresh, true);
    view?.addEventListener("resize", scheduleRectRefresh);
    const mutationObserver = view?.MutationObserver
      ? new view.MutationObserver(scheduleRectRefresh)
      : null;
    if (liveDocument.documentElement) {
      mutationObserver?.observe(liveDocument.documentElement, {
        attributes: true,
        childList: true,
        subtree: true,
      });
    }
    refreshAnnotationRects();
    return () => {
      liveDocument.removeEventListener("mouseover", onHover, true);
      liveDocument.removeEventListener("mouseout", onLeave, true);
      preActivationEvents.forEach((eventName) => {
        liveDocument.removeEventListener(eventName, onPreActivation, true);
      });
      liveDocument.removeEventListener("click", onSelect, true);
      liveDocument.removeEventListener("keydown", onKeyDown, true);
      liveDocument.removeEventListener("scroll", scheduleRectRefresh, true);
      view?.removeEventListener("resize", scheduleRectRefresh);
      mutationObserver?.disconnect();
      if (scheduledFrame !== null) view?.cancelAnimationFrame(scheduledFrame);
      if (scheduledTimer !== null) clearTimeout(scheduledTimer);
      setHoverRect(null);
    };
  }, [
    annotationMode,
    options.canonicalHtml,
    options.enabled,
    options.iframeRef,
    options.loadKey,
    exitAnnotationMode,
  ]);

  const saveComment = useCallback(() => {
    const value = comment.trim();
    if (!pendingTarget || !value) return;
    if (
      !pendingTarget.editId &&
      annotations.length >= HTML_ANNOTATION_MAX_COUNT
    ) {
      setUnsupportedReason(
        `单次最多添加 ${HTML_ANNOTATION_MAX_COUNT} 条批注，请发送后继续批注。`,
      );
      return;
    }
    setUnsupportedReason("");
    setAnnotations((current) => {
      if (pendingTarget.editId) {
        return current.map((annotation) =>
          annotation.id === pendingTarget.editId
            ? { ...annotation, comment: value }
            : annotation,
        );
      }
      return [
        ...current,
        {
          id: `ann-${Date.now()}-${current.length + 1}`,
          comment: value,
          target: pendingTarget.target,
          rect: pendingTarget.rect,
        },
      ];
    });
    setPendingTarget(null);
    setComment("");
  }, [annotations.length, comment, pendingTarget]);

  const editAnnotation = useCallback((annotation: DraftAnnotation) => {
    setPendingTarget({
      target: annotation.target,
      rect: annotation.rect,
      editId: annotation.id,
    });
    setComment(annotation.comment);
  }, []);

  const deleteAnnotation = useCallback((id: string) => {
    setAnnotations((current) => current.filter((item) => item.id !== id));
  }, []);

  const completeAnnotations = useCallback(() => {
    if (
      !options.canonicalHtml ||
      !sourceSha256 ||
      annotations.length === 0 ||
      !annotationContext.activeChatKey
    ) {
      return false;
    }
    annotationContext.stageBundle({
      token: `${annotationContext.activeChatKey}-${sourceSha256}-${Date.now()}`,
      chatKey: annotationContext.activeChatKey,
      sourceKey: options.sourceKey,
      fileName: options.fileName,
      canonicalHtml: options.canonicalHtml,
      sourceSha256,
      annotations: annotations.map((annotation) => ({
        id: annotation.id,
        comment: annotation.comment,
        target: annotation.target,
      })),
    });
    setSourceConflict(false);
    setAnnotationMode(false);
    setPendingTarget(null);
    return true;
  }, [annotationContext, annotations, options, sourceSha256]);

  return useMemo(
    () => ({
      annotationMode,
      annotations,
      comment,
      hoverRect,
      pendingTarget,
      sourceSha256,
      sourceConflict,
      unsupportedReason,
      setAnnotationMode,
      setComment,
      saveComment,
      editAnnotation,
      deleteAnnotation,
      completeAnnotations,
      exitAnnotationMode,
      cancelComment: () => {
        setPendingTarget(null);
        setComment("");
      },
    }),
    [
      annotationMode,
      annotations,
      comment,
      completeAnnotations,
      deleteAnnotation,
      editAnnotation,
      exitAnnotationMode,
      hoverRect,
      pendingTarget,
      saveComment,
      sourceSha256,
      sourceConflict,
      unsupportedReason,
    ],
  );
}
