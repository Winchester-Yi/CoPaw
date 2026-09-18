import { useState } from "react";
import {
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import { OrganizationFilters } from "./OrganizationFilters";
import { getTaskReportOptions } from "../../../api/modules/taskTypeReport";
vi.mock("../../../api/modules/taskTypeReport", () => ({
  getTaskReportOptions: vi.fn(),
  taskReportError: (error: Error) => error.message,
}));
afterEach(cleanup);
function Filters() {
  const [branch, setBranch] = useState<string>();
  const [org, setOrg] = useState<string>();
  return (
    <OrganizationFilters
      endDate="2026-09-14"
      bbk="100"
      branch={branch}
      org={org}
      showOrg
      onBranchChange={(value) => {
        setBranch(value);
        setOrg(undefined);
      }}
      onOrgChange={setOrg}
    />
  );
}
it("选择分行后查询名单支行，切换分行清空旧支行", async () => {
  vi.mocked(getTaskReportOptions).mockImplementation(async (params) => ({
    sync_date: params.end_date,
    items:
      params.kind === "branches"
        ? [
            { value: "110", label: "北京分行" },
            { value: "120", label: "广州分行" },
          ]
        : [
            {
              value: `${params.first_bbk_id}01`,
              label: `${params.first_bbk_id}营业部`,
            },
          ],
  }));
  render(<Filters />);
  expect(screen.getByRole("combobox", { name: "支行" })).toBeDisabled();
  fireEvent.mouseDown(screen.getByRole("combobox", { name: "分行" }));
  fireEvent.click(await screen.findByText("北京分行"));
  await waitFor(() =>
    expect(getTaskReportOptions).toHaveBeenLastCalledWith(
      { kind: "orgs", end_date: "2026-09-14", first_bbk_id: "110" },
      expect.any(AbortSignal),
    ),
  );
  await waitFor(() =>
    expect(screen.getByRole("combobox", { name: "支行" })).toBeEnabled(),
  );
  fireEvent.mouseDown(screen.getByRole("combobox", { name: "支行" }));
  fireEvent.click(await screen.findByText("110营业部"));
  fireEvent.mouseDown(screen.getByRole("combobox", { name: "分行" }));
  fireEvent.click(await screen.findByText("广州分行"));
  await waitFor(() =>
    expect(getTaskReportOptions).toHaveBeenLastCalledWith(
      expect.objectContaining({ kind: "orgs", first_bbk_id: "120" }),
      expect.any(AbortSignal),
    ),
  );
  expect(screen.getByText("全部支行")).toBeInTheDocument();
}, 30000);
