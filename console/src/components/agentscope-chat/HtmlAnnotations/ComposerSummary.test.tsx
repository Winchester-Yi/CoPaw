import { fireEvent, render, screen } from "@testing-library/react";
import { useEffect } from "react";
import { describe, expect, it } from "vitest";
import { HtmlAnnotationProvider, useHtmlAnnotations } from "./context";
import HtmlAnnotationComposerSummary from "./ComposerSummary";

function StageBundle() {
  const { stageBundle } = useHtmlAnnotations();
  useEffect(() => {
    stageBundle({
      token: "token",
      chatKey: "chat-1",
      sourceKey: "source",
      fileName: "一个非常长的季度经营分析报告文件名称.html",
      canonicalHtml: "<html></html>",
      sourceSha256: "a".repeat(64),
      annotations: [
        {
          id: "1",
          comment: "a",
          target: { runtime_generated: false, selector: "h1" },
        },
        {
          id: "2",
          comment: "b",
          target: { runtime_generated: false, selector: "p" },
        },
      ],
    });
  }, [stageBundle]);
  return null;
}

describe("HtmlAnnotationComposerSummary", () => {
  it("shows one source-scoped row and supports explicit removal", async () => {
    render(
      <HtmlAnnotationProvider activeChatKey="chat-1" composerAvailable>
        <StageBundle />
        <HtmlAnnotationComposerSummary />
      </HtmlAnnotationProvider>,
    );

    expect(await screen.findByText("2 条批注")).toBeVisible();
    expect(screen.getByTitle(/季度经营分析报告/)).toBeVisible();
    fireEvent.click(screen.getByRole("button", { name: "移除页面批注" }));
    expect(screen.queryByText("2 条批注")).toBeNull();
  });
});
