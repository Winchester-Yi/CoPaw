import { describe, expect, it, vi } from "vitest";
import type { HtmlTrackerPayloadType } from "@/api/types/htmlPreviewEvents";
import {
  attachHtmlPreviewClickTracker,
  buildHtmlPreviewClickPayload,
  buildHtmlPreviewListSnapshotPayload,
} from "./htmlPreviewClickTracking";

function createDocument(html: string) {
  const doc = document.implementation.createHTMLDocument("preview");
  doc.body.innerHTML = html;
  return doc;
}

describe("htmlPreviewClickTracking", () => {
  it("prefers data-track-id and data-track-name for click payload", () => {
    const doc = createDocument(
      '<button data-track-id="follow" data-track-name="立即跟进">跟进客户</button>',
    );
    const button = doc.querySelector("button") as HTMLElement;

    const payload = buildHtmlPreviewClickPayload(
      button,
      {
        cronTaskId: "task-1",
        cronTaskName: "存款到期提醒",
        fileUrl: "https://example.com/a.html",
        fileName: "a.html",
      },
      new Date("2026-05-30T10:00:00.000Z"),
    );

    expect(payload).toMatchObject({
      cron_task_id: "task-1",
      cron_task_name: "存款到期提醒",
      file_url: "https://example.com/a.html",
      file_name: "a.html",
      list_key: "https://example.com/a.html",
      list_name: "a.html",
      button_id: "follow",
      button_name: "立即跟进",
      button_text: "跟进客户",
      button_type: "other",
      clicked_at: "2026-05-30T10:00:00.000Z",
    });
  });

  it("falls back to id, name and text when tracking attributes are absent", () => {
    const doc = createDocument('<button name="call-customer">预约电话</button>');
    const button = doc.querySelector("button") as HTMLElement;

    const payload = buildHtmlPreviewClickPayload(button, {
      fileUrl: "https://example.com/a.html",
      fileName: "a.html",
    });

    expect(payload?.button_id).toBe("call-customer");
    expect(payload?.button_name).toBe("call-customer");
    expect(payload?.button_text).toBe("预约电话");
  });

  it("uses known button classes before text when tracking attributes are absent", () => {
    const doc = createDocument(`
      <div>
        <a class="link-btn">洞察页面</a>
        <a class="link-btn phone">电话访问</a>
      </div>
    `);
    const links = doc.querySelectorAll("a");

    const insightPayload = buildHtmlPreviewClickPayload(links[0] as HTMLElement, {
      fileUrl: "https://example.com/a.html",
      fileName: "a.html",
    });
    const phonePayload = buildHtmlPreviewClickPayload(links[1] as HTMLElement, {
      fileUrl: "https://example.com/a.html",
      fileName: "a.html",
    });

    expect(insightPayload?.button_id).toBe("insight");
    expect(insightPayload?.button_name).toBe("洞察页面");
    expect(phonePayload?.button_id).toBe("phone");
    expect(phonePayload?.button_name).toBe("电话访问");
    expect(phonePayload?.button_type).toBe("phone");
  });

  it("tracks view plan links without treating generic link buttons as insight", () => {
    const doc = createDocument(`
      <table>
        <thead>
          <tr>
            <th>客户姓名</th>
            <th>经营方案</th>
          </tr>
        </thead>
        <tbody>
          <tr data-customer-id="CUST-001" data-customer-name="祝话">
            <td><strong>祝话</strong></td>
            <td><a class="link-btn" href="https://example.com/plan">查看方案</a></td>
          </tr>
          <tr>
            <td>程广泛</td>
            <td>暂不生成方案</td>
          </tr>
        </tbody>
      </table>
    `);
    const link = doc.querySelector("a") as HTMLElement;

    const payload = buildHtmlPreviewClickPayload(link, {
      fileUrl: "https://example.com/a.html",
      fileName: "a.html",
    });

    expect(payload?.button_id).toBe("plan");
    expect(payload?.button_name).toBe("查看方案");
    expect(payload?.button_text).toBe("查看方案");
    expect(payload?.button_type).toBe("plan");
    expect(payload?.customer_id).toBe("CUST-001");
    expect(payload?.customer_name).toBe("祝话");
  });

  it("prefers structured customer fields from the clicked table row", () => {
    const doc = createDocument(`
      <table>
        <tbody>
          <tr data-customer-id="CUST-001" data-customer-name="祝话" data-customer-product="定存243M">
            <td>祝话</td>
            <td>定存243M</td>
            <td><a data-track-id="insight">洞察页面</a></td>
          </tr>
        </tbody>
      </table>
    `);
    const link = doc.querySelector("a") as HTMLElement;

    const payload = buildHtmlPreviewClickPayload(link, {
      fileUrl: "https://example.com/a.html",
      fileName: "a.html",
    });

    expect(payload?.customer_info).toEqual({
      customer_id: "CUST-001",
      name: "祝话",
    });
    expect(payload?.customer_id).toBe("CUST-001");
    expect(payload?.customer_name).toBe("祝话");
  });

  it("only keeps customer identity when falling back to table headers", () => {
    const doc = createDocument(`
      <table>
        <thead>
          <tr>
            <th>序号</th>
            <th>客户姓名</th>
            <th>到期产品</th>
            <th>到期金额</th>
            <th>操作</th>
          </tr>
        </thead>
        <tbody>
          <tr>
            <td>3</td>
            <td>祝话</td>
            <td>定存243M</td>
            <td>18.00万元</td>
            <td><a class="link-btn phone">电话访问</a></td>
          </tr>
        </tbody>
      </table>
    `);
    const link = doc.querySelector("a") as HTMLElement;

    const payload = buildHtmlPreviewClickPayload(link, {
      fileUrl: "https://example.com/a.html",
      fileName: "a.html",
    });

    expect(payload?.customer_info).toEqual({
      "客户姓名": "祝话",
    });
    expect(payload?.customer_name).toBe("祝话");
  });

  it("builds list snapshot payload from structured and table fallback rows", () => {
    const doc = createDocument(`
      <table>
        <thead>
          <tr>
            <th>序号</th>
            <th>客户姓名</th>
            <th>操作</th>
          </tr>
        </thead>
        <tbody>
          <tr data-customer-id="CUST-001" data-customer-name="祝话">
            <td>1</td>
            <td><strong>祝话</strong></td>
            <td><a class="link-btn">洞察页面</a></td>
          </tr>
          <tr>
            <td>2</td>
            <td><strong>程广泛</strong></td>
            <td><a class="link-btn phone">电话访问</a></td>
          </tr>
        </tbody>
      </table>
    `);

    const payload = buildHtmlPreviewListSnapshotPayload(
      doc,
      {
        cronTaskId: "task-1",
        cronTaskName: "存款到期提醒",
        fileUrl: "https://example.com/a.html",
        fileName: "a.html",
      },
      new Date("2026-05-30T10:00:00.000Z"),
    );

    expect(payload).toMatchObject({
      cron_task_id: "task-1",
      cron_task_name: "存款到期提醒",
      list_key: "https://example.com/a.html",
      list_name: "a.html",
      file_url: "https://example.com/a.html",
      file_name: "a.html",
      snapshot_at: "2026-05-30T10:00:00.000Z",
    });
    expect(payload?.customers).toEqual([
      {
        customer_id: "CUST-001",
        customer_name: "祝话",
        extra_info: {
          customer_id: "CUST-001",
          name: "祝话",
        },
      },
      {
        customer_id: null,
        customer_name: "程广泛",
        extra_info: {
          "客户姓名": "程广泛",
        },
      },
    ]);
  });

  it("listens to iframe clicks without blocking rejected reports", async () => {
    const iframe = document.createElement("iframe");
    document.body.appendChild(iframe);
    const doc = iframe.contentDocument!;
    doc.body.innerHTML =
      '<div><button id="follow"><span>立即跟进</span></button></div>';
    const reporter = vi.fn((_: HtmlTrackerPayloadType) =>
      Promise.reject(new Error("network error")),
    );

    const cleanup = attachHtmlPreviewClickTracker({
      iframe,
      metadata: {
        fileUrl: "https://example.com/a.html",
        fileName: "a.html",
      },
      reporter,
      listSnapshotReporter: vi.fn(),
    });

    const span = doc.querySelector("span") as HTMLElement;
    span.dispatchEvent(new doc.defaultView!.MouseEvent("click", { bubbles: true }));
    await Promise.resolve();

    expect(reporter).toHaveBeenCalledTimes(1);
    expect(reporter.mock.calls[0][0]).toMatchObject({
      button_id: "follow",
      button_name: "立即跟进",
      button_text: "立即跟进",
    });

    cleanup();
    document.body.removeChild(iframe);
  });

  it("does not throw when reporter fails synchronously", () => {
    const iframe = document.createElement("iframe");
    document.body.appendChild(iframe);
    const doc = iframe.contentDocument!;
    doc.body.innerHTML = '<button id="follow">立即跟进</button>';
    const reporter = vi.fn(() => {
      throw new Error("sync error");
    });

    const cleanup = attachHtmlPreviewClickTracker({
      iframe,
      metadata: {
        fileUrl: "https://example.com/a.html",
        fileName: "a.html",
      },
      reporter,
    });

    const button = doc.querySelector("button") as HTMLElement;
    expect(() =>
      button.dispatchEvent(
        new doc.defaultView!.MouseEvent("click", { bubbles: true }),
      ),
    ).not.toThrow();
    expect(reporter).toHaveBeenCalledTimes(1);

    cleanup();
    document.body.removeChild(iframe);
  });

  it("opens nested preview for marked links after recording the click", () => {
    const iframe = document.createElement("iframe");
    document.body.appendChild(iframe);
    const doc = iframe.contentDocument!;
    doc.body.innerHTML = `
      <a
        href="https://example.com/static/%E6%96%B9%E6%A1%88.html"
        data-preview-modal="true"
        data-track-id="view_plan"
        data-track-name="查看方案"
      >
        查看方案
      </a>
    `;
    const reporter = vi.fn();
    const onOpenNestedPreview = vi.fn();

    const cleanup = attachHtmlPreviewClickTracker({
      iframe,
      metadata: {
        fileUrl: "https://example.com/list.html",
        fileName: "list.html",
      },
      reporter,
      onOpenNestedPreview,
    });

    const link = doc.querySelector("a") as HTMLElement;
    const event = new doc.defaultView!.MouseEvent("click", {
      bubbles: true,
      cancelable: true,
    });
    link.dispatchEvent(event);

    expect(reporter).toHaveBeenCalledTimes(1);
    expect(reporter.mock.calls[0][0]).toMatchObject({
      button_id: "view_plan",
      button_name: "查看方案",
      button_type: "plan",
    });
    expect(event.defaultPrevented).toBe(true);
    expect(onOpenNestedPreview).toHaveBeenCalledWith({
      fileUrl: "https://example.com/static/%E6%96%B9%E6%A1%88.html",
      fileName: "方案.html",
      listKey: "https://example.com/list.html",
      listName: "list.html",
      customerInfo: null,
    });

    cleanup();
    document.body.removeChild(iframe);
  });

  it("does not intercept ordinary links", () => {
    const iframe = document.createElement("iframe");
    document.body.appendChild(iframe);
    const doc = iframe.contentDocument!;
    doc.body.innerHTML = '<a href="https://example.com/plain.html">普通链接</a>';
    const reporter = vi.fn();
    const onOpenNestedPreview = vi.fn();

    const cleanup = attachHtmlPreviewClickTracker({
      iframe,
      metadata: {
        fileUrl: "https://example.com/list.html",
        fileName: "list.html",
      },
      reporter,
      onOpenNestedPreview,
    });

    const link = doc.querySelector("a") as HTMLElement;
    const event = new doc.defaultView!.MouseEvent("click", {
      bubbles: true,
      cancelable: true,
    });
    link.dispatchEvent(event);

    expect(reporter).toHaveBeenCalledTimes(1);
    expect(event.defaultPrevented).toBe(false);
    expect(onOpenNestedPreview).not.toHaveBeenCalled();

    cleanup();
    document.body.removeChild(iframe);
  });

  it("keeps nested preview clicks under the parent list and falls back to parent customer info", () => {
    const doc = createDocument(
      '<a data-track-id="insight" data-track-name="洞察">客户洞察</a>',
    );
    const link = doc.querySelector("a") as HTMLElement;

    const payload = buildHtmlPreviewClickPayload(link, {
      fileUrl: "https://example.com/plan.html",
      fileName: "plan.html",
      listKey: "https://example.com/list.html",
      listName: "list.html",
      defaultCustomerInfo: {
        customer_id: "CUST-001",
        name: "张三",
      },
      pageSource: "wealth_workbench",
      platformSource: "wp",
    });

    expect(payload).toMatchObject({
      file_url: "https://example.com/plan.html",
      file_name: "plan.html",
      list_key: "https://example.com/list.html",
      list_name: "list.html",
      button_type: "insight",
      customer_id: "CUST-001",
      customer_name: "张三",
      customer_info: {
        customer_id: "CUST-001",
        name: "张三",
      },
      page_source: "wealth_workbench",
      platform_source: "wp",
    });
    expect(payload).not.toHaveProperty("plantform_source");
  });
});
