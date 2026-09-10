import React from "react";
import {
  cleanup,
  createEvent,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import WelcomeCenterLayout from "./index";
import { chatApi } from "@/api/modules/chat";
import { scenarioPresetApi } from "@/api/modules/scenarioPreset";
import {
  CHAT_INPUT_APPEND_TEXT_EVENT,
  CHAT_INPUT_REPLACE_TEXT_EVENT,
} from "../chatInputDraft";
import quickMenuStyles from "../ComposerQuickMenu/index.module.less";

const quickMenuItemSpy = vi.fn();

vi.mock("@agentscope-ai/icons", () => ({
  SparkAttachmentLine: () => <span data-testid="attachment-icon" />,
}));

vi.mock("@agentscope-ai/design", () => ({
  IconButton: () => <button type="button">upload</button>,
}));

vi.mock("@/components/agentscope-chat", () => ({
  Attachments: ({ items }: { items: Array<{ name?: string }> }) => (
    <div>
      {items.map((item) => (
        <span key={item.name}>{item.name}</span>
      ))}
    </div>
  ),
  useChatAnywhereInput: (selector: (value: { disabled: boolean }) => unknown) =>
    selector({ disabled: false }),
}));

vi.mock("@/components/agentscope-chat/ComposerQuickMenu", () => ({
  default: ({ children }: { children?: React.ReactNode }) => (
    <div data-testid="welcome-quick-menu">{children}</div>
  ),
  ComposerQuickMenuItem: (props: { label: React.ReactNode }) => {
    quickMenuItemSpy(props);
    return <span>{props.label}</span>;
  },
}));

vi.mock("@/components/GlobalVoiceRecorder/VoiceRecorderQuickMenuItem", () => ({
  default: () => <span data-testid="voice-recorder-trigger">语音录制</span>,
}));

vi.mock("@/components/GlobalVoiceRecorder/context", () => ({
  useVoiceRecorderTrigger: () => ({
    disabled: false,
    label: "开始语音录制",
    loading: false,
    panelOpen: false,
    recording: false,
    unsupported: false,
    trigger: () => undefined,
  }),
}));

vi.mock("react-i18next", () => ({
  useTranslation: () => ({
    t: (key: string) => key,
  }),
}));

vi.mock("@/api/modules/chat", () => ({
  chatApi: {
    uploadFile: vi.fn(),
    filePreviewUrl: vi.fn((filename: string) => `/preview/${filename}`),
  },
}));

vi.mock("@/api/modules/scenarioPreset", () => ({
  scenarioPresetApi: { getEffectiveCatalog: vi.fn() },
}));

vi.mock("../FeaturedCases", () => ({
  default: () => <div data-testid="featured-cases" />,
}));

vi.mock("../CaseDetailDrawer", () => ({
  default: () => null,
}));

vi.mock("@/api/modules/featuredCases", () => ({
  featuredCasesApi: {
    getCaseDetail: vi.fn(),
  },
}));

const mockedUploadFile = vi.mocked(chatApi.uploadFile);
const getEffectiveCatalog = vi.mocked(scenarioPresetApi.getEffectiveCatalog);
const skills = [
  {
    id: "skill:browser",
    type: "skill" as const,
    label: "browser",
    name: "browser",
    description: "Use a browser",
  },
  {
    id: "skill:Build",
    type: "skill" as const,
    label: "Build",
    name: "Build",
    description: "Build an app",
  },
];

function setTokenEditorValue(input: HTMLElement, value: string) {
  input.textContent = value;
  fireEvent.input(input);
}

describe("WelcomeCenterLayout", () => {
  afterEach(cleanup);

  beforeEach(() => {
    vi.clearAllMocks();
    quickMenuItemSpy.mockClear();
    mockedUploadFile.mockResolvedValue({
      url: "demo.txt",
      file_name: "demo.txt",
    });
    getEffectiveCatalog.mockResolvedValue({ domains: [] });
  });

  it("sets the welcome input card width before runtime styles are injected", () => {
    const { container } = render(
      <WelcomeCenterLayout greeting="你好" onSubmit={vi.fn()} />,
    );

    expect(container.querySelector(".welcome-input-card")).toHaveStyle({
      boxSizing: "border-box",
      maxWidth: "100%",
      width: "840px",
    });
  });

  it("temporarily hides dictation from the welcome input", () => {
    render(<WelcomeCenterLayout greeting="你好" onSubmit={vi.fn()} />);

    expect(
      screen.queryByRole("button", { name: "语音输入" }),
    ).not.toBeInTheDocument();
  });

  it("keeps the selector width stable when the scenario catalog finishes loading", async () => {
    let resolveCatalog!: (catalog: {
      domains: Array<{
        id: string;
        name: string;
        capabilities: Array<{
          id: string;
          name: string;
          scenarios: [];
        }>;
      }>;
    }) => void;
    getEffectiveCatalog.mockReturnValueOnce(
      new Promise((resolve) => {
        resolveCatalog = resolve;
      }),
    );

    const { container } = render(
      <WelcomeCenterLayout greeting="你好" onSubmit={vi.fn()} />,
    );

    resolveCatalog({
      domains: [
        {
          id: "domain-a",
          name: "文档处理",
          capabilities: [
            { id: "capability-a", name: "信息提取", scenarios: [] },
          ],
        },
      ],
    });
    await waitFor(() => {
      expect(
        container.querySelector(".scenario-preset-selector"),
      ).toBeInTheDocument();
    });

    expect(container.querySelector(".welcome-input-card")).toHaveStyle({
      width: "840px",
    });
    expect(container.querySelector(".scenario-preset-selector")).toHaveStyle({
      width: "840px",
    });
  });

  it("handles files dispatched by the chat drag-and-drop bridge", async () => {
    const file = new File(["hello"], "demo.txt", { type: "text/plain" });

    render(<WelcomeCenterLayout greeting="你好" onSubmit={vi.fn()} />);

    document.dispatchEvent(
      new CustomEvent("pasteFile", {
        detail: { file },
      }),
    );

    expect(mockedUploadFile).toHaveBeenCalledWith(file);
    await waitFor(() => {
      expect(screen.getByText("demo.txt")).toBeInTheDocument();
    });
  });

  it("combines upload, voice recording, and caller actions in one quick menu", () => {
    render(
      <WelcomeCenterLayout
        greeting="你好"
        onSubmit={vi.fn()}
        quickMenuItems={<span>计划模式</span>}
      />,
    );

    expect(screen.getByTestId("welcome-quick-menu")).toHaveTextContent(
      "chat.quickMenu.upload",
    );
    expect(screen.getByTestId("welcome-quick-menu")).toHaveTextContent(
      "计划模式",
    );
    expect(screen.getByTestId("welcome-quick-menu")).toContainElement(
      screen.getByTestId("voice-recorder-trigger"),
    );
    expect(quickMenuItemSpy).toHaveBeenCalledWith(
      expect.objectContaining({
        interactive: true,
        label: "chat.quickMenu.upload",
      }),
    );
    expect(
      screen
        .getByText("chat.quickMenu.upload")
        .closest(`.${quickMenuStyles.uploadTrigger}`),
    ).toBeInTheDocument();
  });

  it("appends transcribed text to the welcome draft without submitting", async () => {
    const onSubmit = vi.fn();
    render(<WelcomeCenterLayout greeting="你好" onSubmit={onSubmit} />);

    const input = screen.getByRole("textbox");
    fireEvent.change(input, { target: { value: "已有草稿" } });
    document.dispatchEvent(
      new CustomEvent(CHAT_INPUT_APPEND_TEXT_EVENT, {
        detail: { content: "语音转写" },
      }),
    );

    await waitFor(() => {
      expect(input).toHaveValue("已有草稿\n语音转写");
    });
    expect(onSubmit).not.toHaveBeenCalled();
  });

  it("replaces the welcome draft without submitting", async () => {
    const onSubmit = vi.fn();
    render(<WelcomeCenterLayout greeting="你好" onSubmit={onSubmit} />);

    const input = screen.getByRole("textbox");
    fireEvent.change(input, { target: { value: "需要清空的原草稿" } });
    document.dispatchEvent(
      new CustomEvent(CHAT_INPUT_REPLACE_TEXT_EVENT, {
        detail: { content: "完整 SOP 提示词" },
      }),
    );

    await waitFor(() => {
      expect(input).toHaveValue("完整 SOP 提示词");
    });
    expect(onSubmit).not.toHaveBeenCalled();
  });

  it("immediately fills a scenario draft and submits the second-level capability marker", async () => {
    getEffectiveCatalog.mockResolvedValueOnce({
      domains: [
        {
          id: "domain-a",
          name: "文档处理",
          capabilities: [
            {
              id: "capability-a",
              name: "信息提取",
              scenarios: [
                {
                  id: "scenario-a",
                  name: "提取字段",
                  prompt_draft: "提取这份文件的字段",
                },
              ],
            },
          ],
        },
      ],
    });
    const onSubmit = vi.fn();
    render(<WelcomeCenterLayout greeting="你好" onSubmit={onSubmit} />);

    fireEvent.click(await screen.findByRole("button", { name: "提取字段" }));

    const input = screen.getByRole("textbox");
    expect(input).toHaveTextContent("@信息提取提取这份文件的字段");

    fireEvent.click(screen.getByRole("button", { name: "发送" }));
    await waitFor(() => {
      expect(onSubmit).toHaveBeenCalledWith({
        fileList: [],
        query: "@信息提取 提取这份文件的字段",
      });
    });
  });

  it("uses the compact recommendation strip treatment in the three-level composer", async () => {
    getEffectiveCatalog.mockResolvedValueOnce({
      domains: [
        {
          id: "domain-a",
          name: "文档处理",
          capabilities: [
            {
              id: "capability-a",
              name: "信息提取",
              scenarios: [
                {
                  id: "scenario-a",
                  name: "提取字段",
                  prompt_draft: "提取",
                },
              ],
            },
          ],
        },
      ],
    });

    render(<WelcomeCenterLayout greeting="你好" onSubmit={vi.fn()} />);

    const capabilityRow = await screen.findByRole("tablist", { name: "能力" });
    const sceneStrip = screen.getByLabelText("推荐场景");
    const capabilityStyle = getComputedStyle(capabilityRow);
    const sceneStyle = getComputedStyle(sceneStrip);

    expect(capabilityStyle.borderTopStyle).toBe("none");
    expect(sceneStyle.minHeight).toBe("44px");
    expect(sceneStyle.marginBottom).toBe("0px");
    expect(sceneStyle.borderBottomStyle).toBe("solid");
    expect(sceneStyle.borderLeftStyle).toBe("none");
    expect(sceneStyle.borderRightStyle).toBe("none");
    expect(sceneStyle.borderTopLeftRadius).toBe("12px");
    expect(sceneStyle.borderTopRightRadius).toBe("12px");
  });

  it("renders centered domain segments and pill-shaped capability and scene controls", async () => {
    getEffectiveCatalog.mockResolvedValueOnce({
      domains: [
        {
          id: "domain-a",
          name: "文档处理",
          capabilities: [
            {
              id: "capability-a",
              name: "信息提取",
              scenarios: [
                {
                  id: "scenario-a",
                  name: "提取字段",
                  prompt_draft: "提取",
                },
              ],
            },
          ],
        },
        {
          id: "domain-b",
          name: "数据分析",
          capabilities: [],
        },
      ],
    });

    render(<WelcomeCenterLayout greeting="你好" onSubmit={vi.fn()} />);

    const domainSelector = await screen.findByRole("tablist", {
      name: "能力域",
    });
    const domainTrack = domainSelector.querySelector(
      ".scenario-preset-domain-track",
    );
    const activeDomain = screen.getByRole("tab", { name: "文档处理" });
    const capability = screen.getByRole("tab", { name: "信息提取" });
    const scene = screen.getByRole("button", { name: "提取字段" });

    expect(domainSelector).toHaveClass("scenario-preset-domain-selector");
    expect(domainTrack).toBeInTheDocument();
    expect(getComputedStyle(domainSelector).justifyContent).toBe("center");
    expect(getComputedStyle(domainTrack as Element).borderTopLeftRadius).toBe(
      "999px",
    );
    expect(getComputedStyle(activeDomain).borderTopLeftRadius).toBe("999px");
    expect(getComputedStyle(capability).borderTopLeftRadius).toBe("999px");
    expect(getComputedStyle(scene).borderTopLeftRadius).toBe("999px");
  });

  it("does not render an empty recommendation strip when the capability has no scenes", async () => {
    getEffectiveCatalog.mockResolvedValueOnce({
      domains: [
        {
          id: "domain-a",
          name: "文档处理",
          capabilities: [
            {
              id: "capability-a",
              name: "信息提取",
              scenarios: [],
            },
          ],
        },
      ],
    });

    render(<WelcomeCenterLayout greeting="你好" onSubmit={vi.fn()} />);

    await screen.findByRole("tab", { name: "信息提取" });
    expect(screen.queryByLabelText("推荐场景")).not.toBeInTheDocument();
  });

  it("uses the conversation palette for the selected domain and secondary controls", async () => {
    getEffectiveCatalog.mockResolvedValueOnce({
      domains: [
        {
          id: "domain-a",
          name: "文档处理",
          capabilities: [
            {
              id: "capability-a",
              name: "信息提取",
              scenarios: [
                {
                  id: "scenario-a",
                  name: "提取字段",
                  prompt_draft: "提取",
                },
              ],
            },
          ],
        },
      ],
    });

    render(<WelcomeCenterLayout greeting="你好" onSubmit={vi.fn()} />);

    const domain = await screen.findByRole("tab", { name: "文档处理" });
    const capability = screen.getByRole("tab", { name: "信息提取" });
    const scene = screen.getByRole("button", { name: "提取字段" });

    const selector = domain.closest(".scenario-preset-selector");
    const selectorStyle = getComputedStyle(selector as Element);

    expect(domain).toHaveClass("is-active");
    expect(
      domain.closest(".scenario-preset-domain-selector"),
    ).toHaveClass("is-single");
    expect(capability).toHaveClass("is-active");
    expect(scene).not.toHaveClass("is-active");
    expect(selectorStyle.getPropertyValue("--segment-track-bg").trim()).toBe(
      "#EDF1F7",
    );
    expect(selectorStyle.getPropertyValue("--segment-active-bg").trim()).toBe(
      "#FFFFFF",
    );
    expect(selectorStyle.getPropertyValue("--segment-active-text").trim()).toBe(
      "#2957DC",
    );
    expect(selectorStyle.getPropertyValue("--chip-active-bg").trim()).toBe(
      "#EEF4FF",
    );
    expect(selectorStyle.getPropertyValue("--chip-active-text").trim()).toBe(
      "#2957DC",
    );
    expect(selectorStyle.getPropertyValue("--chip-active-border").trim()).toBe(
      "#C7D6FF",
    );
    expect(getComputedStyle(scene).backgroundColor).toBe("rgb(233, 237, 244)");
  });

  it("renders secondary capsules with a slim height", async () => {
    getEffectiveCatalog.mockResolvedValueOnce({
      domains: [
        {
          id: "domain-a",
          name: "文档处理",
          capabilities: [
            {
              id: "capability-a",
              name: "信息提取",
              scenarios: [
                {
                  id: "scenario-a",
                  name: "提取字段",
                  prompt_draft: "提取",
                },
              ],
            },
          ],
        },
      ],
    });

    render(<WelcomeCenterLayout greeting="你好" onSubmit={vi.fn()} />);

    const capability = await screen.findByRole("tab", { name: "信息提取" });
    const domain = screen.getByRole("tab", { name: "文档处理" });
    const scene = screen.getByRole("button", { name: "提取字段" });

    expect(getComputedStyle(domain).height).toBe("32px");
    expect(getComputedStyle(capability).height).toBe("30px");
    expect(getComputedStyle(scene).height).toBe("26px");
  });

  it("opens the shared labelled skill menu and selects a matching skill by click", () => {
    const onChange = vi.fn();

    render(
      <WelcomeCenterLayout
        greeting="你好"
        onSubmit={vi.fn()}
        skillMentions={{
          items: skills,
          selected: [],
          onOpen: vi.fn(),
          onChange,
        }}
      />,
    );

    const input = screen.getByRole("textbox");
    setTokenEditorValue(input, "请用 @br");

    expect(
      screen.getByRole("listbox", { name: "可用上下文引用" }),
    ).toBeInTheDocument();
    fireEvent.click(screen.getByRole("option", { name: /browser/ }));

    expect(onChange).toHaveBeenCalledWith([skills[0]]);
    expect(input.textContent).toBe("请用 @browser ");
  });

  it("anchors the context-reference menu below the complete new-conversation card", () => {
    render(
      <WelcomeCenterLayout
        greeting="你好"
        onSubmit={vi.fn()}
        skillMentions={{
          items: skills,
          selected: [],
          onOpen: vi.fn(),
          onChange: vi.fn(),
        }}
      />,
    );

    const input = screen.getByRole("textbox");
    setTokenEditorValue(input, "@");
    const menu = document.getElementById("context-reference-menu");

    expect(menu?.parentElement).toBe(input.closest(".welcome-input-card"));
    expect(menu).toHaveStyle({
      top: "calc(100% + 8px)",
    });
    expect(menu).not.toHaveStyle({
      bottom: "calc(100% + 8px)",
    });
  });

  it("selects a matching skill with Enter without submitting", () => {
    const onChange = vi.fn();
    const onSubmit = vi.fn();

    render(
      <WelcomeCenterLayout
        greeting="你好"
        onSubmit={onSubmit}
        skillMentions={{
          items: skills,
          selected: [],
          onOpen: vi.fn(),
          onChange,
        }}
      />,
    );

    const input = screen.getByRole("textbox");
    setTokenEditorValue(input, "@BU");
    fireEvent.keyDown(input, { key: "Enter" });

    expect(onChange).toHaveBeenCalledWith([skills[1]]);
    expect(onSubmit).not.toHaveBeenCalled();
    expect(input.textContent).toBe("@Build ");
  });

  it("allows editing the skill query while the skill menu is open", () => {
    render(
      <WelcomeCenterLayout
        greeting="你好"
        onSubmit={vi.fn()}
        skillMentions={{
          items: skills,
          selected: [],
          onOpen: vi.fn(),
          onChange: vi.fn(),
        }}
      />,
    );

    const input = screen.getByRole("textbox");
    setTokenEditorValue(input, "@br");
    const event = createEvent.keyDown(input, { key: "Backspace" });
    fireEvent(input, event);

    expect(event.defaultPrevented).toBe(false);
  });

  it("does not submit while a loading skill menu is open", () => {
    const onSubmit = vi.fn();

    render(
      <WelcomeCenterLayout
        greeting="你好"
        onSubmit={onSubmit}
        skillMentions={{
          items: [],
          selected: [],
          loading: true,
          onOpen: vi.fn(),
          onChange: vi.fn(),
        }}
      />,
    );

    const input = screen.getByRole("textbox");
    setTokenEditorValue(input, "@missing");
    const event = createEvent.keyDown(input, { key: "Enter" });
    fireEvent(input, event);

    expect(event.defaultPrevented).toBe(true);
    expect(onSubmit).not.toHaveBeenCalled();
    expect(input.textContent).toBe("@missing");
  });

  it("preserves Enter during IME composition while a matching skill menu is open", () => {
    const onChange = vi.fn();
    const onSubmit = vi.fn();

    render(
      <WelcomeCenterLayout
        greeting="你好"
        onSubmit={onSubmit}
        skillMentions={{
          items: skills,
          selected: [],
          onOpen: vi.fn(),
          onChange,
        }}
      />,
    );

    const input = screen.getByRole("textbox");
    setTokenEditorValue(input, "@br");
    const event = createEvent.keyDown(input, {
      key: "Enter",
      isComposing: true,
    });
    fireEvent(input, event);

    expect(event.defaultPrevented).toBe(false);
    expect(onChange).not.toHaveBeenCalled();
    expect(onSubmit).not.toHaveBeenCalled();
  });

  it("awaits beforeSubmit before sending and clearing the welcome input", async () => {
    const onSubmit = vi.fn();
    let resolveBeforeSubmit!: (result: boolean) => void;
    const beforeSubmit = vi.fn(
      () =>
        new Promise<boolean>((resolve) => {
          resolveBeforeSubmit = resolve;
        }),
    );

    render(
      <WelcomeCenterLayout
        greeting="你好"
        onSubmit={onSubmit}
        beforeSubmit={beforeSubmit}
      />,
    );

    const input = screen.getByRole("textbox");
    fireEvent.change(input, { target: { value: "hello" } });
    fireEvent.click(screen.getByRole("button", { name: "发送" }));

    expect(beforeSubmit).toHaveBeenCalledTimes(1);
    expect(onSubmit).not.toHaveBeenCalled();
    expect(input).toHaveValue("hello");

    resolveBeforeSubmit(true);

    await waitFor(() => {
      expect(onSubmit).toHaveBeenCalledWith({ query: "hello", fileList: [] });
      expect(input).toHaveValue("");
    });
  });

  it("does not submit or clear the welcome input when beforeSubmit returns false", async () => {
    const onSubmit = vi.fn();
    const beforeSubmit = vi.fn().mockResolvedValue(false);

    render(
      <WelcomeCenterLayout
        greeting="你好"
        onSubmit={onSubmit}
        beforeSubmit={beforeSubmit}
      />,
    );

    const input = screen.getByRole("textbox");
    fireEvent.change(input, { target: { value: "hello" } });
    fireEvent.click(screen.getByRole("button", { name: "发送" }));

    await waitFor(() => expect(beforeSubmit).toHaveBeenCalledTimes(1));
    expect(onSubmit).not.toHaveBeenCalled();
    expect(input).toHaveValue("hello");
  });

  it("prevents duplicate sends while beforeSubmit is pending", async () => {
    const onSubmit = vi.fn();
    let resolveBeforeSubmit!: (result: boolean) => void;
    const beforeSubmit = vi.fn(
      () =>
        new Promise<boolean>((resolve) => {
          resolveBeforeSubmit = resolve;
        }),
    );

    render(
      <WelcomeCenterLayout
        greeting="你好"
        onSubmit={onSubmit}
        beforeSubmit={beforeSubmit}
      />,
    );

    const input = screen.getByRole("textbox");
    const sendButton = screen.getByRole("button", { name: "发送" });
    fireEvent.change(input, { target: { value: "hello" } });
    fireEvent.click(sendButton);
    fireEvent.click(sendButton);

    expect(beforeSubmit).toHaveBeenCalledTimes(1);
    expect(input).toBeDisabled();
    expect(sendButton).toBeDisabled();

    resolveBeforeSubmit(true);

    await waitFor(() => {
      expect(onSubmit).toHaveBeenCalledTimes(1);
    });
  });

  it("prevents duplicate sends while the submit handoff is pending", async () => {
    let resolveSubmit!: () => void;
    const onSubmit = vi.fn(
      () =>
        new Promise<void>((resolve) => {
          resolveSubmit = resolve;
        }),
    );

    render(
      <WelcomeCenterLayout
        greeting="你好"
        onSubmit={onSubmit}
        beforeSubmit={vi.fn().mockResolvedValue(true)}
      />,
    );

    const input = screen.getByRole("textbox");
    const sendButton = screen.getByRole("button", { name: "发送" });
    fireEvent.change(input, { target: { value: "hello" } });
    fireEvent.click(sendButton);

    await waitFor(() => expect(onSubmit).toHaveBeenCalledTimes(1));
    await Promise.resolve();

    expect(input).toBeDisabled();
    expect(sendButton).toBeDisabled();
    fireEvent.click(sendButton);

    expect(onSubmit).toHaveBeenCalledTimes(1);

    resolveSubmit();
    await waitFor(() => expect(input).not.toBeDisabled());
  });

  it("does not submit or clear attachments that finish uploading during preflight", async () => {
    const onSubmit = vi.fn();
    let resolveBeforeSubmit!: (result: boolean) => void;
    let resolveUpload!: (result: { url: string; file_name: string }) => void;
    const beforeSubmit = vi.fn(
      () =>
        new Promise<boolean>((resolve) => {
          resolveBeforeSubmit = resolve;
        }),
    );
    mockedUploadFile.mockImplementation(
      () =>
        new Promise((resolve) => {
          resolveUpload = resolve;
        }),
    );

    render(
      <WelcomeCenterLayout
        greeting="你好"
        onSubmit={onSubmit}
        beforeSubmit={beforeSubmit}
      />,
    );

    fireEvent.change(screen.getByRole("textbox"), {
      target: { value: "hello" },
    });
    fireEvent.click(screen.getByRole("button", { name: "发送" }));

    const file = new File(["hello"], "later.txt", { type: "text/plain" });
    document.dispatchEvent(
      new CustomEvent("pasteFile", {
        detail: { file },
      }),
    );
    await waitFor(() =>
      expect(screen.getByText("later.txt")).toBeInTheDocument(),
    );

    resolveUpload({ url: "later.txt", file_name: "later.txt" });
    await waitFor(() =>
      expect(chatApi.filePreviewUrl).toHaveBeenCalledWith("later.txt"),
    );

    resolveBeforeSubmit(true);
    await waitFor(() => {
      expect(onSubmit).toHaveBeenCalledWith({ query: "hello", fileList: [] });
      expect(screen.getByText("later.txt")).toBeInTheDocument();
    });
  });
});
