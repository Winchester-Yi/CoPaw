import { describe, expect, it, vi } from "vitest";
import {
  discardStaleHtmlAnnotationSubmission,
  prepareHtmlAnnotationExecutionMode,
  prepareHtmlAnnotationSubmit,
} from "./submission";
import type { HtmlAnnotationBundle } from "./types";

const bundle: HtmlAnnotationBundle = {
  token: "token-1",
  chatKey: "chat-1",
  sourceKey: "report.html",
  fileName: "季度报告.html",
  canonicalHtml: "<!doctype html><script>window.ok=true</script>",
  sourceSha256: "a".repeat(64),
  annotations: [
    {
      id: "ann-1",
      comment: "修改标题",
      target: {
        runtime_generated: false,
        stable_attributes: { id: "title" },
      },
    },
  ],
};

describe("prepareHtmlAnnotationSubmit", () => {
  it("removes restored annotation metadata and its synthetic attachment after explicit discard", () => {
    const result = discardStaleHtmlAnnotationSubmission({
      query: "改成蓝色",
      fileList: [
        {
          uid: "html-annotation-token-1",
          name: "report.annotation-source.html",
          type: "text/html",
        },
        { uid: "manual-1", name: "notes.txt", type: "text/plain" },
      ],
      biz_params: {
        mode: "normal",
        document_annotations: { schema_version: 1 },
        request_source: "composer",
      },
    });

    expect(result.fileList).toEqual([
      { uid: "manual-1", name: "notes.txt", type: "text/plain" },
    ]);
    expect(result.biz_params).toEqual({
      mode: "normal",
      request_source: "composer",
    });
  });

  it("persistently exits Plan Mode and forces the annotated turn to normal mode", async () => {
    const persistPlanMode = vi.fn(async () => {});

    const result = await prepareHtmlAnnotationExecutionMode(
      {
        query: "应用批注",
        fileList: [],
        biz_params: { mode: "plan", request_source: "composer" },
      },
      {
        planModeEnabled: true,
        persistPlanMode,
        goalModeEnabled: false,
        setGoalModeEnabled: vi.fn(),
      },
    );

    expect(persistPlanMode).toHaveBeenCalledOnce();
    expect(persistPlanMode).toHaveBeenCalledWith(false);
    expect(result.biz_params).toEqual({
      mode: "normal",
      request_source: "composer",
      goal_mode_enabled: false,
    });
  });

  it("stops annotation preparation when exiting Plan Mode fails", async () => {
    const persistPlanMode = vi.fn(async () => {
      throw new Error("persist failed");
    });

    await expect(
      prepareHtmlAnnotationExecutionMode(
        { query: "应用批注", fileList: [] },
        {
          planModeEnabled: true,
          persistPlanMode,
          goalModeEnabled: false,
          setGoalModeEnabled: vi.fn(),
        },
      ),
    ).rejects.toThrow("persist failed");
    expect(persistPlanMode).toHaveBeenCalledWith(false);
  });

  it("disables Goal Mode for the annotated turn", async () => {
    const setGoalModeEnabled = vi.fn();

    const result = await prepareHtmlAnnotationExecutionMode(
      {
        query: "应用批注",
        fileList: [],
        biz_params: { goal_mode_enabled: true },
      },
      {
        planModeEnabled: false,
        persistPlanMode: vi.fn(async () => {}),
        goalModeEnabled: true,
        setGoalModeEnabled,
      },
    );

    expect(setGoalModeEnabled).toHaveBeenCalledWith(false);
    expect(result.biz_params).toMatchObject({
      mode: "normal",
      goal_mode_enabled: false,
    });
  });

  it("uploads canonical HTML and adds a structured top-level request field", async () => {
    const uploadFile = vi.fn(async (file: File) => {
      void file;
      return { url: "media/source.html" };
    });

    const result = await prepareHtmlAnnotationSubmit(
      { query: "", fileList: [], biz_params: { mode: "normal" } },
      bundle,
      {
        uploadFile,
        filePreviewUrl: (url) => `/files/preview/${url}`,
      },
    );

    expect(uploadFile).toHaveBeenCalledOnce();
    const uploaded = uploadFile.mock.calls[0][0];
    expect(await uploaded.text()).toBe(bundle.canonicalHtml);
    expect(result.query).toBe("请根据 1 条页面批注生成修改后的 HTML。");
    expect(result.fileList).toHaveLength(1);
    expect(result.biz_params?.mode).toBe("normal");
    expect(result.biz_params?.document_annotations).toMatchObject({
      schema_version: 1,
      source: {
        attachment_url: "/files/preview/media/source.html",
        sha256: bundle.sourceSha256,
      },
      output: {
        mode: "new_file",
        preserve_original: true,
      },
      annotations: bundle.annotations,
    });
  });

  it("preserves additional visible text", async () => {
    const result = await prepareHtmlAnnotationSubmit(
      { query: "另外请保持配色", fileList: [] },
      bundle,
      {
        uploadFile: async () => ({ url: "media/source.html" }),
        filePreviewUrl: (url) => url,
      },
    );

    expect(result.query).toBe("另外请保持配色");
  });

  it("rejects more than 20 annotations before uploading the source", async () => {
    const uploadFile = vi.fn(async () => ({ url: "media/source.html" }));

    await expect(
      prepareHtmlAnnotationSubmit(
        { query: "", fileList: [] },
        {
          ...bundle,
          annotations: Array.from({ length: 21 }, (_, index) => ({
            ...bundle.annotations[0],
            id: `ann-${index + 1}`,
          })),
        },
        {
          uploadFile,
          filePreviewUrl: (url) => url,
        },
      ),
    ).rejects.toThrow("单次最多添加 20 条批注");
    expect(uploadFile).not.toHaveBeenCalled();
  });

  it("rejects an oversized structured bundle before uploading the source", async () => {
    const uploadFile = vi.fn(async () => ({ url: "media/source.html" }));

    await expect(
      prepareHtmlAnnotationSubmit(
        { query: "", fileList: [] },
        {
          ...bundle,
          annotations: Array.from({ length: 20 }, (_, index) => ({
            ...bundle.annotations[0],
            id: `ann-${index + 1}`,
            target: {
              ...bundle.annotations[0].target,
              rendered_html: "渲染内容".repeat(900),
              ancestor_html: "祖先内容".repeat(900),
            },
          })),
        },
        {
          uploadFile,
          filePreviewUrl: (url) => url,
        },
      ),
    ).rejects.toThrow("批注内容过大");
    expect(uploadFile).not.toHaveBeenCalled();
  });
});
