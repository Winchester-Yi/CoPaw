import {
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import DownloadFileCard from "./index";
import { AutoPreviewHtmlProvider } from "../AutoPreviewHtmlContext";
import { FilePreviewPresentationProvider } from "../FilePreviewPresentationContext";

vi.mock("@agentscope-ai/icons", () => ({
  SparkDownloadLine: () => <span data-testid="download-icon" />,
}));

vi.mock("../FilePreviewModal", () => ({
  default: (props: {
    open: boolean;
    fileName: string;
    enableClickTracking?: boolean;
  }) =>
    props.open ? (
      <div
        data-click-tracking={String(Boolean(props.enableClickTracking))}
        data-testid="file-preview-modal"
      >
        {props.fileName}
      </div>
    ) : null,
}));

vi.mock("../FilePreviewDrawer", () => ({
  default: (props: { open: boolean; fileName: string }) =>
    props.open ? (
      <div data-testid="file-preview-drawer">{props.fileName}</div>
    ) : null,
}));

afterEach(() => {
  cleanup();
});

describe("DownloadFileCard", () => {
  it("普通聊天显式选择 drawer 时使用右侧预览组件", () => {
    render(
      <FilePreviewPresentationProvider value="drawer">
        <DownloadFileCard
          url="https://example.test/static/report.html"
          fileName="report.html"
        />
      </FilePreviewPresentationProvider>,
    );

    fireEvent.click(screen.getByRole("button"));

    expect(screen.getByTestId("file-preview-drawer")).toHaveTextContent(
      "report.html",
    );
    expect(screen.queryByTestId("file-preview-modal")).not.toBeInTheDocument();
  });

  it("普通聊天选择 workspace 时将文件交给统一文件工作台", () => {
    const handler = vi.fn();
    window.addEventListener("copaw:chat-workspace-file", handler);
    render(
      <FilePreviewPresentationProvider value="workspace">
        <DownloadFileCard
          url="https://example.test/static/report.html"
          fileName="report.html"
        />
      </FilePreviewPresentationProvider>,
    );

    fireEvent.click(screen.getByRole("button"));

    expect(handler).toHaveBeenCalledWith(
      expect.objectContaining({
        detail: expect.objectContaining({
          action: "open",
          fileName: "report.html",
        }),
      }),
    );
    expect(screen.queryByTestId("file-preview-drawer")).not.toBeInTheDocument();
    expect(screen.queryByTestId("file-preview-modal")).not.toBeInTheDocument();
    window.removeEventListener("copaw:chat-workspace-file", handler);
  });

  it("显式启用时自动打开带 auto-preview 标记的 HTML 预览", async () => {
    render(
      <DownloadFileCard
        url="https://example.test/static/report[auto-preview]-1.html"
        fileName="report[auto-preview]-1.html"
        autoPreview
      />,
    );

    await waitFor(() => {
      expect(screen.getByTestId("file-preview-modal")).toHaveTextContent(
        "report[auto-preview]-1.html",
      );
    });
  });

  it("页面级自动预览不再匹配存款到期完整客户名单关键词", () => {
    render(
      <AutoPreviewHtmlProvider triggerKey={1} onConsumed={vi.fn()}>
        <DownloadFileCard
          url="https://example.test/static/report-old.html"
          fileName="存款到期完整客户名单-old.html"
        />
        <DownloadFileCard
          url="https://example.test/static/report-new.html"
          fileName="存款到期完整客户名单-new.html"
        />
      </AutoPreviewHtmlProvider>,
    );

    expect(screen.queryByTestId("file-preview-modal")).not.toBeInTheDocument();
  });

  it("普通 HTML 链接不自动打开预览", () => {
    render(
      <DownloadFileCard
        url="https://example.test/static/report.html"
        fileName="report.html"
      />,
    );

    expect(screen.queryByTestId("file-preview-modal")).not.toBeInTheDocument();
  });

  it("页面级自动预览只打开最后一个匹配的 HTML", async () => {
    render(
      <AutoPreviewHtmlProvider triggerKey={1} onConsumed={vi.fn()}>
        <DownloadFileCard
          url="https://example.test/static/report-old[auto-preview].html"
          fileName="report-old[auto-preview].html"
        />
        <DownloadFileCard
          url="https://example.test/static/report-new[auto-preview].html"
          fileName="report-new[auto-preview].html"
        />
      </AutoPreviewHtmlProvider>,
    );

    await waitFor(() => {
      expect(screen.getByTestId("file-preview-modal")).toHaveTextContent(
        "report-new[auto-preview].html",
      );
    });
    expect(screen.getAllByTestId("file-preview-modal")).toHaveLength(1);
  });

  it("仍然支持用户点击卡片后打开预览", () => {
    render(
      <DownloadFileCard
        url="https://example.test/static/report.html"
        fileName="report.html"
      />,
    );

    fireEvent.click(screen.getByRole("button"));

    expect(screen.getByTestId("file-preview-modal")).toHaveTextContent(
      "report.html",
    );
    expect(screen.getByTestId("file-preview-modal")).toHaveAttribute(
      "data-click-tracking",
      "false",
    );
  });

  it("显式传入采集开关时才启用 HTML 点击统计", () => {
    render(
      <DownloadFileCard
        url="https://example.test/static/report[auto-preview].html"
        fileName="report[auto-preview].html"
        enableClickTracking
      />,
    );

    fireEvent.click(screen.getByRole("button"));

    expect(screen.getByTestId("file-preview-modal")).toHaveAttribute(
      "data-click-tracking",
      "true",
    );
  });

  it("auto-preview HTML 即使只传 autoPreview 也会启用点击统计", async () => {
    render(
      <DownloadFileCard
        url="https://example.test/static/report[auto-preview].html"
        fileName="report[auto-preview].html"
        autoPreview
      />,
    );

    await waitFor(() => {
      expect(screen.getByTestId("file-preview-modal")).toHaveAttribute(
        "data-click-tracking",
        "true",
      );
    });
  });

  it("auto-preview HTML 即使关闭自动弹窗，手动打开后也会启用点击统计", () => {
    render(
      <DownloadFileCard
        url="https://example.test/static/report[auto-preview].html"
        fileName="report[auto-preview].html"
        autoPreview={false}
      />,
    );

    fireEvent.click(screen.getByRole("button"));

    expect(screen.getByTestId("file-preview-modal")).toHaveAttribute(
      "data-click-tracking",
      "true",
    );
  });
});
