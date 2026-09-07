import {
  act,
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { MemoryRouter, Route, Routes, useNavigate } from "react-router-dom";

import type {
  WPlusSopSession,
  WPlusSopStageReport,
} from "@/api/types/wplusSop";
import WPlusSopWorkspace from "./index";
import styles from "./index.module.less";

const apiMock = vi.hoisted(() => ({
  getSession: vi.fn(),
  sendCommand: vi.fn(),
  downloadArtifact: vi.fn(),
  downloadStageReportArtifact: vi.fn(),
  downloadCumulativeArtifact: vi.fn(),
  readArtifact: vi.fn(),
  readStageReportArtifact: vi.fn(),
  readCumulativeArtifact: vi.fn(),
  subscribeSessionEvents: vi.fn(),
}));

interface SubscriptionCallbacks {
  afterStateVersion: number;
  onEvent: (event: unknown) => void;
  onError: (error: unknown) => void;
}

let subscriptionCallbacks: SubscriptionCallbacks[] = [];

vi.mock("@/api/modules/wplusSop", () => ({
  wplusSopApi: apiMock,
}));

function makeSession(
  overrides: Partial<WPlusSopSession> = {},
): WPlusSopSession {
  return {
    session_id: "sop-1",
    chat_id: "chat-1",
    title: "客户经营 SOP",
    state: "AwaitingQueueConfirmation",
    state_version: 4,
    revision: 1,
    round: 1,
    runtime_status: {
      status: "ready",
      runtime_ready: true,
      blocking_run_id: null,
    },
    current_stage_id: "stage-1",
    stages: [
      {
        stage_id: "stage-1",
        title: "确认名单范围",
        description: "确定产品和时间窗口",
        status: "current",
      },
      {
        stage_id: "stage-2",
        title: "创建后续任务",
        description: "确认任务字段",
        status: "pending",
      },
    ],
    updated_at: "2026-07-28T08:00:00Z",
    ...overrides,
  };
}

function SessionSwitchControl() {
  const navigate = useNavigate();
  return (
    <button
      type="button"
      onClick={() => navigate("/wplus-sop/sop-2?from=chat")}
    >
      切换测试 Session
    </button>
  );
}

function renderPage(options: { withSessionSwitcher?: boolean } = {}) {
  return render(
    <MemoryRouter initialEntries={["/wplus-sop/sop-1?from=chat"]}>
      {options.withSessionSwitcher ? <SessionSwitchControl /> : null}
      <Routes>
        <Route path="/wplus-sop/:sessionId" element={<WPlusSopWorkspace />} />
        <Route path="/chat/:chatId" element={<p>所属 Chat</p>} />
      </Routes>
    </MemoryRouter>,
  );
}

function deferred<T>() {
  let resolve!: (value: T | PromiseLike<T>) => void;
  let reject!: (reason?: unknown) => void;
  const promise = new Promise<T>((resolvePromise, rejectPromise) => {
    resolve = resolvePromise;
    reject = rejectPromise;
  });
  return { promise, resolve, reject };
}

function emitSafeStreamTrace({
  runId,
  sequence,
  summaryText,
  stateVersion = 5,
  truncated = false,
  subscriptionIndex = 0,
  entries = [],
}: {
  runId: string;
  sequence: number;
  summaryText: string;
  stateVersion?: number;
  truncated?: boolean;
  subscriptionIndex?: number;
  entries?: Array<
    | {
        entry_id: string;
        kind: "assistant_text";
        text: string;
        status: "running" | "completed" | "failed";
      }
    | {
        entry_id: string;
        kind: "tool";
        tool_name: string;
        server_label?: string;
        status: "running" | "completed" | "failed";
      }
  >;
}) {
  act(() => {
    subscriptionCallbacks[subscriptionIndex].onEvent({
      event_id: `trace:sop-1:${runId}:${sequence}`,
      session_id: "sop-1",
      state_version: stateVersion,
      kind: "safe_stream_trace",
      run_id: runId,
      safe_stream_trace: {
        sequence,
        summary_text: summaryText,
        truncated,
        entries,
      },
    });
  });
}

describe("WPlusSopWorkspace", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    subscriptionCallbacks = [];
    apiMock.subscribeSessionEvents.mockImplementation(
      (_sessionId, afterStateVersion, onEvent, onError) => {
        subscriptionCallbacks.push({
          afterStateVersion,
          onEvent,
          onError,
        });
        return {
          close: vi.fn(),
          done: Promise.resolve(),
        };
      },
    );
    apiMock.getSession.mockResolvedValue(makeSession());
    apiMock.downloadArtifact.mockResolvedValue(
      new Blob(["artifact"], { type: "text/plain" }),
    );
    apiMock.downloadStageReportArtifact.mockResolvedValue(
      new Blob(["stage artifact"], { type: "text/plain" }),
    );
    apiMock.downloadCumulativeArtifact.mockResolvedValue(
      new Blob(["cumulative artifact"], { type: "text/plain" }),
    );
    apiMock.readArtifact.mockResolvedValue("<article>最终 SOP</article>");
    apiMock.readStageReportArtifact.mockResolvedValue(
      "<article>阶段 SOP v2</article>",
    );
    apiMock.readCumulativeArtifact.mockResolvedValue(
      "<article>累计 SOP v1</article>",
    );
    apiMock.sendCommand.mockImplementation(async (_sessionId, command) => ({
      command_request_id: command.command_request_id,
      accepted: true,
      session: makeSession({
        state: "GeneratingQuestions",
        state_version: 5,
      }),
    }));
  });

  afterEach(() => {
    cleanup();
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it("names the loading state and explains what is loading", () => {
    apiMock.getSession.mockReturnValue(new Promise(() => {}));
    renderPage();

    expect(
      screen.getByRole("status", { name: "正在加载 W+ SOP 工作台" }),
    ).toHaveAttribute("aria-live", "polite");
    expect(
      screen.getByText("正在同步环节、回答和预跑状态，请稍候。"),
    ).toBeInTheDocument();
  });

  it("announces generation without an estimated run progress bar", async () => {
    apiMock.getSession.mockResolvedValue(
      makeSession({
        state: "GeneratingQuestions",
        state_version: 5,
      }),
    );
    renderPage();

    expect(
      await screen.findByRole("status", { name: "正在生成问题" }),
    ).toHaveAttribute("aria-live", "polite");
    expect(
      screen.getByRole("progressbar", { name: "SOP 总体进度" }),
    ).toHaveAttribute("aria-valuenow", "12");
    expect(
      screen.queryByRole("progressbar", { name: "当前运行进度" }),
    ).not.toBeInTheDocument();
  });

  it("appends a read-only conclusion milestone after every business stage", async () => {
    apiMock.getSession.mockResolvedValue(
      makeSession({
        state: "AwaitingAnswer",
        stages: [
          {
            stage_id: "stage-1",
            title: "初筛存续到期客户名单",
            status: "confirmed",
          },
          {
            stage_id: "stage-2",
            title: "查询客户资产总览与实时收支",
            status: "current",
          },
          {
            stage_id: "stage-3",
            title: "分析存续到期处理建议",
            status: "pending",
          },
        ],
      }),
    );
    renderPage();

    const progressRegion = await screen.findByRole("region", {
      name: "SOP 环节进度",
    });
    const milestones = within(progressRegion).getAllByRole("listitem");
    const conclusion = milestones[milestones.length - 1];

    expect(milestones).toHaveLength(4);
    expect(conclusion).toHaveAttribute("data-status", "pending");
    expect(within(conclusion!).getByText("04")).toBeInTheDocument();
    expect(within(conclusion!).getByText("生成结论")).toBeInTheDocument();
    expect(within(conclusion!).getByText("等待中")).toBeInTheDocument();
  });

  it.each([
    ["FinalizingOutputs", "current", "生成中"],
    ["OutputReview", "current", "待确认结果"],
    ["MemoryReview", "current", "待确认"],
    ["Completed", "confirmed", "已完成"],
  ] as const)(
    "projects %s onto the conclusion milestone as %s",
    async (state, expectedStatus, expectedLabel) => {
      apiMock.getSession.mockResolvedValue(makeSession({ state }));
      renderPage();

      const conclusionTitle = await screen.findByText("生成结论");
      const conclusion = conclusionTitle.closest("li");

      expect(conclusion).toHaveAttribute("data-status", expectedStatus);
      expect(within(conclusion!).getByText(expectedLabel)).toBeInTheDocument();
      if (state === "Completed") {
        expect(
          screen.getByRole("progressbar", { name: "SOP 总体进度" }),
        ).toHaveAttribute("aria-valuenow", "100");
      }
    },
  );

  it("previews and confirms generated outputs before memory review", async () => {
    apiMock.readArtifact.mockImplementation(async (_sessionId, artifactId) =>
      artifactId === "sop_render_html"
        ? "<article><h1>客户经营 SOP</h1></article>"
        : "# 客户经营 SOP\n\n执行复核。",
    );
    apiMock.getSession.mockResolvedValue(
      makeSession({
        state: "OutputReview",
        state_version: 20,
        result_preview: {
          markdown: "# 客户经营 SOP\n\n执行复核。",
          html: "<article><h1>客户经营 SOP</h1></article>",
        },
        artifacts: [
          {
            artifact_id: "sop_render_html",
            name: "sop_render.html",
            format: "html",
            status: "validated",
            download_url: "/download/sop_render_html",
          },
          {
            artifact_id: "sop_render_md",
            name: "sop_render.md",
            format: "markdown",
            status: "validated",
            download_url: "/download/sop_render_md",
          },
        ],
      }),
    );
    renderPage();

    expect(
      await screen.findByRole("heading", { name: "检查最终结果" }),
    ).toBeInTheDocument();
    const htmlPreview = await screen.findByTitle("HTML 最终 SOP 预览");
    expect(htmlPreview).toHaveAttribute("sandbox", "");
    expect(htmlPreview.getAttribute("srcdoc")).toContain(
      "<article><h1>客户经营 SOP</h1></article>",
    );
    fireEvent.click(screen.getByRole("button", { name: "Markdown" }));
    expect(
      await screen.findByText("执行复核。", { exact: false }),
    ).toBeVisible();

    fireEvent.click(screen.getByRole("button", { name: "确认结果并继续" }));
    await waitFor(() =>
      expect(apiMock.sendCommand).toHaveBeenCalledWith(
        "sop-1",
        expect.objectContaining({ command: "confirm_outputs" }),
      ),
    );
  });

  it("uses the authenticated artifact API for generated output previews", async () => {
    const markdownSha = "a".repeat(64);
    const htmlSha = "b".repeat(64);
    const htmlBody = "<article><h1>Fetched HTML</h1></article>";
    apiMock.readArtifact.mockImplementation(async (_sessionId, artifactId) =>
      artifactId === "sop_render_html"
        ? htmlBody
        : "# API-backed SOP\n\nFetched body",
    );
    apiMock.getSession.mockResolvedValue(
      makeSession({
        state: "OutputReview",
        state_version: 20,
        result_preview: {
          markdown: "https://ignored.example.test/sop_render_6.md",
          html: "https://ignored.example.test/sop_render_6.html",
          markdown_url: "https://ignored.example.test/sop_render.md",
          html_url: "https://ignored.example.test/sop_render.html",
          markdown_sha256: markdownSha,
          html_sha256: htmlSha,
        },
        artifacts: [
          {
            artifact_id: "sop_render_html",
            name: "sop_render.html",
            format: "html",
            status: "validated",
          },
          {
            artifact_id: "sop_render_md",
            name: "sop_render.md",
            format: "markdown",
            status: "validated",
          },
        ],
      }),
    );

    renderPage();

    const htmlPreview = await screen.findByTitle("HTML 最终 SOP 预览");
    expect(htmlPreview).toHaveAttribute("sandbox", "");
    expect(htmlPreview.getAttribute("srcdoc")).toContain(htmlBody);
    expect(htmlPreview).not.toHaveAttribute("src");
    fireEvent.click(screen.getByRole("button", { name: "Markdown" }));
    expect(
      await screen.findByText("Fetched body", { exact: false }),
    ).toBeVisible();
    expect(apiMock.readArtifact).toHaveBeenCalledWith(
      "sop-1",
      "sop_render_md",
      expect.any(AbortSignal),
    );
  });

  it("sanitizes HTML previews and injects a restrictive CSP", async () => {
    apiMock.readArtifact.mockResolvedValue(
      '<script>window.pwned=true</script><link rel="stylesheet" href="https://evil.test/x.css"><meta http-equiv="refresh" content="0;url=https://evil.test"><iframe src="https://evil.test"></iframe><object data="https://evil.test"></object><embed src="https://evil.test"><article><img src="data:image/png;base64,AA=="><style>article{color:red}</style>安全正文</article>',
    );
    apiMock.getSession.mockResolvedValue(
      makeSession({
        state: "OutputReview",
        artifacts: [
          {
            artifact_id: "sop_render_html",
            name: "sop_render.html",
            format: "html",
            status: "validated",
          },
        ],
      }),
    );

    renderPage();

    const preview = await screen.findByTitle("HTML 最终 SOP 预览");
    const srcDoc = preview.getAttribute("srcdoc") || "";
    expect(srcDoc).toContain(
      "default-src 'none'; img-src data: blob:; style-src 'unsafe-inline'; font-src data:",
    );
    expect(srcDoc).toContain("安全正文");
    expect(srcDoc).toContain("data:image/png;base64,AA==");
    expect(srcDoc).not.toMatch(/<(script|link|iframe|object|embed)\b/i);
    expect(srcDoc).not.toMatch(/http-equiv=["']refresh/i);
    expect(srcDoc).not.toContain("https://evil.test");
  });

  it("does not render a stale preview body after switching formats", async () => {
    const html = deferred<string>();
    const markdown = deferred<string>();
    apiMock.readArtifact.mockImplementation((_sessionId, artifactId) =>
      artifactId === "sop_render_html" ? html.promise : markdown.promise,
    );
    apiMock.getSession.mockResolvedValue(
      makeSession({
        state: "OutputReview",
        artifacts: [
          {
            artifact_id: "sop_render_html",
            name: "sop_render.html",
            format: "html",
            status: "validated",
          },
          {
            artifact_id: "sop_render_md",
            name: "sop_render.md",
            format: "markdown",
            status: "validated",
          },
        ],
      }),
    );

    renderPage();
    await waitFor(() => expect(apiMock.readArtifact).toHaveBeenCalledTimes(1));
    fireEvent.click(screen.getByRole("button", { name: "Markdown" }));
    markdown.resolve("# 当前 Markdown\n\n不能被旧 HTML 覆盖");
    expect(await screen.findByText(/不能被旧 HTML 覆盖/)).toBeVisible();

    html.resolve("<article>过期 HTML</article>");
    await act(async () => {
      await html.promise;
    });
    expect(screen.getByText(/不能被旧 HTML 覆盖/)).toBeVisible();
    expect(screen.queryByTitle("HTML 最终 SOP 预览")).not.toBeInTheDocument();
  });

  it("previews only the three explicit final SOP artifacts when outputs are reordered", async () => {
    apiMock.readArtifact.mockImplementation(async (_sessionId, artifactId) =>
      artifactId === "sop_render_html"
        ? "<article>最终 SOP 正文</article>"
        : artifactId === "sop_render_md"
        ? "# 最终 SOP 正文"
        : '{"title":"最终 SOP 正文"}',
    );
    apiMock.getSession.mockResolvedValue(
      makeSession({
        state: "OutputReview",
        artifacts: [
          {
            artifact_id: "example_result_html",
            name: "example_result.html",
            format: "html",
            status: "validated",
          },
          {
            artifact_id: "sop_render_md",
            name: "sop_render.md",
            format: "markdown",
            status: "validated",
          },
          {
            artifact_id: "sop_spec",
            name: "sop_spec.json",
            format: "json",
            status: "validated",
          },
          {
            artifact_id: "sop_render_html",
            name: "sop_render.html",
            format: "html",
            status: "validated",
          },
        ],
      }),
    );

    renderPage();

    expect(
      (await screen.findByTitle("HTML 最终 SOP 预览")).getAttribute("srcdoc"),
    ).toContain("<article>最终 SOP 正文</article>");
    expect(apiMock.readArtifact).toHaveBeenCalledWith(
      "sop-1",
      "sop_render_html",
      expect.any(AbortSignal),
    );
    expect(apiMock.readArtifact).not.toHaveBeenCalledWith(
      "sop-1",
      "example_result_html",
      expect.any(AbortSignal),
    );
  });

  it("refreshes authenticated previews when artifact hashes change", async () => {
    const firstMarkdownSha = "1".repeat(64);
    const firstHtmlSha = "2".repeat(64);
    const nextMarkdownSha = "3".repeat(64);
    const nextHtmlSha = "4".repeat(64);
    const htmlBodies = [
      "<article>First HTML</article>",
      "<article>Updated HTML</article>",
    ];
    apiMock.readArtifact.mockImplementation(async () => htmlBodies.shift());
    const firstSession = makeSession({
      state: "OutputReview",
      state_version: 20,
      result_preview: {
        markdown: "ignored",
        html: "ignored",
        markdown_url: "https://ignored.example.test/sop_render.md",
        html_url: "https://ignored.example.test/sop_render.html",
        markdown_sha256: firstMarkdownSha,
        html_sha256: firstHtmlSha,
      },
      artifacts: [
        {
          artifact_id: "sop_render_html",
          name: "sop_render.html",
          format: "html",
          status: "validated",
        },
      ],
    });
    apiMock.getSession.mockResolvedValue(firstSession);
    renderPage();

    const firstHtmlPreview = await screen.findByTitle("HTML 最终 SOP 预览");
    await waitFor(() =>
      expect(firstHtmlPreview.getAttribute("srcdoc")).toContain(
        "<article>First HTML</article>",
      ),
    );

    act(() => {
      subscriptionCallbacks[0].onEvent({
        event_id: "snapshot:sop-1:21",
        session_id: "sop-1",
        state_version: 21,
        kind: "snapshot",
        snapshot: makeSession({
          ...firstSession,
          state_version: 21,
          result_preview: {
            ...firstSession.result_preview!,
            markdown_sha256: nextMarkdownSha,
            html_sha256: nextHtmlSha,
          },
        }),
      });
    });

    const nextHtmlPreview = await screen.findByTitle("HTML 最终 SOP 预览");
    await waitFor(() =>
      expect(nextHtmlPreview.getAttribute("srcdoc")).toContain(
        "<article>Updated HTML</article>",
      ),
    );
    expect(apiMock.readArtifact).toHaveBeenCalledTimes(2);
  });

  it("keeps one artifact request alive across equivalent SSE snapshots", async () => {
    const html = deferred<string>();
    const requestSignals: AbortSignal[] = [];
    apiMock.readArtifact.mockImplementation(
      (_sessionId, _artifactId, signal: AbortSignal) => {
        requestSignals.push(signal);
        return html.promise;
      },
    );
    const session = makeSession({
      state: "OutputReview",
      state_version: 20,
      result_preview: {
        markdown: "ignored",
        html: "ignored",
        markdown_sha256: "a".repeat(64),
        html_sha256: "b".repeat(64),
      },
      artifacts: [
        {
          artifact_id: "sop_render_html",
          name: "sop_render.html",
          format: "html",
          status: "validated",
          sha256: "b".repeat(64),
        },
      ],
    });
    apiMock.getSession.mockResolvedValue(session);
    renderPage();

    await waitFor(() => expect(apiMock.readArtifact).toHaveBeenCalledTimes(1));
    await waitFor(() => expect(subscriptionCallbacks).toHaveLength(1));
    act(() => {
      subscriptionCallbacks[0].onEvent({
        event_id: "snapshot:sop-1:21",
        session_id: "sop-1",
        state_version: 21,
        kind: "snapshot",
        snapshot: makeSession({
          ...session,
          state_version: 21,
          artifacts: session.artifacts?.map((artifact) => ({ ...artifact })),
          result_preview: { ...session.result_preview! },
        }),
      });
    });

    expect(apiMock.readArtifact).toHaveBeenCalledTimes(1);
    expect(requestSignals).toHaveLength(1);
    expect(requestSignals[0].aborted).toBe(false);

    html.resolve("<article>稳定预览</article>");
    expect(
      (await screen.findByTitle("HTML 最终 SOP 预览")).getAttribute("srcdoc"),
    ).toContain("稳定预览");
  });

  it("shows full memory content, target, failure retry, and write receipt", async () => {
    apiMock.getSession.mockResolvedValue(
      makeSession({
        state: "MemoryReview",
        state_version: 21,
        memory_candidates: [
          {
            candidate_id: "candidate-failed",
            title: "保存复核规则",
            memory_type: "common_wplus_knowledge",
            content: { rule: "优先复核高风险分组" },
            evidence: "用户确认该页面事实已经验证。",
            target_scope: "common",
            target_file: "memory/common-wplus-knowledge.jsonl",
            status: "failed",
            failure_reason: "disk unavailable",
          },
          {
            candidate_id: "candidate-approved",
            title: "保存时间窗口",
            memory_type: "sop_case",
            content: { pattern: "默认检查未来 30 天" },
            evidence: "用户确认这是完全脱敏的 SOP 模式。",
            target_scope: "cases",
            target_file: "memory/cases/sop-cases.jsonl",
            status: "approved",
            write_receipt: {
              memory_id: "wplus-sop/sop-1/candidate-approved",
              target_scope: "cases",
              target_file: "memory/cases/sop-cases.jsonl",
              written_at: "2026-08-04T10:00:00Z",
              reused_existing: false,
              store_result: "appended",
            },
          },
        ],
      }),
    );
    renderPage();

    expect(
      await screen.findByText((_, element) =>
        Boolean(
          element?.tagName === "PRE" &&
            element.textContent?.includes("优先复核高风险分组"),
        ),
      ),
    ).toBeInTheDocument();
    expect(screen.getAllByText("公共 W+ 知识")).toHaveLength(2);
    expect(screen.getByText("脱敏 SOP 模式")).toBeInTheDocument();
    expect(
      screen.getByText("用户确认该页面事实已经验证。"),
    ).toBeInTheDocument();
    expect(
      screen.getByText("memory/common-wplus-knowledge.jsonl"),
    ).toBeInTheDocument();
    expect(
      screen.getByText("memory/cases/sop-cases.jsonl"),
    ).toBeInTheDocument();
    expect(screen.getByText("disk unavailable")).toBeInTheDocument();
    expect(
      screen.getByText("wplus-sop/sop-1/candidate-approved"),
    ).toBeInTheDocument();

    fireEvent.click(screen.getByRole("radio", { name: "重试写入" }));
    const retrySubmit = screen.getByRole("button", {
      name: "统一提交记忆选择",
    });
    await waitFor(() => expect(retrySubmit).toBeEnabled());
    fireEvent.click(retrySubmit);
    await waitFor(() =>
      expect(apiMock.sendCommand).toHaveBeenCalledWith(
        "sop-1",
        expect.objectContaining({
          command: "resolve_memory",
          payload: {
            decisions: [
              { candidate_id: "candidate-failed", decision: "approve" },
            ],
          },
        }),
      ),
    );
  });

  it("shows the approved memory candidate as an Agent write in progress", async () => {
    apiMock.getSession.mockResolvedValue(
      makeSession({
        state: "WritingMemory",
        state_version: 22,
        memory_candidates: [
          {
            candidate_id: "candidate-writing",
            title: "保存已验证事实",
            memory_type: "common_wplus_knowledge",
            content: { fact: "支持到期日筛选" },
            evidence: "用户确认该能力已经验证。",
            target_scope: "common",
            target_file: "memory/common-wplus-knowledge.jsonl",
            status: "writing",
          },
        ],
      }),
    );

    renderPage();

    expect(
      await screen.findByText("正在调用 memory_store.py"),
    ).toBeInTheDocument();
    expect(screen.getAllByText("写入中")).toHaveLength(2);
    expect(
      screen.getByRole("button", { name: "统一提交记忆选择" }),
    ).toBeDisabled();
    expect(
      screen.queryByRole("radio", { name: "保存" }),
    ).not.toBeInTheDocument();
  });

  it("shows legacy memory history as read-only and never offers an impossible approval", async () => {
    apiMock.getSession.mockResolvedValue(
      makeSession({
        state: "MemoryReview",
        state_version: 22,
        memory_candidates: [
          {
            candidate_id: "legacy-approved",
            title: "旧版已批准候选",
            content: "旧版自由文本",
            status: "approved",
            legacy_read_only: true,
          },
          {
            candidate_id: "legacy-failed",
            title: "旧版失败候选",
            content: { rule: "旧版记录" },
            status: "failed",
            legacy_read_only: true,
          },
        ],
      }),
    );

    renderPage();

    expect(
      await screen.findByText("历史已批准（无可验证写入回执）"),
    ).toBeInTheDocument();
    expect(screen.queryByText("已写入")).not.toBeInTheDocument();
    const failedChoices = screen.getByRole("radiogroup", {
      name: "记忆候选：旧版失败候选",
    });
    expect(
      within(failedChoices).queryByRole("radio", { name: "重试写入" }),
    ).not.toBeInTheDocument();
    expect(
      within(failedChoices).getByRole("radio", { name: "不保存" }),
    ).toBeInTheDocument();
  });

  it("submits all memory decisions once only after every candidate is selected", async () => {
    apiMock.getSession.mockResolvedValue(
      makeSession({
        state: "MemoryReview",
        state_version: 23,
        memory_candidates: [
          {
            candidate_id: "candidate-1",
            title: "保存规则一",
            content: { rule: "one" },
            status: "pending",
          },
          {
            candidate_id: "candidate-2",
            title: "保存规则二",
            content: { rule: "two" },
            status: "pending",
          },
        ],
      }),
    );
    renderPage();

    const submit = await screen.findByRole("button", {
      name: "统一提交记忆选择",
    });
    const firstChoices = screen.getByRole("radiogroup", {
      name: "记忆候选：保存规则一",
    });
    const secondChoices = screen.getByRole("radiogroup", {
      name: "记忆候选：保存规则二",
    });
    const approve = within(firstChoices).getByRole("radio", { name: "保存" });
    expect(submit).toBeDisabled();
    fireEvent.click(approve);
    expect(approve).toBeChecked();
    expect(submit).toBeDisabled();
    const reject = within(secondChoices).getByRole("radio", {
      name: "不保存",
    });
    fireEvent.click(reject);
    expect(reject).toBeChecked();
    await waitFor(() => expect(submit).toBeEnabled());
    fireEvent.click(submit);

    await waitFor(() =>
      expect(apiMock.sendCommand).toHaveBeenCalledWith(
        "sop-1",
        expect.objectContaining({
          command: "resolve_memory",
          payload: {
            decisions: [
              { candidate_id: "candidate-1", decision: "approve" },
              { candidate_id: "candidate-2", decision: "reject" },
            ],
          },
        }),
      ),
    );
  });

  it("clears memory decisions when navigating to a different session with reused candidate IDs", async () => {
    const memorySession = (sessionId: string, title: string) =>
      makeSession({
        session_id: sessionId,
        title,
        state: "MemoryReview",
        state_version: 23,
        memory_candidates: [
          {
            candidate_id: "shared-candidate",
            title: "共享候选编号",
            content: { source: sessionId },
            status: "pending",
          },
        ],
      });
    apiMock.getSession.mockImplementation(async (requestedSessionId) =>
      requestedSessionId === "sop-2"
        ? memorySession("sop-2", "会话 B")
        : memorySession("sop-1", "会话 A"),
    );
    renderPage({ withSessionSwitcher: true });

    const approveInSessionA = await screen.findByRole("radio", {
      name: /^保\s*存$/,
    });
    fireEvent.click(approveInSessionA);
    expect(approveInSessionA).toBeChecked();
    expect(
      screen.getByRole("button", { name: "统一提交记忆选择" }),
    ).toBeEnabled();

    fireEvent.click(screen.getByRole("button", { name: "切换测试 Session" }));

    expect(
      await screen.findByRole("heading", { name: "会话 B" }),
    ).toBeInTheDocument();
    const sessionBChoices = screen.getByRole("radiogroup", {
      name: "记忆候选：共享候选编号",
    });
    expect(
      within(sessionBChoices).getByRole("radio", { name: /^保\s*存$/ }),
    ).not.toBeChecked();
    expect(
      within(sessionBChoices).getByRole("radio", { name: "不保存" }),
    ).not.toBeChecked();
    expect(
      screen.getByRole("button", { name: "统一提交记忆选择" }),
    ).toBeDisabled();
  });

  it("keeps batch memory submission disabled until the prior Agent is ready", async () => {
    apiMock.getSession.mockResolvedValue(
      makeSession({
        state: "MemoryReview",
        state_version: 24,
        runtime_status: {
          status: "running",
          runtime_ready: false,
          blocking_run_id: "run-prior",
        },
        memory_candidates: [
          {
            candidate_id: "candidate-1",
            title: "保存规则",
            content: { rule: "one" },
            status: "pending",
          },
        ],
      }),
    );
    renderPage();

    fireEvent.click(await screen.findByRole("radio", { name: /^保\s*存$/ }));
    expect(
      screen.getByRole("button", { name: "统一提交记忆选择" }),
    ).toBeDisabled();
    expect(screen.getByText("正在等待上一轮 Agent 完成")).toBeInTheDocument();
    expect(apiMock.sendCommand).not.toHaveBeenCalled();
  });

  it("gives the pre-run result table a caption and scoped headers", async () => {
    apiMock.getSession.mockResolvedValue(
      makeSession({
        state: "AwaitingTrialFeedback",
        state_version: 10,
        trial: {
          run_id: "run-1",
          status: "completed",
          steps: [],
          result_columns: [
            { field: "product", label: "产品" },
            { field: "due_at", label: "到期日" },
          ],
          result_rows: [{ product: "稳健理财", due_at: "2026-08-01" }],
        },
      }),
    );
    renderPage();

    const table = await screen.findByRole("table", {
      name: "系统预跑结果明细",
    });
    const caption = table.querySelector("caption");
    const headers = table.querySelectorAll("th");

    expect(caption).toHaveTextContent("系统预跑结果明细");
    expect(headers).toHaveLength(2);
    expect(Array.from(headers)).toEqual([
      expect.objectContaining({ scope: "col" }),
      expect.objectContaining({ scope: "col" }),
    ]);
  });

  it("exposes the complete value of a long stage title", async () => {
    const longStageTitle =
      "核验跨区域重点客户近十二个月到期资产与当前持仓的完整覆盖范围";
    apiMock.getSession.mockResolvedValue(
      makeSession({
        stages: [
          {
            stage_id: "stage-1",
            title: longStageTitle,
            description: "确定产品和时间窗口",
            status: "current",
          },
        ],
      }),
    );
    renderPage();

    expect(
      await screen.findByText(longStageTitle, { selector: "strong" }),
    ).toHaveAttribute("title", longStageTitle);
  });

  it("opens and closes the named evidence drawer from the narrow-shell entry", async () => {
    renderPage();

    const trigger = await screen.findByRole("button", {
      name: "查看本次 SOP 证据",
      hidden: true,
    });
    fireEvent.click(trigger);

    expect(
      await screen.findByRole("dialog", { name: "本次 SOP 证据" }),
    ).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "关闭本次 SOP 证据" }));
    await waitFor(() =>
      expect(
        screen.queryByRole("dialog", { name: "本次 SOP 证据" }),
      ).not.toBeInTheDocument(),
    );
  });

  it("closes the evidence drawer when navigating to another SOP session", async () => {
    renderPage({ withSessionSwitcher: true });

    fireEvent.click(
      await screen.findByRole("button", {
        name: "查看本次 SOP 证据",
        hidden: true,
      }),
    );
    expect(
      await screen.findByRole("dialog", { name: "本次 SOP 证据" }),
    ).toBeInTheDocument();

    apiMock.getSession.mockResolvedValue(
      makeSession({ session_id: "sop-2", state_version: 1 }),
    );
    fireEvent.click(screen.getByRole("button", { name: "切换测试 Session" }));

    await waitFor(() =>
      expect(
        screen.queryByRole("dialog", { name: "本次 SOP 证据" }),
      ).not.toBeInTheDocument(),
    );
  });

  it("edits and atomically confirms a valid stage queue", async () => {
    renderPage();

    const firstStage = await screen.findByDisplayValue("确认名单范围");
    fireEvent.change(firstStage, { target: { value: "确认目标名单" } });
    fireEvent.click(screen.getByLabelText("将“创建后续任务”上移"));
    fireEvent.click(screen.getByRole("button", { name: "确认这 2 个环节" }));

    await waitFor(() => expect(apiMock.sendCommand).toHaveBeenCalledTimes(1));
    const [, command] = apiMock.sendCommand.mock.calls[0];
    expect(command.command).toBe("confirm_stage_queue");
    expect(command.payload.stages).toEqual([
      expect.objectContaining({ stage_id: "stage-2" }),
      expect.objectContaining({
        stage_id: "stage-1",
        title: "确认目标名单",
      }),
    ]);
  });

  it("adds and confirms a fifth stage without imposing a manual upper limit", async () => {
    apiMock.getSession.mockResolvedValue(
      makeSession({
        stages: [
          ...makeSession().stages,
          {
            stage_id: "stage-3",
            title: "核验客户资料",
            description: "确认资料是否完整",
            status: "pending",
          },
          {
            stage_id: "stage-4",
            title: "安排跟进计划",
            description: "确认后续负责人",
            status: "pending",
          },
        ],
      }),
    );
    renderPage();

    const addButton = await screen.findByRole("button", { name: "增加环节" });
    expect(
      screen.getByText("自动候选 2–4 个 · 手动新增不限"),
    ).toBeInTheDocument();
    expect(addButton).toBeEnabled();
    fireEvent.click(addButton);
    fireEvent.click(screen.getByRole("button", { name: "确认这 5 个环节" }));

    await waitFor(() => expect(apiMock.sendCommand).toHaveBeenCalledTimes(1));
    expect(apiMock.sendCommand.mock.calls[0][1]).toEqual(
      expect.objectContaining({
        command: "confirm_stage_queue",
        payload: {
          stages: expect.arrayContaining([
            expect.objectContaining({ title: "新环节 5" }),
          ]),
        },
      }),
    );
    expect(apiMock.sendCommand.mock.calls[0][1].payload.stages).toHaveLength(5);
  }, 15_000);

  it("uses native radio semantics and submits the whole question batch once", async () => {
    apiMock.getSession.mockResolvedValue(
      makeSession({
        state: "AwaitingAnswer",
        state_version: 7,
        question_batch: {
          batch_id: "batch-1",
          stage_id: "stage-1",
          questions: [
            {
              question_id: "q-1",
              kind: "single_select",
              prompt: "到期窗口多长？",
              required: true,
              options: [
                { option_id: "30d", label: "未来 30 天" },
                { option_id: "60d", label: "未来 60 天" },
              ],
            },
            {
              question_id: "q-2",
              kind: "free_text",
              prompt: "名单状态是什么？",
              required: true,
            },
          ],
        },
      }),
    );
    renderPage();

    const radio = await screen.findByRole("radio", { name: "未来 30 天" });
    fireEvent.click(radio);
    expect(radio).toBeChecked();
    fireEvent.change(screen.getByLabelText("名单状态是什么？"), {
      target: { value: "待处理" },
    });
    fireEvent.click(screen.getByRole("button", { name: "提交本轮 2 个回答" }));

    await waitFor(() => expect(apiMock.sendCommand).toHaveBeenCalledTimes(1));
    expect(apiMock.sendCommand.mock.calls[0][1]).toEqual(
      expect.objectContaining({
        command: "submit_answers",
        expected_state_version: 7,
        payload: {
          batch_id: "batch-1",
          answers: {
            "q-1": "30d",
            "q-2": "待处理",
          },
        },
      }),
    );
  });

  it("keeps answers editable but blocks submission while the owning Chat is finalizing", async () => {
    apiMock.getSession.mockResolvedValue(
      makeSession({
        state: "AwaitingAnswer",
        state_version: 7,
        runtime_status: {
          status: "finalizing",
          runtime_ready: false,
          blocking_run_id: "run-question-batch",
        },
        question_batch: {
          batch_id: "batch-waiting",
          stage_id: "stage-1",
          questions: [
            {
              question_id: "q-waiting",
              kind: "free_text",
              prompt: "补充客户范围",
              required: true,
            },
          ],
        },
      } as Partial<WPlusSopSession>),
    );
    renderPage();

    const answer = await screen.findByLabelText("补充客户范围");
    fireEvent.change(answer, { target: { value: "重点客户" } });

    expect(answer).toHaveValue("重点客户");
    expect(
      screen.getByRole("button", { name: "正在完成上一轮处理" }),
    ).toBeDisabled();
    expect(screen.getAllByText("正在完成上一轮处理")).toHaveLength(2);
    expect(apiMock.sendCommand).not.toHaveBeenCalled();
  });

  it("fails closed when an AwaitingAnswer snapshot has no runtime readiness", async () => {
    apiMock.getSession.mockResolvedValue(
      makeSession({
        state: "AwaitingAnswer",
        state_version: 7,
        runtime_status: undefined,
        question_batch: {
          batch_id: "batch-legacy",
          stage_id: "stage-1",
          questions: [
            {
              question_id: "q-legacy",
              kind: "free_text",
              prompt: "补充名单",
              required: true,
            },
          ],
        },
      } as Partial<WPlusSopSession>),
    );
    renderPage();

    fireEvent.change(await screen.findByLabelText("补充名单"), {
      target: { value: "已补充" },
    });
    expect(
      screen.getByRole("button", { name: "正在完成上一轮处理" }),
    ).toBeDisabled();
  });

  it("enables answer submission from a same-version runtime_status SSE event", async () => {
    apiMock.getSession.mockResolvedValue(
      makeSession({
        state: "AwaitingAnswer",
        state_version: 7,
        runtime_status: {
          status: "finalizing",
          runtime_ready: false,
          blocking_run_id: "run-question-batch",
        },
        question_batch: {
          batch_id: "batch-runtime-ready",
          stage_id: "stage-1",
          questions: [
            {
              question_id: "q-runtime-ready",
              kind: "free_text",
              prompt: "补充触达规则",
              required: true,
            },
          ],
        },
      } as Partial<WPlusSopSession>),
    );
    renderPage();

    fireEvent.change(await screen.findByLabelText("补充触达规则"), {
      target: { value: "仅工作日" },
    });
    await waitFor(() => expect(subscriptionCallbacks).toHaveLength(1));
    expect(
      screen.getByRole("button", { name: "正在完成上一轮处理" }),
    ).toBeDisabled();

    act(() => {
      subscriptionCallbacks[0].onEvent({
        event_id: "runtime-ready:sop-1",
        session_id: "sop-1",
        state_version: 7,
        kind: "runtime_status",
        runtime_status: {
          status: "ready",
          runtime_ready: true,
          blocking_run_id: null,
        },
      });
    });

    const submit = screen.getByRole("button", {
      name: "提交本轮 1 个回答",
    });
    expect(submit).toBeEnabled();
    fireEvent.click(submit);
    await waitFor(() => expect(apiMock.sendCommand).toHaveBeenCalledTimes(1));
  });

  it("preserves answer drafts and explains an owning_chat_finalizing 409", async () => {
    const session = makeSession({
      state: "AwaitingAnswer",
      state_version: 7,
      runtime_status: {
        status: "ready",
        runtime_ready: true,
        blocking_run_id: null,
      },
      question_batch: {
        batch_id: "batch-race",
        stage_id: "stage-1",
        questions: [
          {
            question_id: "q-race",
            kind: "free_text",
            prompt: "填写执行范围",
            required: true,
          },
        ],
      },
    } as Partial<WPlusSopSession>);
    apiMock.getSession.mockResolvedValue(session);
    let rejectCommand: ((reason?: unknown) => void) | undefined;
    apiMock.sendCommand.mockImplementation(
      () =>
        new Promise((_resolve, reject) => {
          rejectCommand = reject;
        }),
    );
    const finalizingError = Object.assign(
      new Error("owning Chat is finalizing"),
      {
        status: 409,
        data: {
          detail: {
            code: "owning_chat_finalizing",
            message: "上一轮 Agent 正在收尾",
            retry_after_ms: 1000,
          },
        },
      },
    );
    renderPage();

    const answer = await screen.findByLabelText("填写执行范围");
    fireEvent.change(answer, { target: { value: "保留这份回答" } });
    fireEvent.click(screen.getByRole("button", { name: "提交本轮 1 个回答" }));
    await waitFor(() => expect(rejectCommand).toBeDefined());
    await act(async () => rejectCommand?.(finalizingError));

    expect(
      await screen.findByText(
        "上一轮处理仍在结束中，回答已保留，请稍候再提交。",
      ),
    ).toBeInTheDocument();
    expect(answer).toHaveValue("保留这份回答");
    expect(apiMock.getSession).toHaveBeenCalledTimes(2);
    await waitFor(() =>
      expect(
        screen.getByRole("button", { name: /提交本轮 1 个回答/ }),
      ).toBeEnabled(),
    );
    expect(screen.queryByText(/页面状态已变化/)).not.toBeInTheDocument();
  });

  it("ignores a late command 409 after navigating to another SOP session", async () => {
    const questionBatch = {
      batch_id: "batch-route-race",
      stage_id: "stage-1",
      questions: [
        {
          question_id: "q-route-race",
          kind: "free_text" as const,
          prompt: "填写范围",
          required: true,
        },
      ],
    };
    apiMock.getSession
      .mockResolvedValueOnce(
        makeSession({
          state: "AwaitingAnswer",
          state_version: 7,
          question_batch: questionBatch,
        }),
      )
      .mockResolvedValueOnce(
        makeSession({
          session_id: "sop-2",
          title: "新会话",
          state: "AwaitingAnswer",
          state_version: 3,
          question_batch: questionBatch,
        }),
      );
    let rejectCommand: ((reason?: unknown) => void) | undefined;
    apiMock.sendCommand.mockImplementation(
      () =>
        new Promise((_resolve, reject) => {
          rejectCommand = reject;
        }),
    );
    renderPage({ withSessionSwitcher: true });

    fireEvent.change(await screen.findByLabelText("填写范围"), {
      target: { value: "旧会话回答" },
    });
    fireEvent.click(screen.getByRole("button", { name: "提交本轮 1 个回答" }));
    await waitFor(() => expect(rejectCommand).toBeDefined());
    fireEvent.click(screen.getByRole("button", { name: "切换测试 Session" }));
    expect(
      await screen.findByRole("heading", { name: "新会话" }),
    ).toBeInTheDocument();

    await act(async () => {
      rejectCommand?.(
        Object.assign(new Error("owning Chat is finalizing"), {
          status: 409,
          data: { detail: { code: "owning_chat_finalizing" } },
        }),
      );
    });
    expect(screen.getByRole("heading", { name: "新会话" })).toBeInTheDocument();
    expect(screen.queryByText(/上一轮处理仍在结束中/)).not.toBeInTheDocument();
    expect(apiMock.getSession).toHaveBeenCalledTimes(2);
  });

  it("submits single- and multi-select custom answers as structured values", async () => {
    apiMock.getSession.mockResolvedValue(
      makeSession({
        state: "AwaitingAnswer",
        state_version: 7,
        question_batch: {
          batch_id: "batch-custom",
          stage_id: "stage-1",
          questions: [
            {
              question_id: "q-single",
              kind: "single_select",
              prompt: "选择触达渠道",
              required: true,
              options: [
                { option_id: "phone", label: "电话" },
                {
                  option_id: "single-other",
                  label: "其他渠道",
                  requires_custom_input: true,
                },
              ],
            },
            {
              question_id: "q-multi",
              kind: "multi_select",
              prompt: "选择跟进动作",
              required: true,
              options: [
                { option_id: "call", label: "致电" },
                {
                  option_id: "multi-other-1",
                  label: "其他动作一",
                  requires_custom_input: true,
                },
                {
                  option_id: "multi-other-2",
                  label: "其他动作二",
                  requires_custom_input: true,
                },
              ],
            },
          ],
        },
      }),
    );
    renderPage();

    fireEvent.click(await screen.findByRole("radio", { name: "其他渠道" }));
    const singleCustomInput = screen.getByLabelText("选择触达渠道 自定义补充");
    expect(singleCustomInput).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: "提交本轮 2 个回答" }),
    ).toBeDisabled();
    fireEvent.change(singleCustomInput, { target: { value: "企业微信" } });

    fireEvent.click(screen.getByRole("checkbox", { name: "致电" }));
    fireEvent.click(screen.getByRole("checkbox", { name: "其他动作一" }));
    fireEvent.click(screen.getByRole("checkbox", { name: "其他动作二" }));
    const multiCustomInput = screen.getByLabelText("选择跟进动作 自定义补充");
    expect(multiCustomInput).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: "提交本轮 2 个回答" }),
    ).toBeDisabled();
    fireEvent.change(multiCustomInput, { target: { value: "寄送纸质资料" } });

    fireEvent.click(screen.getByRole("button", { name: "提交本轮 2 个回答" }));

    await waitFor(() => expect(apiMock.sendCommand).toHaveBeenCalledTimes(1));
    expect(apiMock.sendCommand.mock.calls[0][1]).toEqual(
      expect.objectContaining({
        command: "submit_answers",
        payload: {
          batch_id: "batch-custom",
          answers: {
            "q-single": {
              selected_option_ids: ["single-other"],
              text: "企业微信",
            },
            "q-multi": {
              selected_option_ids: ["call", "multi-other-1", "multi-other-2"],
              text: "寄送纸质资料",
            },
          },
        },
      }),
    );
  }, 15_000);

  it("wraps long prompts, labels selection types, and submits every multi-select option", async () => {
    const longPrompt =
      "请选择本环节需要覆盖的全部客户触达渠道，并结合实际执行范围确认所有适用选项";
    apiMock.getSession.mockResolvedValue(
      makeSession({
        state: "AwaitingAnswer",
        state_version: 7,
        question_batch: {
          batch_id: "batch-selection-types",
          stage_id: "stage-1",
          questions: [
            {
              question_id: "q-single",
              kind: "single_select",
              prompt: "选择主要渠道",
              required: true,
              options: [{ option_id: "phone", label: "电话" }],
            },
            {
              question_id: "q-multi",
              kind: "multi_select",
              prompt: longPrompt,
              required: true,
              options: [
                {
                  option_id: "call",
                  label: "致电",
                  description: "适合需要实时沟通的客户",
                },
                {
                  option_id: "message",
                  label: "企业微信",
                  description: "适合异步发送资料和提醒",
                },
              ],
            },
          ],
        },
      }),
    );
    renderPage();

    const renderedLongPrompt = await screen.findByText(longPrompt);
    expect(renderedLongPrompt).toHaveClass(styles.questionPrompt);
    expect(renderedLongPrompt.closest("legend")).toHaveClass(
      styles.questionLegend,
    );
    expect(screen.getByText("单选")).toBeInTheDocument();
    expect(screen.getByText("多选")).toBeInTheDocument();
    expect(screen.getAllByText("必填")).toHaveLength(2);
    expect(screen.getByText("适合需要实时沟通的客户")).toBeInTheDocument();
    expect(screen.getByText("适合异步发送资料和提醒")).toBeInTheDocument();

    fireEvent.click(screen.getByRole("radio", { name: "电话" }));
    fireEvent.click(
      screen.getByRole("checkbox", {
        name: "致电 适合需要实时沟通的客户",
      }),
    );
    fireEvent.click(
      screen.getByRole("checkbox", {
        name: "企业微信 适合异步发送资料和提醒",
      }),
    );
    fireEvent.click(screen.getByRole("button", { name: "提交本轮 2 个回答" }));

    await waitFor(() => expect(apiMock.sendCommand).toHaveBeenCalledTimes(1));
    expect(apiMock.sendCommand.mock.calls[0][1]).toEqual(
      expect.objectContaining({
        command: "submit_answers",
        payload: {
          batch_id: "batch-selection-types",
          answers: {
            "q-single": "phone",
            "q-multi": ["call", "message"],
          },
        },
      }),
    );
  });

  it("disables every answer control while answers are being submitted", async () => {
    apiMock.getSession.mockResolvedValue(
      makeSession({
        state: "AwaitingAnswer",
        state_version: 7,
        question_batch: {
          batch_id: "batch-busy-controls",
          stage_id: "stage-1",
          questions: [
            {
              question_id: "q-single",
              kind: "single_select",
              prompt: "选择主要渠道",
              required: true,
              options: [
                {
                  option_id: "other",
                  label: "其他渠道",
                  requires_custom_input: true,
                },
              ],
            },
            {
              question_id: "q-multi",
              kind: "multi_select",
              prompt: "选择补充渠道",
              required: true,
              options: [{ option_id: "message", label: "企业微信" }],
            },
            {
              question_id: "q-free",
              kind: "free_text",
              prompt: "填写执行范围",
              required: true,
            },
          ],
        },
      }),
    );
    let rejectCommand: ((reason?: unknown) => void) | undefined;
    apiMock.sendCommand.mockImplementation(
      () =>
        new Promise((_resolve, reject) => {
          rejectCommand = reject;
        }),
    );
    renderPage();

    const single = await screen.findByRole("radio", { name: "其他渠道" });
    const multi = screen.getByRole("checkbox", { name: "企业微信" });
    const freeText = screen.getByLabelText("填写执行范围");
    fireEvent.click(single);
    const customText = screen.getByLabelText("选择主要渠道 自定义补充");
    fireEvent.change(customText, { target: { value: "线下拜访" } });
    fireEvent.click(multi);
    fireEvent.change(freeText, { target: { value: "覆盖华东区" } });
    fireEvent.click(screen.getByRole("button", { name: "提交本轮 3 个回答" }));

    await waitFor(() => expect(rejectCommand).toBeDefined());
    expect(single).toBeDisabled();
    expect(multi).toBeDisabled();
    expect(customText).toBeDisabled();
    expect(freeText).toBeDisabled();

    await act(async () => rejectCommand?.(new Error("network unavailable")));

    await waitFor(() => {
      expect(single).toBeEnabled();
      expect(multi).toBeEnabled();
      expect(customText).toBeEnabled();
      expect(freeText).toBeEnabled();
    });
  });

  it("hides a custom input after switching to a normal option", async () => {
    apiMock.getSession.mockResolvedValue(
      makeSession({
        state: "AwaitingAnswer",
        state_version: 7,
        question_batch: {
          batch_id: "batch-custom-toggle",
          stage_id: "stage-1",
          questions: [
            {
              question_id: "q-single",
              kind: "single_select",
              prompt: "选择触达渠道",
              required: true,
              options: [
                { option_id: "phone", label: "电话" },
                {
                  option_id: "other",
                  label: "其他渠道",
                  requires_custom_input: true,
                },
              ],
            },
          ],
        },
      }),
    );
    renderPage();

    fireEvent.click(await screen.findByRole("radio", { name: "其他渠道" }));
    expect(
      screen.getByLabelText("选择触达渠道 自定义补充"),
    ).toBeInTheDocument();
    fireEvent.click(screen.getByRole("radio", { name: "电话" }));
    expect(
      screen.queryByLabelText("选择触达渠道 自定义补充"),
    ).not.toBeInTheDocument();
  });

  it("does not expose save and exit from the workspace header", async () => {
    apiMock.getSession.mockResolvedValue(
      makeSession({
        state: "AwaitingAnswer",
        state_version: 7,
        question_batch: {
          batch_id: "batch-1",
          stage_id: "stage-1",
          questions: [],
        },
      }),
    );
    renderPage();

    expect(
      await screen.findByRole("heading", { name: "客户经营 SOP" }),
    ).toBeVisible();
    expect(
      screen.queryByRole("button", { name: "保存并退出" }),
    ).not.toBeInTheDocument();
    expect(apiMock.sendCommand).not.toHaveBeenCalled();
  });

  it("isolates answer drafts by session and question batch", async () => {
    const firstBatch = makeSession({
      state: "AwaitingAnswer",
      state_version: 7,
      question_batch: {
        batch_id: "batch-1",
        stage_id: "stage-1",
        questions: [
          {
            question_id: "q-shared",
            kind: "free_text",
            prompt: "第一批说明",
            required: true,
          },
        ],
      },
    });
    apiMock.getSession.mockResolvedValue(firstBatch);
    renderPage();

    const firstAnswer = await screen.findByLabelText("第一批说明");
    fireEvent.change(firstAnswer, { target: { value: "只属于第一批" } });
    await waitFor(() => expect(subscriptionCallbacks).toHaveLength(1));

    const secondBatch = makeSession({
      state: "AwaitingAnswer",
      state_version: 8,
      question_batch: {
        batch_id: "batch-2",
        stage_id: "stage-1",
        questions: [
          {
            question_id: "q-shared",
            kind: "free_text",
            prompt: "第二批说明",
            required: true,
          },
        ],
      },
    });
    act(() => {
      subscriptionCallbacks[0].onEvent({
        event_id: "evt-8",
        session_id: "sop-1",
        state_version: 8,
        kind: "question_batch_presented",
        snapshot: secondBatch,
      });
    });

    expect(await screen.findByLabelText("第二批说明")).toHaveValue("");
    expect(
      screen.getByRole("button", { name: "提交本轮 1 个回答" }),
    ).toBeDisabled();
  });

  it("always exposes feedback after a completed pre-run and starts a real rerun", async () => {
    apiMock.getSession.mockResolvedValue(
      makeSession({
        state: "AwaitingTrialFeedback",
        state_version: 10,
        trial: {
          run_id: "run-1",
          status: "completed",
          steps: [
            {
              step_id: "step-1",
              title: "查询到期产品",
              status: "completed",
            },
          ],
          summary: "已完成查询并脱敏。",
          result_rows: [{ product: "稳健理财", due_at: "2026-08-01" }],
        },
        facts: ["统计范围为未来 30 天"],
        unknowns: ["是否排除已冻结账户"],
        capabilities: [
          {
            capability_id: "crm.query",
            name: "客户产品查询",
            verification_status: "verified",
            output_contract_status: "verified",
          },
        ],
      }),
    );
    renderPage();

    const feedback = await screen.findByLabelText("预跑反馈");
    expect(screen.getByText("已完成查询并脱敏。")).toBeInTheDocument();
    expect(screen.getByText("稳健理财")).toBeInTheDocument();
    expect(screen.getByText("统计范围为未来 30 天")).toBeInTheDocument();
    expect(screen.getByText("是否排除已冻结账户")).toBeInTheDocument();
    expect(screen.getByText("客户产品查询")).toBeInTheDocument();
    expect(screen.getByText("已验证")).toBeInTheDocument();
    fireEvent.change(feedback, {
      target: { value: "排除缺少任务日期的记录" },
    });
    fireEvent.click(screen.getByRole("button", { name: "提交反馈并重新预跑" }));

    await waitFor(() => expect(apiMock.sendCommand).toHaveBeenCalledTimes(1));
    expect(apiMock.sendCommand.mock.calls[0][1]).toEqual(
      expect.objectContaining({
        command: "submit_trial_feedback",
        payload: {
          feedback: "排除缺少任务日期的记录",
          rerun_of_run_id: "run-1",
        },
      }),
    );
  });

  it("preserves a draft and explains a 409 after reloading the snapshot", async () => {
    const feedbackSession = makeSession({
      state: "AwaitingTrialFeedback",
      state_version: 10,
      trial: { run_id: "run-1", status: "completed", steps: [] },
    });
    apiMock.getSession.mockResolvedValue(feedbackSession);
    apiMock.sendCommand.mockRejectedValue(
      Object.assign(new Error("state version conflict"), { status: 409 }),
    );
    renderPage();

    const feedback = await screen.findByLabelText("预跑反馈");
    fireEvent.change(feedback, { target: { value: "保留我的草稿" } });
    fireEvent.click(screen.getByRole("button", { name: "提交反馈并重新预跑" }));

    expect(
      await screen.findByText(/页面状态已变化，已重新同步/),
    ).toBeInTheDocument();
    expect(screen.getByLabelText("预跑反馈")).toHaveValue("保留我的草稿");
    expect(apiMock.getSession).toHaveBeenCalledTimes(2);
  });

  it("does not overwrite an edited stage queue when a 409 refreshes it", async () => {
    apiMock.getSession
      .mockResolvedValueOnce(makeSession())
      .mockResolvedValueOnce(
        makeSession({
          state_version: 5,
          stages: [
            {
              stage_id: "stage-1",
              title: "服务端环节名称",
              description: "服务端新投影",
              status: "current",
            },
            {
              stage_id: "stage-2",
              title: "创建后续任务",
              description: "确认任务字段",
              status: "pending",
            },
          ],
        }),
      );
    apiMock.sendCommand.mockRejectedValue(
      Object.assign(new Error("state version conflict"), { status: 409 }),
    );
    renderPage();

    const firstStage = await screen.findByDisplayValue("确认名单范围");
    fireEvent.change(firstStage, { target: { value: "保留本地环节草稿" } });
    fireEvent.click(screen.getByRole("button", { name: "确认这 2 个环节" }));

    expect(
      await screen.findByText(/页面状态已变化，已重新同步/),
    ).toBeInTheDocument();
    expect(screen.getByDisplayValue("保留本地环节草稿")).toBeInTheDocument();
    expect(
      screen.queryByDisplayValue("服务端环节名称"),
    ).not.toBeInTheDocument();
  });

  it("refreshes the projection and reconnects SSE from the latest version", async () => {
    apiMock.getSession
      .mockResolvedValueOnce(makeSession({ state_version: 4 }))
      .mockResolvedValue(
        makeSession({
          state: "GeneratingQuestions",
          state_version: 5,
        }),
      );
    renderPage();

    await waitFor(() => expect(subscriptionCallbacks).toHaveLength(1));
    expect(subscriptionCallbacks[0].afterStateVersion).toBe(4);
    await act(async () => {
      subscriptionCallbacks[0].onError(new Error("stream ended"));
    });

    await waitFor(() => expect(apiMock.getSession).toHaveBeenCalledTimes(2));
    await waitFor(
      () => {
        expect(subscriptionCallbacks).toHaveLength(2);
      },
      { timeout: 1_000 },
    );
    expect(subscriptionCallbacks[1].afterStateVersion).toBe(5);
    expect(
      screen.getByText(/已刷新最新状态，正在尝试重新连接/),
    ).toBeInTheDocument();
  });

  it("never lets an older async snapshot roll state_version backward", async () => {
    let resolveRefresh: ((session: WPlusSopSession) => void) | undefined;
    apiMock.getSession
      .mockResolvedValueOnce(makeSession({ state_version: 4 }))
      .mockImplementationOnce(
        () =>
          new Promise<WPlusSopSession>((resolve) => {
            resolveRefresh = resolve;
          }),
      );
    renderPage();
    await waitFor(() => expect(subscriptionCallbacks).toHaveLength(1));

    act(() => {
      subscriptionCallbacks[0].onEvent({
        event_id: "evt-5",
        session_id: "sop-1",
        state_version: 5,
        kind: "lifecycle_progress",
        snapshot: makeSession({
          state: "GeneratingQuestions",
          state_version: 5,
        }),
      });
    });
    expect(
      await screen.findByRole("heading", { name: "正在生成问题" }),
    ).toBeInTheDocument();

    await act(async () => {
      subscriptionCallbacks[0].onError(new Error("stream ended"));
      resolveRefresh?.(makeSession({ state_version: 4 }));
    });
    expect(
      screen.getByRole("heading", { name: "正在生成问题" }),
    ).toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: "确认这 2 个环节" }),
    ).not.toBeInTheDocument();
  });

  it("ignores a recovery snapshot after navigating to another SOP session", async () => {
    let resolveOldRecovery: ((session: WPlusSopSession) => void) | undefined;
    apiMock.getSession
      .mockResolvedValueOnce(makeSession({ title: "旧会话" }))
      .mockImplementationOnce(
        () =>
          new Promise<WPlusSopSession>((resolve) => {
            resolveOldRecovery = resolve;
          }),
      )
      .mockResolvedValueOnce(
        makeSession({ session_id: "sop-2", title: "新会话" }),
      );
    renderPage({ withSessionSwitcher: true });

    await waitFor(() => expect(subscriptionCallbacks).toHaveLength(1));
    act(() => subscriptionCallbacks[0].onError(new Error("stream ended")));
    await waitFor(() => expect(apiMock.getSession).toHaveBeenCalledTimes(2));
    fireEvent.click(screen.getByRole("button", { name: "切换测试 Session" }));
    expect(
      await screen.findByRole("heading", { name: "新会话" }),
    ).toBeInTheDocument();

    await act(async () => {
      resolveOldRecovery?.(makeSession({ title: "旧会话覆盖" }));
    });
    expect(screen.getByRole("heading", { name: "新会话" })).toBeInTheDocument();
    expect(
      screen.queryByRole("heading", { name: "旧会话覆盖" }),
    ).not.toBeInTheDocument();
  });

  it("ignores an old-session recovery failure and aborts it on navigation", async () => {
    const oldRecovery = deferred<WPlusSopSession>();
    apiMock.getSession
      .mockResolvedValueOnce(makeSession({ title: "旧会话" }))
      .mockImplementationOnce(() => oldRecovery.promise)
      .mockResolvedValueOnce(
        makeSession({ session_id: "sop-2", title: "新会话" }),
      );
    renderPage({ withSessionSwitcher: true });

    await waitFor(() => expect(subscriptionCallbacks).toHaveLength(1));
    act(() => subscriptionCallbacks[0].onError(new Error("stream ended")));
    await waitFor(() => expect(apiMock.getSession).toHaveBeenCalledTimes(2));
    const recoverySignal = apiMock.getSession.mock.calls[1][1] as
      | AbortSignal
      | undefined;
    expect(recoverySignal).toBeInstanceOf(AbortSignal);
    expect(recoverySignal?.aborted).toBe(false);

    fireEvent.click(screen.getByRole("button", { name: "切换测试 Session" }));
    expect(
      await screen.findByRole("heading", { name: "新会话" }),
    ).toBeInTheDocument();
    expect(recoverySignal?.aborted).toBe(true);

    await act(async () => {
      oldRecovery.reject(
        Object.assign(new Error("old session not found"), { status: 404 }),
      );
      await oldRecovery.promise.catch(() => undefined);
    });
    expect(screen.getByRole("heading", { name: "新会话" })).toBeInTheDocument();
    expect(
      screen.queryByRole("heading", { name: "无法访问这个工作台" }),
    ).not.toBeInTheDocument();
  });

  it("removes old-session controls while the destination session loads", async () => {
    apiMock.getSession
      .mockResolvedValueOnce(
        makeSession({
          state: "AwaitingAnswer",
          state_version: 7,
          question_batch: {
            batch_id: "batch-old-controls",
            stage_id: "stage-1",
            questions: [
              {
                question_id: "q-old-controls",
                kind: "free_text",
                prompt: "旧会话输入",
                required: true,
              },
            ],
          },
        }),
      )
      .mockImplementationOnce(() => new Promise<WPlusSopSession>(() => {}));
    renderPage({ withSessionSwitcher: true });

    fireEvent.change(await screen.findByLabelText("旧会话输入"), {
      target: { value: "完整回答" },
    });
    const oldSubmit = screen.getByRole("button", {
      name: "提交本轮 1 个回答",
    });
    fireEvent.click(screen.getByRole("button", { name: "切换测试 Session" }));

    await waitFor(() =>
      expect(screen.queryByLabelText("旧会话输入")).not.toBeInTheDocument(),
    );
    fireEvent.click(oldSubmit);
    expect(apiMock.sendCommand).not.toHaveBeenCalled();
  });

  it("shows the active run trace without applying it as Session state", async () => {
    apiMock.getSession.mockResolvedValue(
      makeSession({
        state: "GeneratingQuestions",
        state_version: 5,
        trial: { run_id: "run-1", status: "planning", steps: [] },
      }),
    );
    renderPage();

    await screen.findByRole("region", { name: "实时运行过程" });
    await waitFor(() => expect(subscriptionCallbacks).toHaveLength(1));

    emitSafeStreamTrace({
      runId: "run-1",
      sequence: 1,
      summaryText:
        "message role=assistant type=message status=in_progress content_types=text content_chars=8 hidden=true",
      stateVersion: 99,
      truncated: true,
    });
    expect(
      await screen.findByText(/content_chars=8 hidden=true/),
    ).toBeInTheDocument();
    expect(apiMock.getSession).toHaveBeenCalledTimes(1);
    expect(
      screen.getByRole("heading", { name: "正在生成问题" }),
    ).toBeInTheDocument();
    expect(
      screen.getByText("较早内容已截断，仅显示最近片段。"),
    ).toBeInTheDocument();

    act(() => {
      subscriptionCallbacks[0].onEvent({
        event_id: "evt-6",
        session_id: "sop-1",
        state_version: 6,
        kind: "question_batch_presented",
        run_id: "run-2",
        snapshot: makeSession({
          state: "GeneratingQuestions",
          state_version: 6,
          trial: { run_id: "run-2", status: "planning", steps: [] },
        }),
      });
    });

    expect(
      screen.queryByText(/content_chars=8 hidden=true/),
    ).not.toBeInTheDocument();
    expect(await screen.findByText("等待返回内容…")).toBeInTheDocument();

    act(() => {
      subscriptionCallbacks[0].onEvent({
        event_id: "evt-7",
        session_id: "sop-1",
        state_version: 7,
        kind: "stage_proposal",
        run_id: "run-2",
        snapshot: makeSession({
          state: "AwaitingQueueConfirmation",
          state_version: 7,
          trial: { run_id: "run-2", status: "completed", steps: [] },
        }),
      });
    });

    expect(
      screen.queryByRole("region", { name: "实时运行过程" }),
    ).not.toBeInTheDocument();
  });

  it("streams assistant text and tool activity inline, then folds before the question card", async () => {
    apiMock.getSession.mockResolvedValue(
      makeSession({
        state: "GeneratingQuestions",
        state_version: 5,
        trial: { run_id: "run-1", status: "planning", steps: [] },
      }),
    );
    renderPage();

    await screen.findByRole("heading", { name: "正在生成问题" });
    await waitFor(() => expect(subscriptionCallbacks).toHaveLength(1));
    emitSafeStreamTrace({
      runId: "run-1",
      sequence: 2,
      summaryText: "正在核对客户范围。\n已完成范围核对。",
      entries: [
        {
          entry_id: "text-1",
          kind: "assistant_text",
          text: "正在核对客户范围。",
          status: "completed",
        },
        {
          entry_id: "tool-1",
          kind: "tool",
          tool_name: "execute_shell_command",
          status: "completed",
        },
        {
          entry_id: "text-2",
          kind: "assistant_text",
          text: "已完成范围核对。",
          status: "completed",
        },
      ],
    });

    const transcript = await screen.findByRole("region", {
      name: "实时运行过程",
    });
    expect(within(transcript).getByText("正在核对客户范围。")).toBeVisible();
    expect(within(transcript).getByText("执行操作")).toBeVisible();
    expect(within(transcript).getByText("已完成范围核对。")).toBeVisible();
    expect(
      within(transcript).queryByRole("progressbar", { name: "当前运行进度" }),
    ).not.toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: "查看实时返回内容（调试）" }),
    ).not.toBeInTheDocument();

    act(() => {
      subscriptionCallbacks[0].onEvent({
        event_id: "evt-6",
        session_id: "sop-1",
        state_version: 6,
        kind: "question_batch",
        run_id: "run-1",
        snapshot: makeSession({
          state: "AwaitingAnswer",
          state_version: 6,
          trial: { run_id: "run-1", status: "completed", steps: [] },
          question_batch: {
            batch_id: "batch-1",
            stage_id: "stage-1",
            questions: [
              {
                question_id: "q-1",
                kind: "single_select",
                prompt: "请选择客户范围",
                required: true,
                options: [{ option_id: "all", label: "全部客户" }],
              },
            ],
          },
        }),
      });
    });

    expect(
      screen.getByRole("button", { name: /展开运行过程/ }),
    ).toHaveAttribute("aria-expanded", "false");
    expect(screen.getByText("请选择客户范围")).toBeVisible();
    expect(
      within(transcript).queryByText("正在核对客户范围。"),
    ).not.toBeVisible();
  });

  async function renderGeneratingTracePage() {
    apiMock.getSession.mockResolvedValue(
      makeSession({
        state: "GeneratingTrial",
        state_version: 8,
        trial: { run_id: "run-1", status: "planning", steps: [] },
      }),
    );
    renderPage();
    return screen.findByRole("button", {
      name: /折叠运行过程/,
    });
  }

  it("lets the inline run transcript be folded and reopened", async () => {
    const trigger = await renderGeneratingTracePage();
    expect(trigger).toHaveAttribute("aria-expanded", "true");
    expect(await screen.findByText("等待返回内容…")).toBeInTheDocument();
    fireEvent.click(trigger);
    expect(trigger).toHaveAttribute("aria-expanded", "false");
    fireEvent.click(trigger);
    expect(trigger).toHaveAttribute("aria-expanded", "true");
  });

  it("ignores descending sequences and late events from an old run", async () => {
    apiMock.getSession.mockResolvedValue(
      makeSession({
        state: "GeneratingQuestions",
        state_version: 5,
        trial: { run_id: "run-1", status: "planning", steps: [] },
      }),
    );
    renderPage();

    await screen.findByRole("region", { name: "实时运行过程" });
    await waitFor(() => expect(subscriptionCallbacks).toHaveLength(1));

    emitSafeStreamTrace({
      runId: "run-1",
      sequence: 2,
      summaryText: "sequence=2",
    });
    expect(await screen.findByText("sequence=2")).toBeInTheDocument();

    emitSafeStreamTrace({
      runId: "run-1",
      sequence: 1,
      summaryText: "sequence=1",
    });
    expect(screen.queryByText("sequence=1")).not.toBeInTheDocument();

    act(() => {
      subscriptionCallbacks[0].onEvent({
        event_id: "evt-6",
        session_id: "sop-1",
        state_version: 6,
        kind: "question_batch_presented",
        run_id: "run-2",
        snapshot: makeSession({
          state: "GeneratingQuestions",
          state_version: 6,
          trial: { run_id: "run-2", status: "planning", steps: [] },
        }),
      });
    });

    expect(screen.queryByText("sequence=2")).not.toBeInTheDocument();
    expect(await screen.findByText("等待返回内容…")).toBeInTheDocument();
    emitSafeStreamTrace({
      runId: "run-1",
      sequence: 3,
      summaryText: "late-old-run",
    });
    expect(screen.queryByText("late-old-run")).not.toBeInTheDocument();
    expect(screen.getByText("等待返回内容…")).toBeInTheDocument();
  });

  it("follows new trace lines only while the viewer stays near the bottom", async () => {
    apiMock.getSession.mockResolvedValue(
      makeSession({
        state: "GeneratingTrial",
        state_version: 8,
        trial: { run_id: "run-1", status: "planning", steps: [] },
      }),
    );
    renderPage();
    await screen.findByRole("button", { name: /折叠运行过程/ });
    await waitFor(() => expect(subscriptionCallbacks).toHaveLength(1));

    emitSafeStreamTrace({
      runId: "run-1",
      sequence: 1,
      summaryText: "sequence=1",
      stateVersion: 8,
    });
    const trace = await screen.findByTestId("wplus-live-run-body");
    Object.defineProperties(trace, {
      scrollHeight: { configurable: true, value: 500 },
      clientHeight: { configurable: true, value: 100 },
      scrollTop: { configurable: true, writable: true, value: 400 },
    });
    fireEvent.scroll(trace);
    expect(trace.scrollTop).toBe(400);

    trace.scrollTop = 100;
    fireEvent.scroll(trace);
    emitSafeStreamTrace({
      runId: "run-1",
      sequence: 2,
      summaryText: "sequence=2",
      stateVersion: 8,
    });
    expect(await screen.findByText("sequence=2")).toBeInTheDocument();
    expect(trace.scrollTop).toBe(100);

    trace.scrollTop = 400;
    fireEvent.scroll(trace);
    emitSafeStreamTrace({
      runId: "run-1",
      sequence: 3,
      summaryText: "sequence=3",
      stateVersion: 8,
    });
    await waitFor(() => expect(trace.scrollTop).toBe(500));
  });

  it("clears the inline trace when the owning Session changes", async () => {
    apiMock.getSession.mockImplementation(async (requestedSessionId) =>
      makeSession({
        session_id: requestedSessionId,
        state: "GeneratingTrial",
        state_version: 8,
        trial: { run_id: "shared-run-id", status: "planning", steps: [] },
      }),
    );
    renderPage({ withSessionSwitcher: true });

    await screen.findByRole("button", { name: /折叠运行过程/ });
    await waitFor(() => expect(subscriptionCallbacks).toHaveLength(1));
    emitSafeStreamTrace({
      runId: "shared-run-id",
      sequence: 1,
      summaryText: "old-session-trace",
      stateVersion: 8,
    });
    expect(await screen.findByText("old-session-trace")).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "切换测试 Session" }));
    await waitFor(() =>
      expect(apiMock.getSession).toHaveBeenLastCalledWith(
        "sop-2",
        expect.any(AbortSignal),
      ),
    );
    const currentTrigger = screen.getByRole("button", {
      name: /折叠运行过程/,
    });
    expect(currentTrigger).toHaveAttribute("aria-expanded", "true");
    expect(screen.getByText("等待返回内容…")).toBeInTheDocument();

    await waitFor(() => expect(subscriptionCallbacks).toHaveLength(2));
    emitSafeStreamTrace({
      runId: "shared-run-id",
      sequence: 2,
      summaryText: "late-old-session-trace",
      stateVersion: 8,
      subscriptionIndex: 0,
    });
    expect(
      screen.queryByText("late-old-session-trace"),
    ).not.toBeInTheDocument();
  });

  it("preserves an edited queue when an SSE version gap reloads the snapshot", async () => {
    apiMock.getSession
      .mockResolvedValueOnce(makeSession({ state_version: 4 }))
      .mockResolvedValueOnce(
        makeSession({
          state_version: 6,
          stages: [
            {
              stage_id: "stage-1",
              title: "服务端覆盖名称",
              description: "新投影",
              status: "current",
            },
            {
              stage_id: "stage-2",
              title: "创建后续任务",
              description: "确认任务字段",
              status: "pending",
            },
          ],
        }),
      );
    renderPage();
    const firstStage = await screen.findByDisplayValue("确认名单范围");
    fireEvent.change(firstStage, { target: { value: "保留 gap 前的草稿" } });
    await waitFor(() => expect(subscriptionCallbacks).toHaveLength(1));

    act(() => {
      subscriptionCallbacks[0].onEvent({
        event_id: "evt-6",
        session_id: "sop-1",
        state_version: 6,
        kind: "stage_queue_confirmed",
        snapshot: null,
      });
    });

    await waitFor(() => expect(apiMock.getSession).toHaveBeenCalledTimes(2));
    expect(screen.getByDisplayValue("保留 gap 前的草稿")).toBeInTheDocument();
    expect(
      screen.queryByDisplayValue("服务端覆盖名称"),
    ).not.toBeInTheDocument();
  });

  it("downloads a completed artifact through the authenticated API", async () => {
    const createObjectURL = vi.fn(() => "blob:wplus-artifact");
    const revokeObjectURL = vi.fn();
    vi.stubGlobal("URL", { createObjectURL, revokeObjectURL });
    const click = vi
      .spyOn(HTMLAnchorElement.prototype, "click")
      .mockImplementation(() => {});
    apiMock.getSession.mockResolvedValue(
      makeSession({
        state: "Completed",
        state_version: 20,
        artifacts: [
          {
            artifact_id: "sop_render_md",
            name: "sop_render.md",
            format: "markdown",
            status: "validated",
            download_url: "/wplus-sop/sessions/sop-1/artifacts/sop_render_md",
          },
        ],
      }),
    );
    renderPage();

    fireEvent.click(
      await screen.findByRole("button", { name: "sop_render.md" }),
    );
    await waitFor(() =>
      expect(apiMock.downloadArtifact).toHaveBeenCalledWith(
        "sop-1",
        "sop_render_md",
        expect.any(AbortSignal),
      ),
    );
    expect(createObjectURL).toHaveBeenCalled();
    expect(click).toHaveBeenCalled();
    expect(revokeObjectURL).toHaveBeenCalledWith("blob:wplus-artifact");
  });

  it("tracks concurrent downloads independently", async () => {
    const htmlDownload = deferred<Blob>();
    const jsonDownload = deferred<Blob>();
    apiMock.downloadArtifact.mockImplementation((_sessionId, artifactId) =>
      artifactId === "sop_render_html"
        ? htmlDownload.promise
        : jsonDownload.promise,
    );
    apiMock.getSession.mockResolvedValue(
      makeSession({
        state: "Completed",
        artifacts: [
          {
            artifact_id: "sop_render_html",
            name: "sop_render.html",
            format: "html",
            status: "validated",
          },
          {
            artifact_id: "sop_spec",
            name: "sop_spec.json",
            format: "json",
            status: "validated",
          },
        ],
      }),
    );
    vi.stubGlobal("URL", {
      createObjectURL: vi.fn(() => "blob:download"),
      revokeObjectURL: vi.fn(),
    });
    vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(() => {});
    renderPage();

    const htmlButton = await screen.findByRole("button", {
      name: "sop_render.html",
    });
    const jsonButton = screen.getByRole("button", { name: "sop_spec.json" });
    fireEvent.click(htmlButton);
    fireEvent.click(jsonButton);
    expect(htmlButton).toHaveClass("ant-btn-loading");
    expect(jsonButton).toHaveClass("ant-btn-loading");

    htmlDownload.resolve(new Blob(["html"]));
    await waitFor(() => expect(htmlButton).not.toHaveClass("ant-btn-loading"));
    expect(jsonButton).toHaveClass("ant-btn-loading");
    jsonDownload.resolve(new Blob(["json"]));
    await waitFor(() => expect(jsonButton).not.toHaveClass("ant-btn-loading"));
  });

  it("aborts downloads on session switch and suppresses stale errors", async () => {
    let downloadSignal: AbortSignal | undefined;
    apiMock.downloadArtifact.mockImplementation(
      (_sessionId, _artifactId, signal) => {
        downloadSignal = signal;
        return new Promise((_resolve, reject) => {
          signal?.addEventListener("abort", () =>
            reject(new DOMException("Aborted", "AbortError")),
          );
        });
      },
    );
    apiMock.getSession.mockImplementation(async (requestedSessionId) =>
      makeSession({
        session_id: requestedSessionId,
        state: "Completed",
        artifacts: [
          {
            artifact_id: "sop_render_md",
            name: `${requestedSessionId}.md`,
            format: "markdown",
            status: "validated",
          },
        ],
      }),
    );
    renderPage({ withSessionSwitcher: true });

    fireEvent.click(await screen.findByRole("button", { name: "sop-1.md" }));
    await waitFor(() => expect(downloadSignal).toBeDefined());
    fireEvent.click(screen.getByRole("button", { name: "切换测试 Session" }));

    await waitFor(() => expect(downloadSignal?.aborted).toBe(true));
    expect(
      await screen.findByRole("button", { name: "sop-2.md" }),
    ).toBeVisible();
    expect(
      screen.queryByText("产物下载失败，请稍后重试。"),
    ).not.toBeInTheDocument();
  });

  it("shows the completed memory snapshot as read-only history", async () => {
    apiMock.getSession.mockResolvedValue(
      makeSession({
        state: "Completed",
        state_version: 24,
        memory_candidates: [
          {
            candidate_id: "legacy-approved",
            title: "旧版审批记录",
            content: "旧版自由文本",
            status: "approved",
            legacy_read_only: true,
          },
          {
            candidate_id: "modern-approved",
            title: "已写入的公共规则",
            memory_type: "common_wplus_knowledge",
            content: { rule: "复核高风险分组" },
            evidence: "用户确认该规则可以复用。",
            target_scope: "common",
            target_file: "memory/common-wplus-knowledge.jsonl",
            status: "approved",
            write_receipt: {
              memory_id: "wplus-sop/sop-1/modern-approved",
              target_scope: "common",
              target_file: "memory/common-wplus-knowledge.jsonl",
              written_at: "2026-08-04T10:00:00Z",
              reused_existing: false,
              store_result: "appended",
            },
          },
          {
            candidate_id: "rejected",
            title: "未保存的个人偏好",
            content: { preference: "只查看摘要" },
            status: "rejected",
          },
          {
            candidate_id: "failed",
            title: "写入失败的案例",
            content: { pattern: "失败案例" },
            status: "failed",
            failure_reason: "disk unavailable",
          },
        ],
      }),
    );

    renderPage();

    expect(
      await screen.findByRole("region", { name: "记忆处理历史" }),
    ).toBeInTheDocument();
    expect(
      screen.getByText("历史已批准（无可验证写入回执）"),
    ).toBeInTheDocument();
    expect(
      screen.getByText("wplus-sop/sop-1/modern-approved"),
    ).toBeInTheDocument();
    expect(screen.getByText("已拒绝")).toBeInTheDocument();
    expect(screen.getByText("写入失败：disk unavailable")).toBeInTheDocument();
    expect(screen.queryByRole("radio")).not.toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: "统一提交记忆选择" }),
    ).not.toBeInTheDocument();
  });

  it("offers explicit controls while a safe exit is pending", async () => {
    apiMock.getSession.mockResolvedValue(
      makeSession({
        state: "PendingExit",
        state_version: 12,
        pending_exit: {
          requested_action: "pause",
          requested_at: "2026-07-29T00:00:00Z",
        },
      }),
    );
    renderPage();

    expect(
      await screen.findByRole("heading", {
        name: "等待当前完整响应落盘",
      }),
    ).toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: "保存并退出" }),
    ).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "取消本轮并暂停" }));

    await waitFor(() =>
      expect(apiMock.sendCommand).toHaveBeenCalledWith(
        "sop-1",
        expect.objectContaining({
          command: "cancel_run_and_pause",
          expected_state_version: 12,
        }),
      ),
    );
  });

  it("renders a non-leaking unavailable state for 404", async () => {
    apiMock.getSession.mockRejectedValue(
      Object.assign(new Error("not found"), { status: 404 }),
    );
    renderPage();

    expect(
      await screen.findByRole("heading", { name: "无法访问这个工作台" }),
    ).toBeInTheDocument();
    expect(screen.queryByText("not found")).not.toBeInTheDocument();
  });

  it("shows the latest stage report and confirms the stage once validated", async () => {
    const stageReports: WPlusSopStageReport[] = [
      {
        stage_id: "stage-1",
        report_no: 1,
        revision: 1,
        superseded_by: 2,
        created_at: "2026-08-21T01:00:00Z",
        artifacts: [
          {
            artifact_id: "stage_sop_json",
            name: "stage_sop.json",
            format: "json",
            status: "validated",
            download_url: "/static/stage-1-v1.json",
            sha256: "a".repeat(64),
          },
          {
            artifact_id: "stage_sop_md",
            name: "stage_sop.md",
            format: "markdown",
            status: "validated",
            download_url: "/static/stage-1-v1.md",
            sha256: "a".repeat(64),
          },
          {
            artifact_id: "stage_sop_html",
            name: "stage_sop.html",
            format: "html",
            status: "validated",
            download_url: "/static/stage-1-v1.html",
            sha256: "a".repeat(64),
          },
        ],
      },
      {
        stage_id: "stage-1",
        report_no: 2,
        revision: 1,
        superseded_by: null,
        created_at: "2026-08-21T02:00:00Z",
        artifacts: [
          {
            artifact_id: "stage_sop_json",
            name: "stage_sop.json",
            format: "json",
            status: "validated",
            download_url: "/static/stage-1-v2.json",
            sha256: "b".repeat(64),
          },
          {
            artifact_id: "stage_sop_md",
            name: "stage_sop.md",
            format: "markdown",
            status: "validated",
            download_url: "/static/stage-1-v2.md",
            sha256: "b".repeat(64),
          },
          {
            artifact_id: "stage_sop_html",
            name: "stage_sop.html",
            format: "html",
            status: "validated",
            download_url: "/static/stage-1-v2.html",
            sha256: "b".repeat(64),
          },
        ],
      },
    ];
    apiMock.getSession.mockResolvedValue(
      makeSession({
        state: "AwaitingStageConfirmation",
        state_version: 8,
        stage_reports: stageReports,
        cumulative_preview: {
          preview_version: 1,
          stage_order: [],
          snapshots: [],
          artifacts: [],
          rendered_sha256: {},
        },
      }),
    );
    renderPage();

    expect(
      await screen.findByRole("heading", { name: /已通过预跑/ }),
    ).toBeInTheDocument();
    expect(screen.getByText("版本 v2（最新）")).toBeInTheDocument();
    expect(screen.getByText("最新版本")).toBeInTheDocument();
    await waitFor(() =>
      expect(apiMock.readStageReportArtifact).toHaveBeenCalledWith(
        "sop-1",
        {
          stageId: "stage-1",
          revision: 1,
          reportNo: 2,
          artifactId: "stage_sop_html",
        },
        expect.any(AbortSignal),
      ),
    );
    expect(
      screen.getByTitle("HTML 阶段 SOP v2 预览").getAttribute("srcdoc"),
    ).toContain("<article>阶段 SOP v2</article>");

    fireEvent.click(
      screen.getByRole("button", { name: "查看版本 v1（修订 1）" }),
    );
    await waitFor(() =>
      expect(apiMock.readStageReportArtifact).toHaveBeenCalledWith(
        "sop-1",
        expect.objectContaining({ revision: 1, reportNo: 1 }),
        expect.any(AbortSignal),
      ),
    );
    expect(screen.getByText("历史版本只读，不能用于确认环节。")).toBeVisible();

    const confirmButton = screen.getByRole("button", {
      name: "确认并锁定本环节",
    });
    expect(confirmButton).toBeDisabled();
    fireEvent.click(
      screen.getByRole("button", { name: "查看版本 v2（修订 1）" }),
    );
    expect(confirmButton).toBeEnabled();
    await waitFor(() => {
      fireEvent.click(confirmButton);
      expect(apiMock.sendCommand).toHaveBeenCalledWith(
        "sop-1",
        expect.objectContaining({ command: "confirm_stage" }),
      );
    });
  });

  it.each([
    ["补充澄清", "clarify"],
    ["按反馈重新预跑", "rerun"],
  ])(
    "submits stage report feedback through %s",
    async (buttonLabel, nextAction) => {
      apiMock.getSession.mockResolvedValue(
        makeSession({
          state: "AwaitingStageConfirmation",
          state_version: 8,
          trial: {
            run_id: "run-stage-report",
            status: "completed",
            steps: [],
          },
          stage_reports: [
            {
              stage_id: "stage-1",
              report_no: 1,
              revision: 1,
              superseded_by: null,
              created_at: "2026-08-21T01:00:00Z",
              artifacts: [
                {
                  artifact_id: "stage_sop_json",
                  name: "stage_sop.json",
                  format: "json",
                  status: "validated",
                },
                {
                  artifact_id: "stage_sop_md",
                  name: "stage_sop.md",
                  format: "markdown",
                  status: "validated",
                },
                {
                  artifact_id: "stage_sop_html",
                  name: "stage_sop.html",
                  format: "html",
                  status: "validated",
                },
              ],
            },
          ],
        }),
      );
      renderPage();

      fireEvent.change(await screen.findByLabelText("阶段 SOP 反馈"), {
        target: { value: "补充异常处理规则后继续" },
      });
      fireEvent.click(screen.getByRole("button", { name: buttonLabel }));

      await waitFor(() =>
        expect(apiMock.sendCommand).toHaveBeenCalledWith(
          "sop-1",
          expect.objectContaining({
            command: "submit_trial_feedback",
            payload: {
              feedback: "补充异常处理规则后继续",
              rerun_of_run_id: "run-stage-report",
              next_action: nextAction,
            },
          }),
        ),
      );
    },
  );

  it("switches stage preview formats, pretty prints JSON, and downloads the selected version", async () => {
    const createObjectURL = vi.fn(() => "blob:stage-artifact");
    const revokeObjectURL = vi.fn();
    vi.stubGlobal("URL", { createObjectURL, revokeObjectURL });
    vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(() => {});
    apiMock.readStageReportArtifact.mockImplementation(
      async (_sessionId, identity) =>
        identity.artifactId === "stage_sop_json"
          ? '{"title":"阶段 SOP","steps":[1,2]}'
          : identity.artifactId === "stage_sop_md"
          ? "# 阶段 SOP\n\n执行步骤"
          : "<article>阶段 SOP</article>",
    );
    apiMock.getSession.mockResolvedValue(
      makeSession({
        state: "AwaitingStageConfirmation",
        stage_reports: [
          {
            stage_id: "stage-1",
            report_no: 3,
            revision: 2,
            superseded_by: null,
            created_at: "2026-08-21T02:00:00Z",
            artifacts: [
              {
                artifact_id: "stage_sop_json",
                name: "stage_sop.json",
                format: "json",
                status: "validated",
                download_url: "/ignored",
              },
              {
                artifact_id: "stage_sop_md",
                name: "stage_sop.md",
                format: "markdown",
                status: "validated",
                download_url: "/ignored",
              },
              {
                artifact_id: "stage_sop_html",
                name: "stage_sop.html",
                format: "html",
                status: "validated",
                download_url: "/ignored",
              },
            ],
          },
        ],
      }),
    );
    renderPage();

    expect(await screen.findByTitle("HTML 阶段 SOP v3 预览")).toHaveAttribute(
      "sandbox",
      "",
    );
    fireEvent.click(screen.getByRole("button", { name: "JSON" }));
    expect(await screen.findByText(/"title": "阶段 SOP"/)).toBeVisible();
    fireEvent.click(screen.getByRole("button", { name: "下载 JSON" }));
    await waitFor(() =>
      expect(apiMock.downloadStageReportArtifact).toHaveBeenCalledWith(
        "sop-1",
        {
          stageId: "stage-1",
          revision: 2,
          reportNo: 3,
          artifactId: "stage_sop_json",
        },
        expect.any(AbortSignal),
      ),
    );
  });

  it("loads the cumulative preview by version and offers retry after an error", async () => {
    apiMock.readCumulativeArtifact
      .mockRejectedValueOnce(new Error("network"))
      .mockResolvedValueOnce("<article>累计 SOP 已恢复</article>");
    apiMock.getSession.mockResolvedValue(
      makeSession({
        state: "AwaitingStageConfirmation",
        stage_reports: [],
        cumulative_preview: {
          preview_version: 6,
          stage_order: ["stage-1"],
          snapshots: [
            {
              stage_id: "stage-1",
              report_no: 2,
              revision: 1,
              artifact_sha256: "a".repeat(64),
              confirmed_at: "2026-08-21T02:00:00Z",
            },
          ],
          artifacts: [
            {
              artifact_id: "cumulative_html",
              name: "cumulative.html",
              format: "html",
              status: "validated",
              download_url: "/ignored",
            },
          ],
          rendered_sha256: { html: "a".repeat(64) },
        },
      }),
    );
    renderPage();

    expect(await screen.findByText("HTML 预览加载失败。")).toBeVisible();
    expect(apiMock.readCumulativeArtifact).toHaveBeenCalledWith(
      "sop-1",
      { previewVersion: 6, artifactId: "cumulative_html" },
      expect.any(AbortSignal),
    );
    fireEvent.click(screen.getByRole("button", { name: "重试 HTML 预览" }));
    expect(
      (await screen.findByTitle("HTML 累计 SOP v6 预览")).getAttribute(
        "srcdoc",
      ),
    ).toContain("<article>累计 SOP 已恢复</article>");
  });

  it("hides the cumulative preview while the next stage is in progress", async () => {
    apiMock.getSession.mockResolvedValue(
      makeSession({
        state: "GeneratingQuestions",
        current_stage_id: "stage-2",
        stages: [
          {
            stage_id: "stage-1",
            title: "确认名单范围",
            description: "确定产品和时间窗口",
            status: "confirmed",
          },
          {
            stage_id: "stage-2",
            title: "创建后续任务",
            description: "确认任务字段",
            status: "current",
          },
        ],
        cumulative_preview: {
          preview_version: 1,
          stage_order: ["stage-1"],
          snapshots: [
            {
              stage_id: "stage-1",
              report_no: 2,
              revision: 1,
              artifact_sha256: "a".repeat(64),
              confirmed_at: "2026-08-21T02:00:00Z",
            },
          ],
          artifacts: [
            {
              artifact_id: "cumulative_html",
              name: "cumulative.html",
              format: "html",
              status: "validated",
            },
          ],
          rendered_sha256: { html: "a".repeat(64) },
        },
      }),
    );

    renderPage();

    expect(
      await screen.findByRole("heading", { name: "客户经营 SOP" }),
    ).toBeVisible();
    expect(screen.queryByText("累计 SOP 预览")).not.toBeInTheDocument();
    expect(apiMock.readCumulativeArtifact).not.toHaveBeenCalled();
  });

  it("blocks stage confirmation until the latest report is fully validated", async () => {
    apiMock.getSession.mockResolvedValue(
      makeSession({
        state: "AwaitingStageConfirmation",
        state_version: 8,
        stage_reports: [
          {
            stage_id: "stage-1",
            report_no: 1,
            revision: 1,
            superseded_by: null,
            created_at: "2026-08-21T01:00:00Z",
            artifacts: [
              {
                artifact_id: "stage_sop_json",
                name: "stage_sop.json",
                format: "json",
                status: "validated" as const,
                download_url: "/static/stage-1.json",
                sha256: "a".repeat(64),
              },
              {
                artifact_id: "stage_sop_md",
                name: "stage_sop.md",
                format: "markdown",
                status: "failed" as const,
                download_url: null,
                sha256: null,
              },
              {
                artifact_id: "stage_sop_html",
                name: "stage_sop.html",
                format: "html",
                status: "validated" as const,
                download_url: "/static/stage-1.html",
                sha256: "a".repeat(64),
              },
            ],
          },
        ],
      }),
    );
    renderPage();

    expect(await screen.findByText("环节报告尚未校验完成")).toBeInTheDocument();
    const confirmButton = screen.getByRole("button", {
      name: "确认并锁定本环节",
    });
    expect(confirmButton).toBeDisabled();
    expect(apiMock.sendCommand).not.toHaveBeenCalled();
  });
});
