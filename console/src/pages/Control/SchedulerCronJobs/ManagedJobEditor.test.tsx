import {
  cleanup,
  render,
  screen,
  waitFor,
  fireEvent,
} from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import { request } from "@/api/request";
import { ManagedJobEditor } from "./ManagedJobEditor";

vi.mock("@/api/request", () => ({ request: vi.fn() }));
vi.mock("@agentscope-ai/design", async (original) => ({
  ...(await original<object>()),
  Drawer: (await import("antd")).Drawer,
}));
vi.mock("@/api", () => ({
  default: {
    listSweSkills: vi.fn().mockResolvedValue({ skills: [] }),
    getUserTimezone: vi.fn().mockResolvedValue({ timezone: "Asia/Shanghai" }),
  },
}));
vi.mock("@/hooks/useExecutionModelOptions", async (original) => ({
  ...(await original<object>()),
  useExecutionModelOptions: () => ({
    options: [],
    loading: false,
    tenantDefaultLabel: "默认模型",
  }),
}));
afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});
const context = {
  actor_id: "owner",
  tenant_id: "owner",
  source_id: "bank",
  agent_id: "default",
};
const job = {
  id: "time-job",
  name: "十点任务",
  enabled: true,
  schedule: { type: "cron", cron: "0 10 * * *", timezone: "Asia/Shanghai" },
  task_type: "text",
  text: "test",
  dispatch: {
    type: "channel",
    channel: "console",
    target: { user_id: "owner", session_id: "cron-session" },
    mode: "final",
  },
  runtime: {
    max_concurrency: 1,
    timeout_seconds: 7200,
    misfire_grace_seconds: 300,
  },
  meta: {},
  management_owner: "scheduler",
  tenant_id: "owner",
  source_id: "bank",
  agent_id: "default",
  definition_version: 2,
  management_version: 2,
  state_version: 2,
  management_status: "ready",
  deleted: false,
  dispatch_state: "ordinary",
} as const;

it.each([true, false])(
  "父任务子任务存在=%s，日期单独清空只走日期接口",
  async (hasChildren) => {
    const dated = {
      ...job,
      run_date_range: { start_date: "2026-11-01", end_date: "2026-11-30" },
    };
    vi.mocked(request).mockImplementation(async (url) => {
      if (url.includes("/broadcast/children"))
        return { items: hasChildren ? [{ id: "child" }] : [] };
      if (url.endsWith("/run-date-range"))
        return { operation: { status: "completed", operation_id: "dates" } };
      return { ...job, spec: dated };
    });
    const save = vi.fn();
    const view = render(
      <ManagedJobEditor
        context={context}
        job={dated}
        saving={false}
        onSave={save}
        onClose={() => {}}
      />,
    );
    expect(await screen.findByDisplayValue("2026-11-01")).not.toBeDisabled();
    expect(screen.getByDisplayValue("十点任务")).toHaveProperty(
      "disabled",
      hasChildren,
    );
    fireEvent.click(
      view.baseElement.querySelector(".ant-picker-range .ant-picker-clear")!,
    );
    fireEvent.click(screen.getByRole("button", { name: /保\s*存/ }));
    await waitFor(() =>
      expect(
        vi
          .mocked(request)
          .mock.calls.some(([url]) => url.endsWith("/run-date-range")),
      ).toBe(true),
    );
    expect(save).not.toHaveBeenCalled();
  },
);

it("列表10点的任务打开真实编辑表单应为10点，原样保存仍为10点", async () => {
  const { schedule, ...rest } = job;
  vi.mocked(request).mockResolvedValue({
    ...rest,
    spec: { ...job, schedule },
    items: [],
  });
  const save = vi
    .fn()
    .mockResolvedValue({ operation_id: "save", status: "completed" });
  render(
    <ManagedJobEditor
      context={context}
      job={job}
      saving={false}
      onSave={save}
      onClose={() => {}}
    />,
  );
  await screen.findByDisplayValue("十点任务");
  await waitFor(() =>
    expect(screen.getByDisplayValue("10:00")).toBeInTheDocument(),
  );
  fireEvent.click(screen.getByRole("button", { name: /保\s*存/ }));
  await waitFor(() => expect(save).toHaveBeenCalled());
  expect(save.mock.calls[0][1].schedule).toMatchObject({
    cron: "0 10 * * *",
    timezone: "Asia/Shanghai",
  });
});

it("同时修改名称和日期，只提交一次普通更新", async () => {
  const dated = {
    ...job,
    run_date_range: { start_date: "2026-11-01", end_date: "2026-11-30" },
  };
  vi.mocked(request).mockImplementation(async (url) =>
    url.includes("/broadcast/children")
      ? { items: [] }
      : { ...job, spec: dated },
  );
  const save = vi.fn().mockResolvedValue({ status: "completed" });
  const view = render(
    <ManagedJobEditor
      context={context}
      job={dated}
      saving={false}
      onSave={save}
      onClose={() => {}}
    />,
  );
  await screen.findByDisplayValue("2026-11-01");
  fireEvent.change(screen.getByDisplayValue("十点任务"), {
    target: { value: "新名称" },
  });
  fireEvent.click(
    view.baseElement.querySelector(".ant-picker-range .ant-picker-clear")!,
  );
  fireEvent.click(screen.getByRole("button", { name: /保\s*存/ }));
  await waitFor(() => expect(save).toHaveBeenCalledTimes(1));
  expect(save.mock.calls[0][1]).toMatchObject({
    name: "新名称",
    run_date_range: null,
  });
  expect(
    vi
      .mocked(request)
      .mock.calls.some(([url]) => url.endsWith("/run-date-range")),
  ).toBe(false);
});

