import {
  cleanup,
  fireEvent,
  render,
  screen,
  within,
} from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { RecordCell } from "./RecordCell";

const originalShowModal = HTMLDialogElement.prototype.showModal;
const originalClose = HTMLDialogElement.prototype.close;
beforeEach(() => {
  HTMLDialogElement.prototype.showModal = function () {
    this.setAttribute("open", "");
  };
  HTMLDialogElement.prototype.close = function () {
    this.removeAttribute("open");
  };
});
afterEach(() => {
  cleanup();
  HTMLDialogElement.prototype.showModal = originalShowModal;
  HTMLDialogElement.prototype.close = originalClose;
});
it.each(["contactPlan", "followUp"] as const)(
  "%s 编辑入口先展示全部，再编辑单条",
  (kind) => {
    const entries = [
      { id: "a", date: "2026-11-06", method: 0 as const, content: "最新内容" },
      { id: "b", date: "2026-11-05", method: 1 as const, content: "历史内容" },
    ];
    const onEdit = vi.fn();
    render(
      <RecordCell
        entries={entries}
        kind={kind}
        customerName="林芳"
        disabled={false}
        onEdit={onEdit}
        onAdd={vi.fn()}
      />,
    );
    expect(
      screen.queryByRole("button", { name: /编辑林芳/ }),
    ).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: /查看林芳的全部/ }));
    expect(screen.getByText("编辑 (2)")).toBeInTheDocument();
    expect(onEdit).not.toHaveBeenCalled();
    const dialog = screen.getByRole("dialog");
    expect(within(dialog).getByText("最新内容")).toBeInTheDocument();
    expect(within(dialog).getByText("历史内容")).toBeInTheDocument();
    fireEvent.click(within(dialog).getByRole("button", { name: /2026-11-05/ }));
    expect(onEdit).toHaveBeenCalledWith(entries[1]);
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  },
);
it("只有一条时也先进入管理弹窗，新增保留原入口", () => {
  const onAdd = vi.fn(),
    onEdit = vi.fn();
  render(
    <RecordCell
      entries={[{ id: "a", date: "2026-11-06", method: 0, content: "计划" }]}
      kind="contactPlan"
      customerName="林芳"
      disabled={false}
      onEdit={onEdit}
      onAdd={onAdd}
    />,
  );
  fireEvent.click(screen.getByRole("button", { name: /查看林芳的全部/ }));
  const dialog = screen.getByRole("dialog");
  expect(within(dialog).getByText("共 1 条")).toBeInTheDocument();
  fireEvent.click(within(dialog).getByRole("button", { name: "新增计划" }));
  expect(onAdd).toHaveBeenCalledOnce();
  expect(onEdit).not.toHaveBeenCalled();
});
