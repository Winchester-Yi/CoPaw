import "@testing-library/jest-dom/vitest";
import {
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const mocks = vi.hoisted(() => ({
  getConfiguration: vi.fn(),
  listScripts: vi.fn(),
  saveConfiguration: vi.fn(),
  uploadScripts: vi.fn(),
  manualTest: vi.fn(),
  distributeToDefaultAgents: vi.fn(),
  message: { success: vi.fn(), error: vi.fn(), warning: vi.fn() },
}));

vi.mock("@/api/modules/hookManagement", () => ({
  hookManagementApi: mocks,
}));

vi.mock("@/hooks/useAppMessage", () => ({
  useAppMessage: () => ({ message: mocks.message }),
}));

vi.mock("@/components/TenantSelector", () => ({
  TenantSelector: ({
    onChange,
  }: {
    onChange: (tenantIds: string[]) => void;
  }) => (
    <button type="button" onClick={() => onChange(["tenant-b"])}>
      选择目标租户
    </button>
  ),
}));

vi.mock("@/utils/identity", () => ({ getUserId: () => "tenant-a" }));

import HookManagementPage from ".";

async function openPreToolHandler(handlerId = "guard-shell") {
  fireEvent.click(
    await screen.findByRole("button", { name: "编辑配置 PreToolUse" }),
  );
  if (handlerId !== "guard-shell") {
    fireEvent.click(
      await screen.findByRole("button", { name: `编辑 ${handlerId}` }),
    );
  }
}

async function openPreToolGroup() {
  fireEvent.click(
    await screen.findByRole("button", { name: "编辑配置 PreToolUse" }),
  );
  fireEvent.click(await screen.findByRole("tab", { name: "适用范围" }));
  fireEvent.click(await screen.findByRole("button", { name: "所有工具" }));
}

async function openStopHandler(handlerId: string) {
  fireEvent.click(await screen.findByRole("button", { name: "编辑配置 Stop" }));
  fireEvent.click(
    await screen.findByRole("button", { name: `编辑 ${handlerId}` }),
  );
}

const hooks = {
  enabled: true,
  events: {
    PreToolUse: [
      {
        id: "tool-guards",
        matcher: { tools: [] },
        hooks: [
          {
            id: "guard-shell",
            type: "command",
            argv: ["python", "hooks/scripts/guard.py"],
          },
        ],
      },
    ],
  },
};

const stopHooks = {
  enabled: true,
  events: {
    Stop: [
      {
        id: "stop-output",
        matcher: { tools: [] },
        hooks: [
          {
            id: "stop-command",
            type: "command",
            argv: ["echo", "stop"],
            once: true,
          },
          { id: "stop-http", type: "http", url: "https://example.test" },
          { id: "stop-prompt", type: "prompt", prompt: "Review output" },
        ],
      },
    ],
  },
};

describe("HookManagementPage", () => {
  afterEach(cleanup);

  beforeEach(() => {
    vi.clearAllMocks();
    mocks.getConfiguration.mockResolvedValue({ hooks, revision: "rev-1" });
    mocks.listScripts.mockResolvedValue([
      { filename: "guard.py", size: 12, sha256: "a".repeat(64) },
    ]);
    mocks.saveConfiguration.mockResolvedValue({ hooks, revision: "rev-2" });
    mocks.uploadScripts.mockResolvedValue({
      accepted: [],
      warned: [],
      failed: [],
    });
    mocks.manualTest.mockResolvedValue({ redacted_summary: { status: "ok" } });
    mocks.distributeToDefaultAgents.mockResolvedValue({
      source_revision: "rev-1",
      results: [
        {
          tenant_id: "tenant-b",
          success: true,
          bootstrapped: false,
          matcher_group_ids: ["tool-guards"],
          script_names: ["guard.py"],
          error: "",
        },
      ],
    });
  });

  it("selects a Handler and exposes ordered argv fields", async () => {
    render(<HookManagementPage />);
    await openPreToolHandler();

    expect(screen.getByLabelText("命令参数 1")).toHaveValue("python");
    expect(screen.getByLabelText("命令参数 2")).toHaveValue(
      "hooks/scripts/guard.py",
    );
  });

  it("keeps a renamed Handler selected for continued editing", async () => {
    render(<HookManagementPage />);
    await openPreToolHandler();
    fireEvent.change(screen.getByLabelText("Handler ID"), {
      target: { value: "guard-shell-renamed" },
    });

    expect(screen.getByLabelText("Handler ID")).toHaveValue(
      "guard-shell-renamed",
    );
    expect(screen.getByLabelText("命令参数 1")).toHaveValue("python");
  });

  it("edits the selected Matcher Group and its tool matcher", async () => {
    render(<HookManagementPage />);
    await openPreToolGroup();

    expect(screen.getByLabelText("Matcher Group ID")).toHaveValue(
      "tool-guards",
    );
    expect(screen.getByLabelText("匹配工具（每行一个）")).toHaveValue("");
  });

  it("adds an empty event to the local draft", async () => {
    render(<HookManagementPage />);
    fireEvent.click(
      await screen.findByRole("button", { name: "新建规则 SessionStart" }),
    );

    expect(
      screen.getByRole("button", { name: "编辑配置 SessionStart" }),
    ).toBeInTheDocument();
  });

  it("exposes all supported common and command Handler fields", async () => {
    render(<HookManagementPage />);
    await openPreToolHandler();

    fireEvent.click(screen.getByText("高级设置"));

    expect(screen.getByLabelText("状态消息")).toBeInTheDocument();
    expect(screen.getByLabelText("仅执行一次")).toBeInTheDocument();
    expect(screen.getByLabelText("附带会话快照")).toBeInTheDocument();
    expect(screen.queryByLabelText("Shell")).not.toBeInTheDocument();
    expect(screen.getByText("Shell：bash")).toBeInTheDocument();
    expect(screen.getByLabelText("环境变量（JSON）")).toBeInTheDocument();
  });

  it.each(["stop-command", "stop-http", "stop-prompt"])(
    "shows final reply transformation only for Stop %s Handlers",
    async (handlerId) => {
      mocks.getConfiguration.mockResolvedValueOnce({
        hooks: stopHooks,
        revision: "rev-1",
      });
      render(<HookManagementPage />);

      await openStopHandler(handlerId);

      expect(
        screen.getByRole("switch", { name: "转换最终回复" }),
      ).toBeInTheDocument();
      expect(screen.getByText("高级设置")).toBeInTheDocument();
    },
  );

  it("does not show final reply transformation for non-Stop Handlers", async () => {
    render(<HookManagementPage />);
    await openPreToolHandler();

    expect(
      screen.queryByRole("switch", { name: "转换最终回复" }),
    ).not.toBeInTheDocument();
    expect(
      screen.queryByRole("switch", { name: "追加独立消息" }),
    ).not.toBeInTheDocument();
  });

  it.each(["stop-command", "stop-http", "stop-prompt"])(
    "makes append and transform mutually exclusive for %s",
    async (handlerId) => {
      mocks.getConfiguration.mockResolvedValueOnce({
        hooks: stopHooks,
        revision: "rev-1",
      });
      render(<HookManagementPage />);
      await openStopHandler(handlerId);
      fireEvent.click(screen.getByRole("switch", { name: "转换最终回复" }));
      fireEvent.click(screen.getByRole("switch", { name: "追加独立消息" }));
      expect(
        screen.getByRole("switch", { name: "转换最终回复" }),
      ).not.toBeChecked();
      fireEvent.click(screen.getByText("高级设置"));
      expect(screen.getByLabelText("仅执行一次")).toBeDisabled();
      expect(screen.getByLabelText("仅执行一次")).not.toBeChecked();
      expect(screen.getByRole("combobox", { name: "失败策略" })).toBeDisabled();
      expect(
        screen.getByText(
          "有效失败策略固定为 allow：跳过失败处理器，保留正文，不插入错误气泡。",
        ),
      ).toBeInTheDocument();
      expect(screen.getByText(/16 KiB/)).toBeInTheDocument();
      fireEvent.click(screen.getByRole("switch", { name: "转换最终回复" }));
      expect(
        screen.getByRole("switch", { name: "追加独立消息" }),
      ).not.toBeChecked();
      fireEvent.click(screen.getByRole("switch", { name: "追加独立消息" }));
      fireEvent.click(screen.getByRole("button", { name: "保存并激活 Stop" }));
      await waitFor(() =>
        expect(mocks.saveConfiguration).toHaveBeenCalledWith(
          expect.objectContaining({
            events: {
              Stop: [
                expect.objectContaining({
                  hooks: expect.arrayContaining([
                    expect.objectContaining({
                      id: handlerId,
                      outputAppend: true,
                      outputTransform: false,
                      once: false,
                    }),
                  ]),
                }),
              ],
            },
          }),
          "rev-1",
        ),
      );
    },
    10_000,
  );

  it("requires a non-empty assistant response for appended Stop manual tests", async () => {
    mocks.getConfiguration.mockResolvedValueOnce({
      hooks: stopHooks,
      revision: "rev-1",
    });
    render(<HookManagementPage />);
    await openStopHandler("stop-command");
    fireEvent.click(screen.getByRole("switch", { name: "追加独立消息" }));
    fireEvent.click(screen.getByRole("button", { name: "执行人工测试" }));
    const context = JSON.parse(
      (screen.getByLabelText("Hook Context（JSON）") as HTMLTextAreaElement)
        .value,
    );
    fireEvent.change(screen.getByLabelText("Hook Context（JSON）"), {
      target: {
        value: JSON.stringify({ ...context, assistant_response: " " }),
      },
    });
    fireEvent.click(screen.getByLabelText(/确认将执行真实/i));
    fireEvent.click(screen.getByRole("button", { name: "执行测试" }));
    expect(
      await screen.findByText("追加消息测试需要非空的 assistant_response"),
    ).toBeInTheDocument();
    expect(mocks.manualTest).not.toHaveBeenCalled();
  }, 10_000);

  it("shows only safe append diagnostics in the modal and page summary", async () => {
    mocks.getConfiguration.mockResolvedValueOnce({
      hooks: stopHooks,
      revision: "rev-1",
    });
    mocks.manualTest.mockResolvedValueOnce({
      redacted_summary: {
        output_append: true,
        status: "allowed",
        append_applied: true,
        append_bytes: 42,
        effective_fail_policy: "allow",
        duration_ms: 8,
        failed: false,
        failure_type: "",
        append_text: "private append content",
        assistant_response: "private answer",
      },
    });
    render(<HookManagementPage />);
    await openStopHandler("stop-command");
    fireEvent.click(screen.getByRole("switch", { name: "追加独立消息" }));
    fireEvent.click(screen.getByRole("tab", { name: "测试与发布" }));
    fireEvent.click(screen.getByRole("button", { name: "执行人工测试" }));
    fireEvent.click(screen.getByLabelText(/确认将执行真实/i));
    fireEvent.click(screen.getByRole("button", { name: "执行测试" }));
    expect(await screen.findAllByText("append_bytes: 42")).toHaveLength(2);
    expect(screen.getAllByText("output_append: true")).toHaveLength(2);
    expect(screen.getAllByText("append_applied: true")).toHaveLength(2);
    expect(screen.getAllByText("effective_fail_policy: allow")).toHaveLength(2);
    expect(screen.getAllByText("duration_ms: 8")).toHaveLength(2);
    expect(document.body.textContent).not.toContain("private append content");
    expect(document.body.textContent).not.toContain("private answer");
  }, 10_000);

  it("enabling final reply transformation clears and disables once before saving", async () => {
    mocks.getConfiguration.mockResolvedValueOnce({
      hooks: stopHooks,
      revision: "rev-1",
    });
    render(<HookManagementPage />);
    await openStopHandler("stop-command");

    fireEvent.click(screen.getByRole("switch", { name: "转换最终回复" }));
    fireEvent.click(screen.getByText("高级设置"));

    expect(screen.getByLabelText("仅执行一次")).not.toBeChecked();
    expect(screen.getByLabelText("仅执行一次")).toBeDisabled();
    expect(screen.getByLabelText("仅执行一次")).toHaveAttribute(
      "title",
      expect.stringContaining("转换最终回复"),
    );
    expect(screen.getByLabelText("仅执行一次")).toHaveAttribute(
      "aria-describedby",
      "once-output-transform-reason",
    );
    expect(
      screen.getByText("转换最终回复开启后，必须在每次 Stop 时执行。"),
    ).toBeInTheDocument();
    expect(
      screen.getByText(
        '有效替换格式为 decision: "allow"，可选 hookSpecificOutput.replacementText。',
      ),
    ).toBeInTheDocument();
    expect(
      screen.getByText(
        "处理器执行失败或输出无效时遵循失败策略：allow 保留当前文本继续，block 终止。",
      ),
    ).toBeInTheDocument();
    expect(
      screen.getByText(
        "Prompt 返回仍仅支持 allow；command/http 的 Stop 返回 block 会终止。",
      ),
    ).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "保存并激活 Stop" }));
    await waitFor(() =>
      expect(mocks.saveConfiguration).toHaveBeenCalledWith(
        expect.objectContaining({
          events: expect.objectContaining({
            Stop: [
              expect.objectContaining({
                hooks: expect.arrayContaining([
                  expect.objectContaining({
                    id: "stop-command",
                    outputTransform: true,
                    once: false,
                  }),
                ]),
              }),
            ],
          }),
        }),
        "rev-1",
      ),
    );
  }, 10_000);

  it("requires a non-empty assistant response for transformed Stop manual tests", async () => {
    mocks.getConfiguration.mockResolvedValueOnce({
      hooks: {
        ...stopHooks,
        events: {
          Stop: [
            {
              ...stopHooks.events.Stop[0],
              hooks: [
                {
                  ...stopHooks.events.Stop[0].hooks[0],
                  outputTransform: true,
                },
              ],
            },
          ],
        },
      },
      revision: "rev-1",
    });
    render(<HookManagementPage />);
    await openStopHandler("stop-command");
    fireEvent.click(screen.getByRole("button", { name: "执行人工测试" }));
    fireEvent.change(screen.getByLabelText("Hook Context（JSON）"), {
      target: {
        value: JSON.stringify({
          session_id: "session-1",
          transcript_path: "/tmp/transcript.jsonl",
          cwd: "/workspace",
          tenant_id: "tenant-a",
          effective_tenant_id: "tenant-a",
          user_id: "user-a",
          agent_id: "agent-a",
          channel: "cli",
          hook_event_name: "Stop",
          assistant_response: " ",
        }),
      },
    });
    fireEvent.click(screen.getByLabelText(/确认将执行真实/i));
    fireEvent.click(screen.getByRole("button", { name: "执行测试" }));

    expect(
      await screen.findByText("输出转换测试需要非空的 assistant_response"),
    ).toBeInTheDocument();
    expect(mocks.manualTest).not.toHaveBeenCalled();
  }, 10_000);

  it("shows a redacted transformed manual-test summary", async () => {
    mocks.getConfiguration.mockResolvedValueOnce({
      hooks: {
        ...stopHooks,
        events: {
          Stop: [
            {
              ...stopHooks.events.Stop[0],
              hooks: [
                {
                  ...stopHooks.events.Stop[0].hooks[0],
                  outputTransform: true,
                },
              ],
            },
          ],
        },
      },
      revision: "rev-1",
    });
    mocks.manualTest.mockResolvedValueOnce({
      redacted_summary: {
        status: "allowed",
        replacement_applied: true,
        replacement_length: 14,
        failed: false,
        failure_type: "",
        output_transform: true,
        candidate_text: "candidate must never be shown",
        replacement_text: "replacement must never be shown",
      },
    });
    render(<HookManagementPage />);
    await openStopHandler("stop-command");
    fireEvent.click(screen.getByRole("button", { name: "执行人工测试" }));
    fireEvent.click(screen.getByLabelText(/确认将执行真实/i));
    fireEvent.click(screen.getByRole("button", { name: "执行测试" }));

    expect(await screen.findByText("status: allowed")).toBeInTheDocument();
    expect(screen.getByText("replacement_applied: true")).toBeInTheDocument();
    expect(screen.getByText("replacement_length: 14")).toBeInTheDocument();
    expect(screen.getByText("failed: false")).toBeInTheDocument();
    expect(screen.getByText(/^failure_type:/)).toBeInTheDocument();
    expect(
      screen.getByText("仅执行当前处理器，不模拟转换链路或总时间预算。"),
    ).toBeInTheDocument();
    expect(
      screen.queryByText("candidate must never be shown"),
    ).not.toBeInTheDocument();
    expect(
      screen.queryByText("replacement must never be shown"),
    ).not.toBeInTheDocument();
  }, 10_000);

  it("uploads a newly selected script without waiting for state to settle", async () => {
    render(<HookManagementPage />);
    fireEvent.click(await screen.findByRole("tab", { name: "脚本库" }));
    const file = new File(["print('ok')"], "new-hook.py", {
      type: "text/x-python",
    });

    fireEvent.change(screen.getByLabelText("选择 Hook 脚本文件"), {
      target: { files: [file] },
    });

    await waitFor(() =>
      expect(mocks.uploadScripts).toHaveBeenCalledWith([file], []),
    );
  });

  it("does not retain JSON field values when switching Handlers", async () => {
    mocks.getConfiguration.mockResolvedValueOnce({
      revision: "rev-1",
      hooks: {
        enabled: true,
        events: {
          PreToolUse: [
            {
              id: "tool-guards",
              matcher: { tools: [] },
              hooks: [
                {
                  id: "first-command",
                  type: "command",
                  argv: ["echo"],
                  env: { FIRST: "one" },
                },
                {
                  id: "second-command",
                  type: "command",
                  argv: ["echo"],
                  env: { SECOND: "two" },
                },
              ],
            },
          ],
        },
      },
    });
    render(<HookManagementPage />);

    await openPreToolHandler("first-command");
    expect(
      (screen.getByLabelText("环境变量（JSON）") as HTMLTextAreaElement).value,
    ).toContain("FIRST");

    fireEvent.click(
      screen.getByRole("button", { name: "编辑 second-command" }),
    );
    expect(
      (screen.getByLabelText("环境变量（JSON）") as HTMLTextAreaElement).value,
    ).toContain("SECOND");
  }, 10_000);

  it("requires confirmation before submitting a real manual test", async () => {
    render(<HookManagementPage />);
    await openPreToolHandler();
    fireEvent.click(screen.getByRole("button", { name: "执行人工测试" }));

    const execute = screen.getByRole("button", { name: "执行测试" });
    expect(execute).toBeDisabled();

    fireEvent.click(screen.getByLabelText(/确认将执行真实/i));
    fireEvent.click(execute);

    await waitFor(() => expect(mocks.manualTest).toHaveBeenCalled());
    expect(mocks.manualTest).toHaveBeenCalledWith(
      expect.objectContaining({ id: "guard-shell" }),
      expect.objectContaining({ hook_event_name: "PreToolUse" }),
    );
  }, 10_000);

  it("shows invalid manual-test Context errors inside the test dialog", async () => {
    render(<HookManagementPage />);
    await openPreToolHandler();
    fireEvent.click(screen.getByRole("button", { name: "执行人工测试" }));
    fireEvent.change(screen.getByLabelText("Hook Context（JSON）"), {
      target: { value: "not-json" },
    });
    fireEvent.click(screen.getByLabelText(/确认将执行真实/i));
    const execute = screen.getByRole("button", { name: "执行测试" });
    await waitFor(() => expect(execute).toBeEnabled());
    fireEvent.click(execute);

    expect(
      await screen.findByText("Hook Context 必须是有效 JSON"),
    ).toBeInTheDocument();
  }, 10_000);

  it("keeps a draft and offers reload when saving conflicts", async () => {
    mocks.saveConfiguration.mockRejectedValueOnce(
      Object.assign(new Error("stale"), { status: 409 }),
    );
    render(<HookManagementPage />);

    fireEvent.click(await screen.findByRole("button", { name: "保存并激活" }));

    expect(await screen.findByText("配置已被更新")).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: "重新加载最新配置" }),
    ).toBeEnabled();
  });

  it("shows configured and empty events without rendering the configuration tree", async () => {
    render(<HookManagementPage />);

    expect(
      await screen.findByRole("heading", { name: /Hook 管理/ }),
    ).toBeInTheDocument();
    expect(screen.getAllByText("PreToolUse")).toHaveLength(2);
    expect(screen.getByText("处理器数量")).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: "新建规则 SessionStart" }),
    ).toBeInTheDocument();
    expect(screen.queryByText("事件与处理链")).not.toBeInTheDocument();
  });

  it("shows hook health, lifecycle and processor chains in the overview", async () => {
    render(<HookManagementPage />);

    expect(await screen.findByText("Hook 已启用")).toBeInTheDocument();
    expect(screen.getByText("已配置事件")).toBeInTheDocument();
    expect(screen.getByText("处理器数量")).toBeInTheDocument();
    expect(screen.getByText("生命周期总览")).toBeInTheDocument();
    expect(screen.getAllByText("PreToolUse")).toHaveLength(2);
    expect(screen.getByText("Command")).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: "编辑配置 PreToolUse" }),
    ).toBeInTheDocument();
  });

  it("marks a changed configuration as unsaved until it is saved", async () => {
    render(<HookManagementPage />);

    fireEvent.click(await screen.findByRole("switch", { name: "启用 Hook" }));
    expect(screen.getByText("未保存更改")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "分发 Hook" })).toBeDisabled();

    fireEvent.click(screen.getByRole("button", { name: "保存并激活" }));
    await waitFor(() => expect(mocks.saveConfiguration).toHaveBeenCalled());
    await waitFor(() =>
      expect(screen.queryByText("未保存更改")).not.toBeInTheDocument(),
    );
  });

  it("distributes selected Matcher Groups to selected tenants", async () => {
    render(<HookManagementPage />);

    fireEvent.click(await screen.findByRole("button", { name: "分发 Hook" }));
    fireEvent.click(screen.getByLabelText("选择 tool-guards"));
    fireEvent.click(screen.getByRole("button", { name: "选择目标租户" }));
    expect(
      screen.getByText(
        "分发后将开启目标 Default Agent 的 Hook 总开关，目标已有分组也会按规则参与执行。",
      ),
    ).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "分发并启用" }));

    await waitFor(() =>
      expect(mocks.distributeToDefaultAgents).toHaveBeenCalledWith({
        matcherGroupIds: ["tool-guards"],
        targetTenantIds: ["tenant-b"],
      }),
    );
    expect(mocks.message.success).toHaveBeenCalledWith("已成功分发到 1 个租户");
  });

  it("removes an event from the drawer after explicit confirmation", async () => {
    render(<HookManagementPage />);

    fireEvent.click(
      await screen.findByRole("button", { name: "编辑配置 PreToolUse" }),
    );
    fireEvent.click(screen.getByRole("button", { name: "删除事件" }));
    fireEvent.click(await screen.findByRole("button", { name: "确认删除" }));

    expect(
      await screen.findByRole("button", { name: "新建规则 PreToolUse" }),
    ).toBeInTheDocument();
  }, 10_000);

  it("creates a scenario event from the new-event flow", async () => {
    render(<HookManagementPage />);

    fireEvent.click(
      await screen.findByRole("button", { name: "新建 Hook 规则" }),
    );
    fireEvent.click(screen.getByRole("button", { name: "从场景模板开始" }));
    fireEvent.click(screen.getByRole("button", { name: /工具调用审计/ }));

    expect(screen.getByLabelText("编辑 PostToolUse")).toBeInTheDocument();
    expect(screen.getAllByText("工具调用审计")).toHaveLength(2);
    expect(screen.getByText("执行顺序")).toBeInTheDocument();
  }, 10_000);

  it("moves a Handler down while preserving its event and group", async () => {
    mocks.getConfiguration.mockResolvedValueOnce({
      revision: "rev-1",
      hooks: {
        enabled: true,
        events: {
          PreToolUse: [
            {
              id: "tool-guards",
              matcher: { tools: [] },
              hooks: [
                { id: "guard-shell", type: "command", argv: ["echo", "one"] },
                {
                  id: "second-handler",
                  type: "command",
                  argv: ["echo", "two"],
                },
              ],
            },
          ],
        },
      },
    });
    render(<HookManagementPage />);

    fireEvent.click(
      await screen.findByRole("button", { name: "编辑配置 PreToolUse" }),
    );
    fireEvent.click(screen.getByRole("button", { name: "guard-shell 下移" }));
    fireEvent.click(
      screen.getByRole("button", { name: "保存并激活 PreToolUse" }),
    );

    await waitFor(() =>
      expect(mocks.saveConfiguration).toHaveBeenCalledWith(
        expect.objectContaining({
          events: expect.objectContaining({
            PreToolUse: [
              expect.objectContaining({
                hooks: [
                  expect.objectContaining({ id: "second-handler" }),
                  expect.objectContaining({ id: "guard-shell" }),
                ],
              }),
            ],
          }),
        }),
        "rev-1",
      ),
    );
  }, 10_000);

  it("opens a four-section event workspace with a processor pipeline", async () => {
    render(<HookManagementPage />);
    fireEvent.click(
      await screen.findByRole("button", { name: "编辑配置 PreToolUse" }),
    );

    expect(screen.getByLabelText("编辑 PreToolUse")).toBeInTheDocument();
    expect(screen.getByRole("tab", { name: "基本设置" })).toBeInTheDocument();
    expect(screen.getByRole("tab", { name: "适用范围" })).toBeInTheDocument();
    expect(screen.getByRole("tab", { name: "处理器编排" })).toBeInTheDocument();
    expect(screen.getByRole("tab", { name: "测试与发布" })).toBeInTheDocument();
    expect(screen.getByText("执行顺序")).toBeInTheDocument();
  });
});
