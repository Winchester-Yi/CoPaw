import { act, cleanup, renderHook, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import {
  getTaskTypeReport,
  type ReportParams,
  type ReportResponse,
  type ReportRow,
} from "../../../api/modules/taskTypeReport";
import { buildTaskReportDemo } from "../../../api/modules/taskTypeReportDemo";
import { useReport } from "./useReport";
vi.mock("../../../api/modules/taskTypeReport", async (importOriginal) => ({
  ...(await importOriginal<
    typeof import("../../../api/modules/taskTypeReport")
  >()),
  getTaskTypeReport: vi.fn(),
}));
const base: ReportParams = {
  start_date: "2026-09-01",
  end_date: "2026-09-14",
  group_by: "manager",
  task_type: "push_plan",
};
const sample = buildTaskReportDemo(base).items[0];
function response(page: number, ids: string[], more: boolean): ReportResponse {
  return {
    sync_date: "2026-09-14",
    warnings: [],
    items: ids.map((user_id): ReportRow => ({ ...sample, user_id })),
    page,
    page_size: 20,
    total: 40,
    has_more: more,
  };
}
afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});
describe("滚动加载", () => {
  it("branch loads once and exposes 20 then remaining rows locally", async () => {
    vi.mocked(getTaskTypeReport).mockResolvedValue(
      response(
        1,
        Array.from({ length: 25 }, (_, i) => String(i)),
        false,
      ),
    );
    const { result } = renderHook(() =>
      useReport({ ...base, group_by: "branch" }, 0),
    );
    await waitFor(() => expect(result.current.rows).toHaveLength(20));
    await act(() => result.current.loadMore());
    expect(result.current.rows).toHaveLength(25);
    expect(getTaskTypeReport).toHaveBeenCalledTimes(1);
    expect(getTaskTypeReport).toHaveBeenCalledWith(
      expect.not.objectContaining({ page: 1 }),
      expect.any(AbortSignal),
    );
  });
  it("manager requests page 2 once, preserves rows on failure, and retries the same page", async () => {
    vi.mocked(getTaskTypeReport)
      .mockResolvedValueOnce(response(1, ["a"], true))
      .mockRejectedValueOnce(new Error("第二页失败"))
      .mockResolvedValueOnce(response(2, ["b"], false));
    const { result } = renderHook(() => useReport(base, 0));
    await waitFor(() => expect(result.current.rows).toHaveLength(1));
    await act(async () => {
      await Promise.all([result.current.loadMore(), result.current.loadMore()]);
    });
    expect(getTaskTypeReport).toHaveBeenCalledTimes(2);
    expect(result.current.rows[0].user_id).toBe("a");
    expect(result.current.error).toBe("第二页失败");
    await act(() => result.current.loadMore());
    expect(result.current.rows.map((row) => row.user_id)).toEqual(["a", "b"]);
    expect(getTaskTypeReport).toHaveBeenLastCalledWith(
      expect.objectContaining({ page: 2, page_size: 20 }),
      expect.any(AbortSignal),
    );
    expect(result.current.hasMore).toBe(false);
  });
  it("filter changes abort and ignore old requests", async () => {
    let resolveOld: (value: ReportResponse) => void = () => undefined;
    vi.mocked(getTaskTypeReport)
      .mockImplementationOnce(
        () =>
          new Promise((resolve) => {
            resolveOld = resolve;
          }),
      )
      .mockResolvedValueOnce(response(1, ["new"], false));
    const { result, rerender } = renderHook(
      ({ keyword }) => useReport({ ...base, keyword }, 0),
      { initialProps: { keyword: "old" } },
    );
    const oldSignal = vi.mocked(getTaskTypeReport).mock.calls[0][1];
    rerender({ keyword: "new" });
    await waitFor(() => expect(result.current.rows[0]?.user_id).toBe("new"));
    await act(async () => resolveOld(response(1, ["old"], false)));
    expect(oldSignal?.aborted).toBe(true);
    expect(result.current.rows[0]?.user_id).toBe("new");
  });
});
