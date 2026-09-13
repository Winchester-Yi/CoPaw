import "@testing-library/jest-dom/vitest";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { ProviderInfo } from "../../../../../api/types";
import { RemoteModelManageModal } from "./RemoteModelManageModal";

const mocks = vi.hoisted(() => ({
  error: vi.fn(),
  setActiveLlm: vi.fn(),
}));

vi.mock("@agentscope-ai/design", () => {
  const Form = ({ children }: { children: React.ReactNode }) => <>{children}</>;
  Form.useForm = () => [
    { setFieldsValue: vi.fn(), validateFields: vi.fn(), resetFields: vi.fn() },
  ];
  Form.Item = ({ children }: { children: React.ReactNode }) => <>{children}</>;

  return {
    Button: ({
      children,
      ...props
    }: React.ButtonHTMLAttributes<HTMLButtonElement>) => (
      <button {...props}>{children}</button>
    ),
    Checkbox: ({ children }: { children: React.ReactNode }) => <>{children}</>,
    Form,
    Input: () => <input />,
    InputNumber: () => <input />,
    Modal: ({
      children,
      open,
    }: {
      children: React.ReactNode;
      open?: boolean;
    }) => (open ? <div>{children}</div> : null),
    Switch: () => <button type="button" />,
    Tag: ({ children }: { children: React.ReactNode }) => (
      <span>{children}</span>
    ),
  };
});

vi.mock("../../../../../api", () => ({
  default: {
    setActiveLlm: mocks.setActiveLlm,
  },
}));

vi.mock("react-i18next", () => ({
  useTranslation: () => ({
    t: (_key: string, fallback?: string) => fallback || _key,
  }),
}));

vi.mock("../../../../../contexts/ThemeContext", () => ({
  useTheme: () => ({ isDark: false }),
}));

vi.mock("../../../../../hooks/useAppMessage", () => ({
  useAppMessage: () => ({
    message: {
      error: mocks.error,
      success: vi.fn(),
      warning: vi.fn(),
      info: vi.fn(),
    },
  }),
}));

vi.mock("@ant-design/icons", () => ({
  ApiOutlined: () => <span />,
  DeleteOutlined: () => <span />,
  EyeOutlined: () => <span />,
  PlusOutlined: () => <span />,
  SettingOutlined: () => <span />,
  SyncOutlined: () => <span />,
}));

const provider: ProviderInfo = {
  id: "openai",
  name: "OpenAI",
  api_key_prefix: "",
  chat_model: "OpenAIChatModel",
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
  support_model_discovery: false,
  support_connection_check: false,
  freeze_url: false,
  require_api_key: false,
  api_key: "",
  base_url: "https://api.openai.test",
};

describe("RemoteModelManageModal", () => {
  beforeEach(() => {
    mocks.error.mockReset();
    mocks.setActiveLlm.mockReset().mockResolvedValue({});
  });

  it("keeps the newly activated model selected when refresh fails", async () => {
    render(
      <RemoteModelManageModal
        provider={provider}
        activeModels={{ active_llm: { provider_id: "openai", model: "gpt-4" } }}
        open
        onClose={vi.fn()}
        onSaved={vi.fn().mockRejectedValue(new Error("refresh failed"))}
      />,
    );

    fireEvent.click(screen.getByRole("button", { name: /GPT-5/ }));

    await waitFor(() =>
      expect(mocks.setActiveLlm).toHaveBeenCalledWith({
        provider_id: "openai",
        model: "gpt-5",
        scope: "global",
      }),
    );
    expect(screen.getByRole("button", { name: /GPT-5/ })).toHaveAttribute(
      "aria-pressed",
      "true",
    );
    expect(mocks.error).not.toHaveBeenCalled();
  });
});
