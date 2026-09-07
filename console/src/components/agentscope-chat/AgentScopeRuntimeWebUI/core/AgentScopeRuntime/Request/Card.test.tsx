import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { BubbleProps } from "@/components/agentscope-chat/Bubble/interface";
import Builder from "./Builder";
import RequestCard from "./Card";

vi.mock("./style", () => ({ default: () => null }));
vi.mock("@/components/agentscope-chat", () => ({
  Bubble: ({ cards, className, role }: BubbleProps) => (
    <div data-testid="message" className={className} data-role={role}>
      {cards?.map((card, index) => (
        <div key={index} data-testid={card.code}>
          {JSON.stringify(card.data)}
        </div>
      ))}
    </div>
  ),
}));

const files = [
  {
    uid: "1",
    name: "报告.pdf",
    type: "application/pdf",
    response: { url: "/report.pdf" },
  },
  {
    uid: "2",
    name: "截图.png",
    type: "image/png",
    response: { url: "/first.png" },
  },
  {
    uid: "3",
    name: "截图2.png",
    type: "image/png",
    response: { url: "/second.png" },
  },
];

describe("user messages with attachments", () => {
  afterEach(cleanup);
  it("applies attachment layout to a single card containing six images", () => {
    const images = Array.from({ length: 6 }, (_, index) => ({
      uid: String(index),
      name: `image-${index}.png`,
      type: "image/png",
      response: { url: `/image-${index}.png` },
    }));
    render(
      <RequestCard
        data={new Builder().handle({ query: "", fileList: images })}
      />,
    );
    expect(screen.getByTestId("message")).toHaveClass("swe-request-card");
    expect(screen.getByTestId("message")).not.toHaveClass(
      "swe-request-grouped",
    );
    expect(screen.queryByTestId("Text")).not.toBeInTheDocument();
    expect(screen.getAllByTestId("Images")).toHaveLength(1);
    expect(JSON.parse(screen.getByTestId("Images").textContent!)).toHaveLength(
      6,
    );
  });

  it("keeps text and attachments in one request and one grouped display after reload", () => {
    const request = new Builder().handle({
      query: "请分析这些附件",
      fileList: files,
    });
    expect(request.input).toHaveLength(1);
    expect(request.input[0].content.map((part) => part.type)).toEqual([
      "text",
      "file",
      "image",
      "image",
    ]);
    const { rerender } = render(<RequestCard data={request} />);
    expect(screen.getAllByTestId("message")).toHaveLength(1);
    expect(screen.getByTestId("message")).toHaveClass("swe-request-grouped");
    expect(screen.getByTestId("Text")).toHaveTextContent("请分析这些附件");
    expect(screen.getByTestId("Files")).toHaveTextContent("报告.pdf");
    expect(screen.getAllByTestId("Images")).toHaveLength(1);
    expect(screen.getByTestId("Images")).toHaveTextContent("/first.png");
    expect(screen.getByTestId("Images")).toHaveTextContent("/second.png");
    rerender(<RequestCard data={JSON.parse(JSON.stringify(request))} />);
    expect(screen.getByTestId("message")).toHaveClass("swe-request-grouped");
  });

  it("does not render an empty text bubble for attachment-only messages", () => {
    render(
      <RequestCard
        data={new Builder().handle({ query: "  ", fileList: files })}
      />,
    );
    expect(screen.queryByTestId("Text")).not.toBeInTheDocument();
    expect(screen.getByTestId("Files")).toBeInTheDocument();
    expect(screen.getByTestId("Images")).toBeInTheDocument();
  });

  it("preserves the existing text-only bubble", () => {
    render(
      <RequestCard
        data={new Builder().handle({ query: "普通消息", fileList: [] })}
      />,
    );
    expect(screen.getByTestId("message")).not.toHaveClass(
      "swe-request-grouped",
    );
    expect(screen.getByTestId("Text")).toHaveTextContent("普通消息");
  });
});
