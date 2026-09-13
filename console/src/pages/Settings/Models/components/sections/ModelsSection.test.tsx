import "@testing-library/jest-dom/vitest";
import {
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ModelsSection } from "./ModelsSection";
import type { ProviderInfo } from "../../../../../api/types";

const mocks = vi.hoisted(() => ({
  setActiveLlm: vi.fn(),
  onSaved: vi.fn(),
}));

interface SelectOption {
  value?: string;
  label?: string;
  options?: SelectOption[];
}

interface SelectProps {
  options?: SelectOption[];
  value?: string;
  disabled?: boolean;
  onChange?: (value: string) => void;
}

vi.mock("@agentscope-ai/design", () => ({
  Button: ({
    children,
    ...props
  }: React.ButtonHTMLAttributes<HTMLButtonElement>) => (
    <button {...props}>{children}</button>
  ),
  Modal: ({ children, open }: { children: React.ReactNode; open?: boolean }) =>
    open ? <div>{children}</div> : null,
  Select: ({ options, value, onChange, disabled }: SelectProps) => (
    <select
      data-testid="active-model-picker"
      value={value || ""}
      disabled={disabled}
      onChange={(event) => onChange(event.target.value)}
    >
      <option value="">models.selectModel</option>
      {options?.map((option) =>
        option.options ? (
          <optgroup key={option.label} label={option.label}>
            {option.options.map((child) => (
              <option key={child.value} value={child.value}>
                {child.label}
              </option>
            ))}
          </optgroup>
        ) : (
          <option key={option.value} value={option.value}>
            {option.label}
          </option>
        ),
      )}
    </select>
  ),
}));

vi.mock("../../../../../api", () => ({
  default: { setActiveLlm: mocks.setActiveLlm },
}));

vi.mock("../modals/ModelRuntimeConfigModal", () => ({
  ModelRuntimeConfigModal: () => <div data-testid="runtime-config-modal" />,
}));

vi.mock("../../../../../hooks/useAppMessage", () => ({
  useAppMessage: () => ({ message: { error: vi.fn(), success: vi.fn() } }),
}));

vi.mock("../../../../../stores/iframeStore", () => ({
  useIframeStore: () => null,
}));

vi.mock("../../../../../utils/identity", () => ({
  getUserId: () => "tenant-a",
}));

vi.mock("../../../../../components/TenantSelector", () => ({
  TenantSelector: () => null,
}));

vi.mock("react-i18next", () => ({
  useTranslation: () => ({ t: (key: string) => key }),
}));

describe("ModelsSection", () => {
  afterEach(cleanup);

  beforeEach(() => {
    mocks.setActiveLlm.mockReset().mockResolvedValue({});
  });

  it("lists only configured remote models grouped by provider", () => {
    render(
      <ModelsSection
        providers={
          [
            {
              id: "openai",
              name: "OpenAI",
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
              require_api_key: true,
              api_key: "sk-test",
              base_url: "https://api.openai.com/v1",
            },
            {
              id: "local",
              name: "Local",
              models: [{ id: "local-model", name: "Local" }],
              extra_models: [],
              is_custom: false,
              is_local: true,
              require_api_key: false,
              api_key: "",
              base_url: "http://localhost",
            },
          ] as ProviderInfo[]
        }
        activeModels={{
          active_llm: { provider_id: "openai", model: "gpt-5" },
        }}
      />,
    );

    expect(screen.getByTestId("active-model-picker")).toHaveValue(
      "openai:gpt-5",
    );
    expect(screen.getByRole("group", { name: "OpenAI" })).toBeInTheDocument();
    expect(screen.queryByText("Local")).not.toBeInTheDocument();
  });

  it("activates a selected model immediately", async () => {
    render(
      <ModelsSection
        providers={
          [
            {
              id: "openai",
              name: "OpenAI",
              models: [
                {
                  id: "gpt-4",
                  name: "GPT-4",
                  supports_multimodal: false,
                  supports_image: false,
                  supports_video: false,
                },
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
              require_api_key: true,
              api_key: "sk-test",
              base_url: "https://api.openai.com/v1",
            },
          ] as ProviderInfo[]
        }
        activeModels={{
          active_llm: { provider_id: "openai", model: "gpt-4" },
        }}
      />,
    );

    fireEvent.change(screen.getByTestId("active-model-picker"), {
      target: { value: "openai:gpt-5" },
    });

    await waitFor(() =>
      expect(mocks.setActiveLlm).toHaveBeenCalledWith({
        provider_id: "openai",
        model: "gpt-5",
        scope: "global",
      }),
    );
  });

  it("keeps the selected model when refreshing after activation fails", async () => {
    mocks.onSaved.mockRejectedValueOnce(new Error("refresh failed"));
    render(
      <ModelsSection
        providers={[
          {
            id: "openai",
            name: "OpenAI",
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
            require_api_key: true,
            api_key: "sk-test",
            base_url: "https://api.openai.com/v1",
          } as ProviderInfo,
        ]}
        activeModels={{ active_llm: { provider_id: "openai", model: "gpt-4" } }}
        onSaved={mocks.onSaved}
      />,
    );

    fireEvent.change(screen.getByTestId("active-model-picker"), {
      target: { value: "openai:gpt-5" },
    });

    await waitFor(() =>
      expect(screen.getByTestId("active-model-picker")).toHaveValue(
        "openai:gpt-5",
      ),
    );
  });

  it("does not render the legacy active-model picker or save action", () => {
    render(
      <ModelsSection
        providers={
          [
            {
              id: "local",
              name: "Local",
              api_key_prefix: "",
              chat_model: "OpenAIChatModel",
              models: [],
              extra_models: [],
              is_custom: false,
              is_local: true,
              support_model_discovery: false,
              support_connection_check: false,
              freeze_url: false,
              require_api_key: false,
              api_key: "",
              base_url: "",
            },
          ] as ProviderInfo[]
        }
        activeModels={{
          active_llm: { provider_id: "local", model: "gpt-5" },
        }}
      />,
    );

    expect(screen.getByTestId("active-model-picker")).toBeDisabled();
    expect(
      screen.queryByRole("button", { name: "models.save" }),
    ).not.toBeInTheDocument();
  });
});
