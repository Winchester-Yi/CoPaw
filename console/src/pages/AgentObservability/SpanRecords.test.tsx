import { act, fireEvent, render, screen } from "@testing-library/react";
import { MemoryRouter, Route, Routes, useLocation } from "react-router-dom";
import { beforeEach, expect, it, vi } from "vitest";
import SpanRecords from "./SpanRecords";

const request = vi.hoisted(() => vi.fn());
vi.mock("../../api/request", () => ({ request }));
beforeEach(() => request.mockReset());

function Destination() {
  const location = useLocation();
  return (
    <div>
      目标：{location.pathname}
      {location.search}
    </div>
  );
}

it("continues events in server order and opens a deep link", async () => {
  request
    .mockResolvedValueOnce({
      items: [
        {
          event_id: "1",
          event_name: "first",
          metadata: {
            links: [
              {
                relation_type: "retry_of",
                obs_attempt_id: "attempt-1",
                obs_span_id: "span-1",
              },
              { relation_type: "other", target_unavailable: true },
            ],
          },
        },
      ],
      next_cursor: "cursor/+",
    })
    .mockResolvedValueOnce({
      items: [{ event_id: "2", event_name: "second" }],
      next_cursor: null,
    });
  render(
    <MemoryRouter initialEntries={["/here?hideMenu=true"]}>
      <Routes>
        <Route
          path="/here"
          element={
            <SpanRecords
              runId="run"
              spanId="s"
              sourceKey="source"
              tab="events"
              active
            />
          }
        />
        <Route path="/agent-observability/runs/:id" element={<Destination />} />
      </Routes>
    </MemoryRouter>,
  );
  expect(await screen.findByText("first")).toBeInTheDocument();
  expect(screen.getByText("other · 目标不可用")).toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "继续加载" }));
  expect(await screen.findByText("second")).toBeInTheDocument();
  expect(request).toHaveBeenLastCalledWith(
    "/agent-observability/runs/run/events?span_id=s&cursor=cursor%2F%2B",
  );
  fireEvent.click(
    screen.getByRole("link", { name: "retry_of · 打开关联 Run" }),
  );
  expect(
    screen.getByText(
      "目标：/agent-observability/runs/run?attempt=attempt-1&span=span-1&hideMenu=true",
    ),
  ).toBeInTheDocument();
});

it("paginates assets and discards a late page after switching spans", async () => {
  let resolveOld!: (value: unknown) => void;
  request
    .mockResolvedValueOnce({
      items: [
        {
          asset_ref: "a",
          asset_type: "model",
          asset_id: "old",
          asset_source: "runtime",
        },
      ],
      next_cursor: "next",
    })
    .mockImplementationOnce(
      () =>
        new Promise((resolve) => {
          resolveOld = resolve;
        }),
    )
    .mockResolvedValueOnce({
      items: [{ asset_ref: "b", asset_id: "new", asset_source: "catalog" }],
      next_cursor: null,
    });
  const view = render(
    <MemoryRouter>
      <SpanRecords runId="r" spanId="old" sourceKey="a" tab="assets" active />
    </MemoryRouter>,
  );
  expect(await screen.findByText(/来源：runtime/)).toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "继续加载" }));
  view.rerender(
    <MemoryRouter>
      <SpanRecords runId="r" spanId="new" sourceKey="a" tab="assets" active />
    </MemoryRouter>,
  );
  expect(screen.queryByText(/old/)).not.toBeInTheDocument();
  expect(await screen.findByText(/来源：catalog/)).toBeInTheDocument();
  await act(async () =>
    resolveOld({
      items: [{ asset_ref: "late", asset_id: "late" }],
      next_cursor: null,
    }),
  );
  expect(screen.queryByText(/late/)).not.toBeInTheDocument();
  expect(request).toHaveBeenCalledWith(
    "/agent-observability/runs/r/assets?span_id=old&cursor=next",
  );
});

it("loads and paginates links independently, and discards old context responses", async () => {
  let resolveOld!: (value: unknown) => void;
  request
    .mockResolvedValueOnce({
      items: [{ event_id: "e", event_name: "event" }],
      next_cursor: "event-cursor",
    })
    .mockResolvedValueOnce({
      items: [{ event_id: "l", event_name: "link-one" }],
      next_cursor: "link-cursor",
    })
    .mockImplementationOnce(
      () =>
        new Promise((resolve) => {
          resolveOld = resolve;
        }),
    )
    .mockResolvedValueOnce({
      items: [{ event_id: "new-e", event_name: "new-event" }],
      next_cursor: null,
    })
    .mockResolvedValueOnce({
      items: [{ event_id: "new-l", event_name: "new-link" }],
      next_cursor: null,
    });
  const view = render(
    <MemoryRouter>
      <SpanRecords runId="r" spanId="old" sourceKey="a" tab="events" active />
    </MemoryRouter>,
  );
  expect(await screen.findByText("event")).toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "关联" }));
  expect(await screen.findByText("link-one")).toBeInTheDocument();
  expect(request).toHaveBeenCalledWith(
    "/agent-observability/runs/r/events?span_id=old&view=links",
  );
  fireEvent.click(screen.getByRole("button", { name: "事件" }));
  expect(screen.getByText("event")).toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "关联" }));
  expect(screen.getByText("link-one")).toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "继续加载" }));
  expect(request).toHaveBeenLastCalledWith(
    "/agent-observability/runs/r/events?span_id=old&view=links&cursor=link-cursor",
  );
  view.rerender(
    <MemoryRouter>
      <SpanRecords runId="r" spanId="new" sourceKey="b" tab="events" active />
    </MemoryRouter>,
  );
  expect(screen.queryByText("link-one")).not.toBeInTheDocument();
  expect(await screen.findByText("new-event")).toBeInTheDocument();
  expect(request).not.toHaveBeenCalledWith(
    "/agent-observability/runs/r/events?span_id=new&view=links",
  );
  await act(async () =>
    resolveOld({
      items: [{ event_id: "late", event_name: "late-link" }],
      next_cursor: null,
    }),
  );
  expect(screen.queryByText("late-link")).not.toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "关联" }));
  expect(await screen.findByText("new-link")).toBeInTheDocument();
  expect(request).toHaveBeenCalledWith(
    "/agent-observability/runs/r/events?span_id=new&view=links",
  );
});
