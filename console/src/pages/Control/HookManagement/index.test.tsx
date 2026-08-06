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
  message: { success: vi.fn(), error: vi.fn(), warning: vi.fn() },
}));

vi.mock("@/api/modules/hookManagement", () => ({
  hookManagementApi: mocks,
}));

vi.mock("@/hooks/useAppMessage", () => ({
  useAppMessage: () => ({ message: mocks.message }),
}));

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
  });

  it("selects a Handler and exposes ordered argv fields", async () => {
    render(<HookManagementPage />);
    await openPreToolHandler();

    expect(screen.getByLabelText("命令参数 1")).toHaveValue("python");
    expect(screen.getByLabelText("命令参数 2")).toHaveValue(
      "hooks/scripts/guard.py",
    );
  });

  it("shows invalid script diagnostics without replacing the management page", async () => {
    mocks.getConfiguration.mockResolvedValueOnce({
      hooks,
      revision: "rev-1",
      diagnostics: [
        {
          event: "PreToolUse",
          group_id: "tool-guards",
          handler_id: "guard-shell",
          argument: "hooks/scripts/missing.py",
          reason: "script is not in the controlled library: missing.py",
        },
      ],
    });
    render(<HookManagementPage />);

    expect(
      await screen.findByRole("heading", { name: /Hook 管理/ }),
    ).toBeInTheDocument();
    expect(screen.getByText("Hook 脚本引用需要修复")).toBeInTheDocument();
    expect(
      screen.getByText(/PreToolUse · tool-guards · guard-shell/),
    ).toBeInTheDocument();
    expect(screen.getByText(/hooks\/scripts\/missing.py/)).toBeInTheDocument();
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
    expect(screen.getByLabelText("Shell")).toBeInTheDocument();
    expect(screen.getByLabelText("环境变量（JSON）")).toBeInTheDocument();
  });

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

    fireEvent.click(screen.getByRole("button", { name: "保存并激活" }));
    await waitFor(() => expect(mocks.saveConfiguration).toHaveBeenCalled());
    await waitFor(() =>
      expect(screen.queryByText("未保存更改")).not.toBeInTheDocument(),
    );
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
                { id: "second-handler", type: "command", argv: ["echo", "two"] },
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
    fireEvent.click(
      screen.getByRole("button", { name: "guard-shell 下移" }),
    );
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