it("子任务日期和调度只读，不查询自己的子任务或提交父组设置", async () => {
  const child = {
    ...job,
    meta: { broadcast_source_job_id: "parent" },
    run_date_range: { start_date: "2026-11-01", end_date: "2026-11-30" },
  };
  vi.mocked(request).mockResolvedValue({
    ...job,
    meta: child.meta,
    spec: child,
  });
  render(
    <ManagedJobEditor
      context={context}
      job={child}
      saving={false}
      onSave={vi.fn()}
      onClose={() => {}}
    />,
  );
  expect(await screen.findByDisplayValue("2026-11-01")).toBeDisabled();
  fireEvent.click(screen.getByRole("tab", { name: "调度设置" }));
  await screen.findByText(/继承父任务的调度配置/);
  expect(screen.queryByRole("button", { name: "提交切换" })).toBeNull();
  expect(
    vi
      .mocked(request)
      .mock.calls.some(([url]) => url.includes("/broadcast/children")),
  ).toBe(false);
});

it("修改后关闭先确认，取消关闭保留表单和草稿", async () => {
  vi.mocked(request).mockResolvedValue({ ...job, spec: job, items: [] });
  const close = vi.fn();
  render(
    <ManagedJobEditor
      context={context}
      job={job}
      saving={false}
      onSave={vi.fn()}
      onClose={close}
    />,
  );
  await screen.findByDisplayValue("十点任务");
  fireEvent.change(screen.getByDisplayValue("十点任务"), {
    target: { value: "未保存草稿" },
  });
  fireEvent.click(screen.getByRole("button", { name: /取\s*消/ }));
  expect(
    (await screen.findAllByText("有尚未保存的修改")).length,
  ).toBeGreaterThan(0);
  expect(close).not.toHaveBeenCalled();
  fireEvent.click(screen.getByRole("button", { name: "继续编辑" }));
  expect(screen.getByDisplayValue("未保存草稿")).toBeInTheDocument();
});

it("低于列表定义版本的详情不允许回填旧时间并提交", async () => {
  vi.mocked(request).mockResolvedValue({
    ...job,
    definition_version: 1,
    spec: { ...job, schedule: { ...job.schedule, cron: "0 9 * * *" } },
  });
  const save = vi.fn();
  render(
    <ManagedJobEditor
      context={context}
      job={job}
      saving={false}
      onSave={save}
      onClose={() => {}}
    />,
  );
  await screen.findByText("任务详情版本落后于列表，请刷新后重新编辑。");
  expect(screen.getByRole("button", { name: /保\s*存/ })).toBeDisabled();
  expect(save).not.toHaveBeenCalled();
});

it("新表单可选月末工作日与非工作日开关，并按新协议保存", async () => {
  vi.mocked(request).mockResolvedValue({ ...job, items: [], spec: job });
  const save = vi.fn().mockResolvedValue({ operation_id: "save" });
  render(
    <ManagedJobEditor
      context={context}
      job={job}
      saving={false}
      onSave={save}
      onClose={() => {}}
    />,
  );
  await screen.findByDisplayValue("十点任务");
  const holiday = await screen.findByRole("switch", { name: "非工作日执行" });
  expect(holiday).not.toBeChecked();
  fireEvent.click(holiday);
  fireEvent.mouseDown(screen.getAllByRole("combobox")[0]);
  fireEvent.click(await screen.findByText("每月最后一个工作日（北京时间）"));
  fireEvent.click(screen.getByRole("button", { name: /保\s*存/ }));
  await waitFor(() => expect(save).toHaveBeenCalled());
  expect(save.mock.calls[0][1].schedule).toMatchObject({
    cron: "0 10 LW * *",
    timezone: "Asia/Shanghai",
    run_on_non_workday: true,
  });
});

it.each([
  "0 9,10,11 * * *",
  "*/5 * * * *",
  "0 10 15 * *",
  "0 9 * * MON 2026-2029",
])("真实编辑框以自定义模式展示%s并保留原表达式", async (cron) => {
  const { schedule, ...status } = job;
  vi.mocked(request).mockResolvedValue({
    ...status,
    items: [],
    spec: { ...job, schedule: { ...schedule, cron } },
  });
  const save = vi.fn().mockResolvedValue({ operation_id: "save" });
  render(
    <ManagedJobEditor
      context={context}
      job={job}
      saving={false}
      onSave={save}
      onClose={() => {}}
    />,
  );
  expect(
    await screen.findByLabelText("cronJobs.cronCustomExpression"),
  ).toHaveValue(cron);
  expect(screen.queryByDisplayValue("09:00")).not.toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: /保\s*存/ }));
  await waitFor(() => expect(save).toHaveBeenCalled());
  expect(save.mock.calls[0][1].schedule.cron).toBe(cron);
});
