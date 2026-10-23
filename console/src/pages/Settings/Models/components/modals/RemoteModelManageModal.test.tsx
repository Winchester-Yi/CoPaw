import "@testing-library/jest-dom/vitest";
import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { ProviderInfo } from "../../../../../api/types";
import { RemoteModelManageModal } from "./RemoteModelManageModal";

const mocks = vi.hoisted(() => ({ error: vi.fn() }));

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
    Form,
    Input: () => <input />,
    Modal: ({
      children,
      open,
    }: {
      children: React.ReactNode;
      open?: boolean;
    }) => (open ? <div>{children}</div> : null),
    Tag: ({ children }: { children: React.ReactNode }) => (
      <span>{children}</span>
    ),
  };
});

vi.mock("../../../../../api", () => ({ default: {} }));
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
  afterEach(cleanup);

  it("keeps model management focused on directory maintenance", () => {
    render(
      <RemoteModelManageModal
        provider={provider}
        open
        onClose={vi.fn()}
        onSaved={vi.fn()}
      />,
    );

    expect(
      screen.getAllByRole("button", { name: "models.testConnection" }),
    ).toHaveLength(2);
    expect(
      screen.queryByRole("button", { name: "配置" }),
    ).not.toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: /GPT-5/ }),
    ).not.toBeInTheDocument();
  });
});
