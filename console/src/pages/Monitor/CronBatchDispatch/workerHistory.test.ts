import { expect, it } from "vitest";
import type {
  CronDispatchCapacityItem,
  CronDispatchWorkersResponse,
} from "../../../api/modules/monitor";
import { buildWorkerSeries, loadWorkerHistory } from "./workerHistory";

const row = (id: number, provider: string, time: string, workers: number) =>
  ({
    id,
    provider_id: provider,
    model_id: "same-model",
    created_at: time,
    effective_workers: workers,
  }) as CronDispatchCapacityItem;
const page = (items: CronDispatchCapacityItem[], cursor: string | null) =>
  ({
    source_id: "s",
    policies: [],
    current_capacity: [],
    capacity_events: items,
    capacity_events_next_cursor: cursor,
  }) as CronDispatchWorkersResponse;

it("loads all pages and stops superseded requests", async () => {
  const cursors: (string | undefined)[] = [];
  const result = await loadWorkerHistory(
    async (cursor) => {
      cursors.push(cursor);
      return cursor
        ? page([row(1, "p", "2026-09-09T08:00:00", 1)], null)
        : page([row(2, "p", "2026-09-09T09:00:00", 2)], "next");
    },
    () => true,
  );
  expect(cursors).toEqual([undefined, "next"]);
  expect(result?.capacity_events.map((x) => x.id)).toEqual([2, 1]);
  let current = true;
  const cancelled = await loadWorkerHistory(
    async () => {
      current = false;
      return page([], "next");
    },
    () => current,
  );
  expect(cancelled).toBeNull();
});

it("separates providers and sorts equal timestamps by record ID", () => {
  const series = buildWorkerSeries([
    row(2, "p1", "2026-09-09T08:00:00", 4),
    row(1, "p1", "2026-09-09T08:00:00", 2),
    row(3, "p2", "2026-09-09T08:00:00", 8),
  ]);
  expect(series).toHaveLength(2);
  expect(series[0].data.map((x) => x.value[1])).toEqual([2, 4]);
  expect(series[1].data.map((x) => x.value[1])).toEqual([8]);
  expect(series[0].step).toBe("end");
  expect(buildWorkerSeries([])).toEqual([]);
});

it("does not return a partial history after a page fails", async () => {
  await expect(
    loadWorkerHistory(
      async (cursor) => {
        if (cursor) throw new Error("page failed");
        return page([row(1, "p", "2026-09-09T08:00:00", 1)], "next");
      },
      () => true,
    ),
  ).rejects.toThrow("page failed");
});
