import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { Form } from "@agentscope-ai/design";
import ChatTaskEditFormBody from "./ChatTaskEditFormBody";

vi.mock("react-i18next", () => ({
  useTranslation: () => ({ t: (key: string) => key }),
}));

describe("ChatTaskEditFormBody", () => {
  it("does not show Agent request content for workflow tasks", () => {
    render(
      <Form initialValues={{ task_type: "workflow" }}>
        <ChatTaskEditFormBody />
      </Form>,
    );

    expect(screen.queryByLabelText("请求内容")).toBeNull();
    expect(screen.queryByLabelText("消息内容")).toBeNull();
  });
});
