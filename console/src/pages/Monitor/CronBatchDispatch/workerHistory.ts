import dayjs from "dayjs";
import type {
  CronDispatchCapacityItem,
  CronDispatchWorkersResponse,
} from "../../../api/modules/monitor";

export async function loadWorkerHistory(
  loadPage: (cursor?: string) => Promise<CronDispatchWorkersResponse>,
  isCurrent: () => boolean,
  onProgress: (count: number) => void = () => {},
): Promise<CronDispatchWorkersResponse | null> {
  let first: CronDispatchWorkersResponse | undefined;
  let cursor: string | undefined;
  const seen = new Set<string>();
  const records = new Map<number, CronDispatchCapacityItem>();
  do {
    if (!isCurrent()) return null;
    const page = await loadPage(cursor);
    if (!isCurrent()) return null;
    first ??= page;
    if (page.source_id !== first.source_id)
      throw new Error("Worker 历史渠道不一致，请刷新");
    for (const row of page.capacity_events) records.set(row.id, row);
    onProgress(records.size);
    cursor = page.capacity_events_next_cursor || undefined;
    if (cursor && seen.has(cursor))
      throw new Error("Worker 历史分页未推进，请刷新");
    if (cursor) seen.add(cursor);
  } while (cursor);
  return {
    ...first!,
    capacity_events: [...records.values()],
    capacity_events_next_cursor: null,
  };
}

export interface WorkerPoint {
  value: [number, number];
  record: CronDispatchCapacityItem;
}

export function buildWorkerSeries(items: CronDispatchCapacityItem[]) {
  const groups = new Map<string, { name: string; data: WorkerPoint[] }>();
  for (const record of items) {
    const time = record.created_at ? dayjs(record.created_at).valueOf() : NaN;
    if (!Number.isFinite(time) || !Number.isFinite(record.effective_workers))
      continue;
    const key = JSON.stringify([record.provider_id, record.model_id]);
    const group = groups.get(key) || {
      name: `${record.provider_id} / ${record.model_id}`,
      data: [],
    };
    group.data.push({ value: [time, record.effective_workers], record });
    groups.set(key, group);
  }
  return [...groups]
    .sort(([a], [b]) => a.localeCompare(b))
    .map(([id, group]) => ({
      id,
      name: group.name,
      type: "line" as const,
      step: "end" as const,
      showSymbol: group.data.length <= 30,
      data: group.data.sort(
        (a, b) => a.value[0] - b.value[0] || a.record.id - b.record.id,
      ),
    }));
}
