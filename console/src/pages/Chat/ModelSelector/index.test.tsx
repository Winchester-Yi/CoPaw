import {
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import ModelSelector from ".";
import type { ModelRuntimeConfig, ProviderInfo } from "../../../api/types";

const mocks = vi.hoisted(() => ({
  loadModelData: vi.fn(),
  setModelRuntimeConfig: vi.fn(),
  setActiveLlm: vi.fn(),
  updateModelRuntimeConfig: vi.fn(),
  messageError: vi.fn(),
}));

const providerFixture: ProviderInfo[] = [
  {
    id: "openai",
    name: "OpenAI",
    api_key_prefix: "",
    chat_model: "OpenAIChatModel",
    models: [
      {
        id: "gpt-5",
        name: "GPT-5",
        supports_multimodal: false,
        supports_image: false,
        supports_video: false,
      },
    ],
    extra_models: [],
    is_custom: false,
    is_local: false,
    support_model_discovery: false,
    support_connection_check: false,
    freeze_url: false,
    require_api_key: false,
    api_key: "key",
    base_url: "https://api.openai.test",
    model_configs: {
      "gpt-5": {
        supports_enable_thinking: true,
        supported_reasoning_efforts: ["low", "high", "max"],
        enable_thinking: true,
        reasoning_effort: null,
      },
    },
  },
  {
    id: "dashscope",
    name: "DashScope",
    api_key_prefix: "",
    chat_model: "OpenAIChatModel",
    models: [
      {
        id: "qwen-max",
        name: "Qwen Max",
        supports_multimodal: false,
        supports_image: false,
        supports_video: false,
      },
    ],
    extra_models: [],
    is_custom: false,
    is_local: false,
    support_model_discovery: false,
    support_connection_check: false,
    freeze_url: false,
    require_api_key: false,
    api_key: "key",
    base_url: "https://dashscope.test",
    model_configs: {
      "qwen-max": {
        supports_enable_thinking: false,
        supported_reasoning_efforts: ["low", "max"],
        enable_thinking: false,
        reasoning_effort: "max",
      },
    },
  },
  {
    id: "plain",
    name: "Plain Provider",
    api_key_prefix: "",
    chat_model: "OpenAIChatModel",
    models: [
      {
        id: "plain-model",
        name: "Plain Model",
        supports_multimodal: false,
        supports_image: false,
        supports_video: false,
      },
    ],
    extra_models: [],
    is_custom: false,
    is_local: false,
    support_model_discovery: false,
    support_connection_check: false,
    freeze_url: false,
    require_api_key: false,
    api_key: "key",
    base_url: "https://plain.test",
    model_configs: {
      "plain-model": {
        supports_enable_thinking: false,
        supported_reasoning_efforts: [],
        enable_thinking: false,
        reasoning_effort: null,
      },
    },
  },
];

let activeModel = { provider_id: "openai", model: "gpt-5" };

vi.mock("antd", () => ({
  Button: ({ children, ...props }: React.ButtonHTMLAttributes<HTMLButtonElement>) => (
    <button {...props}>{children}</button>
  ),
  Dropdown: ({ children, dropdownRender }: { children: React.ReactNode; dropdownRender: () => React.ReactNode }) => (
    <>
      {children}
      {dropdownRender()}
    </>
  ),
  Select: ({ onChange, options }: { onChange: (value: string) => void; options: Array<{ value: string; label: string }> }) => (
    <select onChange={(event) => onChange(event.target.value)}>
      {options.map((option) => (
        <option key={option.value} value={option.value}>
          {option.label}
        </option>
      ))}
    </select>
  ),
  Spin: () => <span>loading</span>,
  Switch: ({ checked, onChange }: { checked: boolean; onChange: (next: boolean) => void }) => (
    <button aria-checked={checked} role="switch" onClick={() => onChange(!checked)} />
  ),
  Tooltip: ({ children }: { children: React.ReactNode }) => <>{children}</>,
}));

vi.mock("@ant-design/icons", () => ({
  CheckOutlined: () => <span />,
  LoadingOutlined: () => <span />,
  RightOutlined: () => <span />,
}));

vi.mock("@agentscope-ai/icons", () => ({
  SparkDownLine: () => <span />,
}));

vi.mock("react-i18next", () => ({
  useTranslation: () => ({
    t: (_key: string, fallback?: string) => fallback || _key,
  }),
}));

vi.mock("react-router-dom", () => ({
  useLocation: () => ({ pathname: "/chat" }),
}));

vi.mock("../../../hooks/useAppMessage", () => ({
  useAppMessage: () => ({ message: { error: mocks.messageError } }),
}));

vi.mock("../../../api/modules/provider", () => ({
  providerApi: {
    setActiveLlm: mocks.setActiveLlm,
    updateModelRuntimeConfig: mocks.updateModelRuntimeConfig,
  },
}));

vi.mock("../../../stores/providerModelStore", () => ({
  useProviderModelStore: (selector: (state: Record<string, unknown>) => unknown) =>
    selector({
      providers: providerFixture,
      activeModels: { active_llm: activeModel },
      loading: false,
      loadModelData: mocks.loadModelData,
      setModelRuntimeConfig: mocks.setModelRuntimeConfig,
    }),
}));

describe("ModelSelector", () => {
  afterEach(cleanup);

  beforeEach(() => {
    activeModel = { provider_id: "openai", model: "gpt-5" };
    providerFixture[0].model_configs!["gpt-5"] = {
      supports_enable_thinking: true,
      supported_reasoning_efforts: ["low", "high", "max"],
      enable_thinking: true,
      reasoning_effort: null,
    };
    mocks.loadModelData.mockResolvedValue({
      providers: providerFixture,
      activeModels: { active_llm: activeModel },
    });
    mocks.setActiveLlm.mockResolvedValue({ active_llm: activeModel });
    mocks.updateModelRuntimeConfig.mockResolvedValue(
      providerFixture[0].model_configs?.["gpt-5"],
    );
    vi.clearAllMocks();
  });

  it("renders every model with its provider and display-name metadata", () => {
    render(<ModelSelector />);

    expect(screen.getByRole("button", { name: /gpt-5/i })).toBeInTheDocument();
    expect(screen.getByText("OpenAI · GPT-5")).toBeInTheDocument();
    expect(screen.getByText("DashScope · Qwen Max")).toBeInTheDocument();
  });

  it("shows declared efforts without selecting one until the user chooses", () => {
    render(<ModelSelector />);

    expect(screen.getByRole("button", { name: "低" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "高" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "极高" })).toBeInTheDocument();
    expect(screen.getByText("请选择思考强度")).toBeInTheDocument();
  });

  it("persists the selected reasoning effort as a tenant model default", async () => {
    render(<ModelSelector />);

    fireEvent.click(screen.getByRole("button", { name: "高" }));

    await waitFor(() =>
      expect(mocks.updateModelRuntimeConfig).toHaveBeenCalledWith(
        "openai",
        "gpt-5",
        { reasoning_effort: "high" },
      ),
    );
  });

  it("prevents concurrent model configuration updates while one is saving", async () => {
    let resolveUpdate!: (config: ModelRuntimeConfig) => void;
    mocks.updateModelRuntimeConfig.mockImplementationOnce(
      () =>
        new Promise<ModelRuntimeConfig>((resolve) => {
          resolveUpdate = resolve;
        }),
    );
    render(<ModelSelector />);

    fireEvent.click(screen.getByRole("button", { name: "高" }));

    await waitFor(() =>
      expect(screen.getByRole("button", { name: "低" })).toBeDisabled(),
    );
    expect(screen.getByRole("button", { name: /qwen-max/i })).toBeDisabled();

    resolveUpdate(providerFixture[0].model_configs!["gpt-5"]!);
    await waitFor(() =>
      expect(screen.getByRole("button", { name: "低" })).toBeEnabled(),
    );
  });

  it("keeps a selected effort visible but disabled while thinking is off", () => {
    providerFixture[0].model_configs!["gpt-5"] = {
      supports_enable_thinking: true,
      supported_reasoning_efforts: ["low", "high"],
      enable_thinking: false,
      reasoning_effort: "high",
    };
    render(<ModelSelector />);

    expect(screen.getByRole("button", { name: "高" })).toBeDisabled();
    expect(screen.getByText("平衡推理效果与速度")).toBeInTheDocument();
  });

  it("shows only effort controls for a reasoning-only model", () => {
    activeModel = { provider_id: "dashscope", model: "qwen-max" };
    render(<ModelSelector />);

    expect(screen.queryByText("思考模式")).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "低" })).toBeEnabled();
    expect(screen.getByRole("button", { name: "极高" })).toHaveAttribute(
      "aria-pressed",
      "true",
    );
  });

  it("does not render a configuration region for a model without thinking capabilities", () => {
    activeModel = { provider_id: "plain", model: "plain-model" };
    render(<ModelSelector />);

    expect(screen.queryByText("模型配置")).not.toBeInTheDocument();
    expect(screen.queryByText("思考模式")).not.toBeInTheDocument();
  });

  it("persists a selected model and refreshes its runtime configuration", async () => {
    render(<ModelSelector />);

    fireEvent.click(screen.getByRole("button", { name: /qwen-max/i }));

    await waitFor(() =>
      expect(mocks.setActiveLlm).toHaveBeenCalledWith({
        provider_id: "dashscope",
        model: "qwen-max",
        scope: "global",
      }),
    );
    expect(mocks.loadModelData).toHaveBeenCalledWith({ scope: "effective" });
  });

  it("keeps activation successful when the post-activation refresh fails", async () => {
    render(<ModelSelector />);
    await waitFor(() => expect(mocks.loadModelData).toHaveBeenCalled());
    mocks.loadModelData.mockRejectedValueOnce(new Error("refresh failed"));
    const dispatchSpy = vi
      .spyOn(window, "dispatchEvent")
      .mockImplementation(() => true);

    fireEvent.click(screen.getByRole("button", { name: /qwen-max/i }));

    await waitFor(() =>
      expect(mocks.setActiveLlm).toHaveBeenCalledWith({
        provider_id: "dashscope",
        model: "qwen-max",
        scope: "global",
      }),
    );
    await waitFor(() =>
      expect(dispatchSpy).toHaveBeenCalledWith(
        expect.objectContaining({ type: "model-switched" }),
      ),
    );
    expect(mocks.messageError).not.toHaveBeenCalled();
    dispatchSpy.mockRestore();
  });

  it("keeps the previous active card and reports an error when model activation fails", async () => {
    mocks.setActiveLlm.mockRejectedValueOnce(new Error("activation failed"));
    render(<ModelSelector />);

    fireEvent.click(screen.getByRole("button", { name: /qwen-max/i }));

    await waitFor(() =>
      expect(mocks.messageError).toHaveBeenCalledWith("activation failed"),
    );
    expect(screen.getByRole("button", { name: /gpt-5/i })).toHaveAttribute(
      "aria-pressed",
      "true",
    );
  });

  it("restores the previous effort when saving it fails", async () => {
    mocks.updateModelRuntimeConfig.mockRejectedValueOnce(
      new Error("save failed"),
    );
    render(<ModelSelector />);

    fireEvent.click(screen.getByRole("button", { name: "高" }));

    await waitFor(() =>
      expect(mocks.messageError).toHaveBeenCalledWith("save failed"),
    );
    expect(screen.getByRole("button", { name: "高" })).toHaveAttribute(
      "aria-pressed",
      "false",
    );
  });
});
