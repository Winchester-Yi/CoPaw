vi.mock("./TrackingSelect", () => ({
  TrackingSelect: ({
    options = [],
    value,
    onChange,
    ...props
  }: Omit<React.SelectHTMLAttributes<HTMLSelectElement>, "onChange"> & {
    options?: { value: string | number; label: React.ReactNode }[];
    onChange?: (value?: string | number) => void;
  }) => (
    <select
      {...props}
      value={value ?? ""}
      onChange={(event) =>
        onChange?.(
          options.find((option) => String(option.value) === event.target.value)
            ?.value,
        )
      }
    >
      {value == null || value === "" ? <option value="" /> : null}
      {options.map((option) => (
        <option key={option.value} value={option.value}>
          {option.label}
        </option>
      ))}
    </select>
  ),
}));
import {
  act,
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { InsuranceEditor } from "./Editor";
import { fetchSceneSkills } from "../../api";
import type { InsuranceCustomer } from "./api";

vi.mock("./TrackingDatePicker", async (importOriginal) => {
  const original = await importOriginal<
    typeof import("./TrackingDatePicker")
  >();
  return {
    ...original,
    TrackingDatePicker: ({
      value,
      onChange,
      withTime,
      ...props
    }: Parameters<typeof original.TrackingDatePicker>[0]) => (
      <input
        {...props}
        type={withTime ? "datetime-local" : "date"}
        value={value ?? ""}
        onChange={(event) => onChange?.(event.target.value)}
      />
    ),
  };
});
vi.mock("../../api", async (original) => ({
  ...(await original<typeof import("../../api")>()),
  fetchSceneSkills: vi.fn(),
}));
const customer: InsuranceCustomer = {
  id: "1",
  custUid: "c1",
  name: "林芳",
  intention: 0,
  skillId: "main",
  sceneName: "主记录场景",
};
const record = {
  id: "11",
  date: "2026-11-05T10:00:00",
  content: "原沟通内容",
  method: 0 as const,
  result: 0 as const,
  skillId: "historic",
  sceneName: "历史跟进场景",
};
const scene = (skillId: string, name: string, category = "insurance") => ({
  skillId,
  senceName: name,
  category,
  mcpRelationList: [],
});
const save = vi.fn();
const originalShowModal = HTMLDialogElement.prototype.showModal;
const originalClose = HTMLDialogElement.prototype.close;
beforeEach(() => {
  vi.resetAllMocks();
  HTMLDialogElement.prototype.showModal = function () {
    this.setAttribute("open", "");
  };
  HTMLDialogElement.prototype.close = function () {
    this.removeAttribute("open");
  };
  vi.mocked(fetchSceneSkills).mockResolvedValue([
    scene("new", "保险新场景"),
    scene("finance", "理财场景", "finance"),
  ]);
});
afterEach(() => {
  cleanup();
  HTMLDialogElement.prototype.showModal = originalShowModal;
  HTMLDialogElement.prototype.close = originalClose;
});
function show(props: Partial<Parameters<typeof InsuranceEditor>[0]> = {}) {
  return render(
    <InsuranceEditor
      customer={customer}
      kind="followUp"
      onSave={save}
      onClose={vi.fn()}
      {...props}
    />,
  );
}
function fillRequired() {
  fireEvent.change(screen.getByLabelText("触达时间"), {
    target: { value: "2026-11-05T10:30" },
  });
  fireEvent.change(screen.getByLabelText(/沟通记录/), {
    target: { value: "沟通内容" },
  });
}
it("新增默认主表技能，未命中时补入，选中保险新场景后ID和名称一起保存", async () => {
  show();
  const select = screen.getByLabelText("经营场景");
  expect(select).toHaveValue("main");
  await screen.findByRole("option", { name: "保险新场景" });
  expect(fetchSceneSkills).toHaveBeenCalledWith(
    "insurance",
    expect.any(AbortSignal),
  );
  expect(
    within(select).getByRole("option", { name: "主记录场景" }),
  ).toBeInTheDocument();
  expect(
    screen.queryByRole("option", { name: "理财场景" }),
  ).not.toBeInTheDocument();
  fireEvent.change(select, { target: { value: "new" } });
  fillRequired();
  fireEvent.click(screen.getByRole("button", { name: "保存记录" }));
  await waitFor(() =>
    expect(save).toHaveBeenCalledWith(
      expect.objectContaining({
        kind: "followUp",
        value: expect.objectContaining({
          skillId: "new",
          sceneName: "保险新场景",
        }),
      }),
    ),
  );
});
it("编辑默认单条trace技能，未命中仍可选，不能误用主表技能", async () => {
  show({ record });
  await screen.findByRole("option", { name: "保险新场景" });
  expect(screen.getByLabelText("经营场景")).toHaveValue("historic");
  expect(
    screen.getByRole("option", { name: "历史跟进场景" }),
  ).toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "保存记录" }));
  await waitFor(() =>
    expect(save).toHaveBeenCalledWith(
      expect.objectContaining({
        recordId: "11",
        value: expect.objectContaining({
          skillId: "historic",
          sceneName: "历史跟进场景",
        }),
      }),
    ),
  );
});
it("默认ID已存在不重复，提交名称与可见选项一致", async () => {
  vi.mocked(fetchSceneSkills).mockResolvedValue([
    scene("main", "场景最新名称"),
  ]);
  show();
  await screen.findByRole("option", { name: "场景最新名称" });
  const select = screen.getByLabelText("经营场景");
  expect(
    within(select)
      .getAllByRole("option")
      .filter((option) => (option as HTMLOptionElement).value === "main"),
  ).toHaveLength(1);
  fillRequired();
  fireEvent.click(screen.getByRole("button", { name: "保存记录" }));
  await waitFor(() =>
    expect(save).toHaveBeenCalledWith(
      expect.objectContaining({
        value: expect.objectContaining({
          skillId: "main",
          sceneName: "场景最新名称",
        }),
      }),
    ),
  );
});
it("查询失败可保留默认值，重试后仍保留用户当前选择", async () => {
  vi.mocked(fetchSceneSkills).mockRejectedValueOnce(new Error("网络错误"));
  show();
  await screen.findByRole("alert");
  expect(screen.getByLabelText("经营场景")).toHaveValue("main");
  fireEvent.click(screen.getByRole("button", { name: "重新加载" }));
  await screen.findByRole("option", { name: "保险新场景" });
  fireEvent.change(screen.getByLabelText("经营场景"), {
    target: { value: "new" },
  });
  expect(screen.getByLabelText("经营场景")).toHaveValue("new");
});
it("失败草稿优先于记录默认技能，空缺技能时必须选择后才可保存", async () => {
  const { unmount } = show({
    record,
    draft: {
      kind: "followUp",
      recordId: "11",
      requestId: "local",
      value: { ...record, skillId: "draft", sceneName: "草稿场景" },
    },
  });
  expect(screen.getByLabelText("经营场景")).toHaveValue("draft");
  unmount();
  show({ record: { ...record, skillId: undefined, sceneName: undefined } });
  await screen.findByRole("option", { name: "保险新场景" });
  expect(screen.getByLabelText("经营场景")).toHaveValue("");
  fireEvent.click(screen.getByRole("button", { name: "保存记录" }));
  await screen.findByText("请选择经营场景", {
    selector: ".ant-form-item-explain-error",
  });
  expect(save).not.toHaveBeenCalled();
});
it("卸载取消选项请求，迟到响应不更新已经关闭的弹窗", async () => {
  let resolve!: (items: Awaited<ReturnType<typeof fetchSceneSkills>>) => void;
  vi.mocked(fetchSceneSkills).mockImplementationOnce(
    () =>
      new Promise((yes) => {
        resolve = yes;
      }),
  );
  const { unmount } = show();
  const signal = vi.mocked(fetchSceneSkills).mock.calls[0][1];
  unmount();
  expect(signal?.aborted).toBe(true);
  await act(async () => resolve([scene("late", "迟到场景")]));
  expect(
    screen.queryByRole("option", { name: "迟到场景" }),
  ).not.toBeInTheDocument();
});
