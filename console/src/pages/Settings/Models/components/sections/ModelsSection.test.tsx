import "@testing-library/jest-dom/vitest";
import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { ModelsSection } from "./ModelsSection";

vi.mock("@agentscope-ai/design", () => ({
  Button: ({
    children,
    ...props
  }: React.ButtonHTMLAttributes<HTMLButtonElement>) => (
    <button {...props}>{children}</button>
  ),
  Modal: ({ children, open }: { children: React.ReactNode; open?: boolean }) =>
    open ? <div>{children}</div> : null,
  Select: () => <div data-testid="active-model-picker" />,
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
  it("does not render the legacy active-model picker or save action", () => {
    render(
      <ModelsSection
        providers={[
          {
            id: "local",
            name: "Local",
            is_local: true,
          },
        ]}
        activeModels={{
          active_llm: { provider_id: "local", model: "gpt-5" },
        }}
      />,
    );

    expect(screen.queryByTestId("active-model-picker")).not.toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: "models.save" }),
    ).not.toBeInTheDocument();
  });
});
