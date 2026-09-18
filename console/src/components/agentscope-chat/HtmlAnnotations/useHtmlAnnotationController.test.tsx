import { createRef } from "react";
import { act, renderHook, waitFor } from "@testing-library/react";
import type { PropsWithChildren } from "react";
import { describe, expect, it, vi } from "vitest";
import { HtmlAnnotationProvider, useHtmlAnnotations } from "./context";
import { computeHtmlSha256 } from "./domEvidence";
import type { HtmlAnnotationBundle } from "./types";
import {
  resolveTargetElement,
  useHtmlAnnotationController,
} from "./useHtmlAnnotationController";

const iframeRef = createRef<HTMLIFrameElement>();

describe("useHtmlAnnotationController", () => {
  it("does not move a structural marker to a different element after insertion", () => {
    const document = new DOMParser().parseFromString(
      "<main><button>第一项</button><button>目标项</button></main>",
      "text/html",
    );
    const target = {
      runtime_generated: false,
      tag_name: "button",
      selector: "main > button:nth-of-type(2)",
      text_quote: { exact: "目标项" },
      rendered_html: "<button>目标项</button>",
    };

    expect(resolveTargetElement(target, document)?.textContent).toBe("目标项");
    const inserted = document.createElement("button");
    inserted.textContent = "新增项";
    document.querySelector("main")?.prepend(inserted);

    expect(resolveTargetElement(target, document)).toBeNull();
  });

  it("keeps a marker on its stable id after sibling insertion", () => {
    const document = new DOMParser().parseFromString(
      '<main><button>第一项</button><button id="target">目标项</button></main>',
      "text/html",
    );
    const target = {
      runtime_generated: false,
      tag_name: "button",
      stable_attributes: { id: "target" },
      selector: "#target",
      text_quote: { exact: "目标项" },
    };
    const inserted = document.createElement("button");
    inserted.textContent = "新增项";
    document.querySelector("main")?.prepend(inserted);

    expect(resolveTargetElement(target, document)?.id).toBe("target");
  });

  it("invalidates a staged bundle when the dynamic HTML source changes", async () => {
    const firstHtml = "<html><body><main>第一版</main></body></html>";
    const firstDigest = await computeHtmlSha256(firstHtml);
    const wrapper = ({ children }: PropsWithChildren) => (
      <HtmlAnnotationProvider activeChatKey="chat-a" composerAvailable>
        {children}
      </HtmlAnnotationProvider>
    );
    const { result, rerender } = renderHook(
      ({ canonicalHtml }) => {
        const context = useHtmlAnnotations();
        const controller = useHtmlAnnotationController({
          iframeRef,
          enabled: true,
          canonicalHtml,
          sourceKey: "report.html",
          fileName: "report.html",
          loadKey: 0,
        });
        return { context, controller };
      },
      { initialProps: { canonicalHtml: firstHtml }, wrapper },
    );

    await waitFor(() =>
      expect(result.current.controller.sourceSha256).toBe(firstDigest),
    );
    const stagedBundle: HtmlAnnotationBundle = {
      token: "bundle-a",
      chatKey: "chat-a",
      sourceKey: "report.html",
      fileName: "report.html",
      canonicalHtml: firstHtml,
      sourceSha256: firstDigest,
      annotations: [
        {
          id: "ann-1",
          comment: "修改标题",
          target: { runtime_generated: false, selector: "main" },
        },
      ],
    };
    act(() => result.current.context.stageBundle(stagedBundle));
    expect(result.current.context.pendingBundle).toEqual(stagedBundle);

    rerender({
      canonicalHtml: "<html><body><main>第二版</main></body></html>",
    });

    await waitFor(() =>
      expect(result.current.context.pendingBundle).toBeNull(),
    );
    expect(result.current.controller.sourceConflict).toBe(true);
  });

  it("does not calculate a source digest when annotation is disabled", async () => {
    const { result } = renderHook(() =>
      useHtmlAnnotationController({
        iframeRef,
        enabled: false,
        canonicalHtml: "<html><body>普通预览</body></html>",
        sourceKey: "report.html",
        fileName: "report.html",
        loadKey: 0,
      }),
    );

    await act(async () => Promise.resolve());
    expect(result.current.sourceSha256).toBe("");
  });

  it("reports an unavailable digest without rejecting outside the hook", async () => {
    vi.stubGlobal("crypto", {} as Crypto);
    try {
      const { result } = renderHook(() =>
        useHtmlAnnotationController({
          iframeRef,
          enabled: true,
          canonicalHtml: "<html><body>需要批注</body></html>",
          sourceKey: "report.html",
          fileName: "report.html",
          loadKey: 0,
        }),
      );

      await waitFor(() =>
        expect(result.current.unsupportedReason).toBe(
          "当前环境无法校验 HTML 源码，暂时无法完成批注。",
        ),
      );
      expect(result.current.sourceSha256).toBe("");
    } finally {
      vi.unstubAllGlobals();
    }
  });
});
