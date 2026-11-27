import {
  act,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import { beforeEach, expect, it, vi } from "vitest";
import SpanEvidence from "./SpanEvidence";

const request = vi.hoisted(() => vi.fn());
vi.mock("../../api/request", () => ({ request }));

beforeEach(() => request.mockReset());

it("loads on activation, displays API states, and continues UTF-8 bytes", async () => {
  const bytes = new TextEncoder().encode('{"text":"中文"}');
  const encode = (part: Uint8Array) =>
    btoa(String.fromCharCode(...part))
      .replace(/=/g, "")
      .replace(/\+/g, "-")
      .replace(/\//g, "_");
  request
    .mockResolvedValueOnce({
      items: [
        {
          evidence_ref: "a",
          status: "truncated",
          message_chunk: encode(bytes.slice(0, 10)),
          has_more: true,
          next_cursor: "cursor/+",
        },
        ...[
          "pending",
          "not_saved",
          "compacted",
          "unavailable",
          "unresolved",
        ].map((status) => ({ evidence_ref: status, status })),
      ],
    })
    .mockResolvedValueOnce({
      items: [
        {
          evidence_ref: "a",
          status: "available",
          chunk_offset: 10,
          message_chunk: encode(bytes.slice(10)),
          has_more: false,
        },
      ],
    });
  const view = render(
    <SpanEvidence
      runId="run"
      spanId="span"
      sourceKey="source"
      active={false}
    />,
  );
  expect(request).not.toHaveBeenCalled();
  view.rerender(
    <SpanEvidence runId="run" spanId="span" sourceKey="source" active />,
  );
  for (const label of ["待写入", "未留存", "已压缩", "不可用", "无法解析"])
    expect(await screen.findByText(new RegExp(label))).toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "继续加载" }));
  expect(await screen.findByText('{"text":"中文"}')).toBeInTheDocument();
  expect(request).toHaveBeenLastCalledWith(
    "/agent-observability/runs/run/spans/span/evidence?cursor=cursor%2F%2B",
  );
});

it("discards late responses on span and source changes", async () => {
  let resolveOld!: (value: unknown) => void;
  request
    .mockImplementationOnce(
      () =>
        new Promise((resolve) => {
          resolveOld = resolve;
        }),
    )
    .mockResolvedValueOnce({
      items: [{ evidence_ref: "new", status: "available", message: "new" }],
    })
    .mockResolvedValueOnce({ items: [] });
  const view = render(
    <SpanEvidence runId="r" spanId="old" sourceKey="a" active />,
  );
  view.rerender(<SpanEvidence runId="r" spanId="new" sourceKey="a" active />);
  expect(await screen.findByText('"new"')).toBeInTheDocument();
  await act(async () =>
    resolveOld({
      items: [{ evidence_ref: "old", status: "available", message: "old" }],
    }),
  );
  expect(screen.queryByText('"old"')).not.toBeInTheDocument();
  view.rerender(<SpanEvidence runId="r" spanId="new" sourceKey="b" active />);
  expect(screen.queryByText('"new"')).not.toBeInTheDocument();
  await waitFor(() =>
    expect(screen.getByText("暂无正文证据")).toBeInTheDocument(),
  );
});

it("discards an in-flight continuation after the source changes", async () => {
  let resolveMore!: (value: unknown) => void;
  request
    .mockResolvedValueOnce({
      items: [
        {
          evidence_ref: "a",
          status: "truncated",
          message_chunk: "ImE",
          has_more: true,
          next_cursor: "next",
        },
      ],
    })
    .mockImplementationOnce(
      () =>
        new Promise((resolve) => {
          resolveMore = resolve;
        }),
    )
    .mockResolvedValueOnce({ items: [] });
  const view = render(
    <SpanEvidence runId="r" spanId="s" sourceKey="a" active />,
  );
  fireEvent.click(await screen.findByRole("button", { name: "继续加载" }));
  view.rerender(<SpanEvidence runId="r" spanId="s" sourceKey="b" active />);
  expect(await screen.findByText("暂无正文证据")).toBeInTheDocument();
  await act(async () =>
    resolveMore({
      items: [
        {
          evidence_ref: "a",
          status: "available",
          chunk_offset: 2,
          message_chunk: "YiI",
          has_more: false,
        },
      ],
    }),
  );
  expect(screen.queryByText('"ab"')).not.toBeInTheDocument();
});

it.each(["unavailable", "unresolved", "truncated"])(
  "replaces stale content when continuation returns %s without a chunk",
  async (status) => {
    request
      .mockResolvedValueOnce({
        items: [
          {
            evidence_ref: "a",
            status: "truncated",
            message_chunk: "Im9sZA",
            next_cursor: "next",
            has_more: true,
          },
        ],
      })
      .mockResolvedValueOnce({ items: [{ evidence_ref: "a", status }] });
    render(<SpanEvidence runId="r" spanId="s" sourceKey="a" active />);
    fireEvent.click(await screen.findByRole("button", { name: "继续加载" }));
    await waitFor(() =>
      expect(
        screen.queryByRole("button", { name: "继续加载" }),
      ).not.toBeInTheDocument(),
    );
    expect(screen.queryByText('"old')).not.toBeInTheDocument();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  },
);

it("clears changed content and reloads from the beginning after a conflict", async () => {
  request
    .mockResolvedValueOnce({
      items: [
        {
          evidence_ref: "a",
          status: "truncated",
          message_chunk: "Im9sZA",
          next_cursor: "next",
          has_more: true,
        },
      ],
    })
    .mockRejectedValueOnce(Object.assign(new Error(), { status: 409 }))
    .mockResolvedValueOnce({
      items: [{ evidence_ref: "a", status: "available", message: "new" }],
    });
  render(<SpanEvidence runId="r" spanId="s" sourceKey="a" active />);
  fireEvent.click(await screen.findByRole("button", { name: "继续加载" }));
  fireEvent.click(await screen.findByRole("button", { name: "重新读取" }));
  expect(await screen.findByText('"new"')).toBeInTheDocument();
  expect(request).toHaveBeenLastCalledWith(
    "/agent-observability/runs/r/spans/s/evidence",
  );
});
