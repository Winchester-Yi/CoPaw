import {
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import ScheduledTaskPopup from ".";
import { cronCalendarApi } from "@/api/modules/cronCalendar";
import { userTimezoneApi } from "@/api/modules/userTimezone";

vi.mock("@/hooks/useAppMessage", () => ({
  useAppMessage: () => ({ message: { success: vi.fn(), error: vi.fn() } }),
}));
vi.mock("@/hooks/useExecutionModelOptions", () => ({
  DEFAULT_EXECUTION_MODEL_KEY: "default",
  parseExecutionModelKey: () => undefined,
  useExecutionModelOptions: () => ({
    loading: false,
    options: [],
    tenantDefaultLabel: "默认模型",
  }),
}));
vi.mock("@/api/modules/userTimezone", () => ({
  userTimezoneApi: {
    getUserTimezone: vi.fn().mockResolvedValue({ timezone: "Asia/Tokyo" }),
  },
}));
vi.mock("@/api/modules/cronCalendar", () => ({
  cronCalendarApi: { preview: vi.fn() },
}));
const context = {
  actor_id: "alice",
  tenant_id: "alice",
  source_id: "bank",
  agent_id: "default",
};
const props = {
  open: true,
  onClose: vi.fn(),
  onConfirm: vi.fn().mockResolvedValue(undefined),
};
beforeEach(() => {
  vi.clearAllMocks();
  vi.mocked(cronCalendarApi.preview).mockResolvedValue({
    source: "api",
    next_run_times: ["2026-10-30T09:00:00+08:00"],
  });
  vi.mocked(userTimezoneApi.getUserTimezone).mockResolvedValue({
    timezone: "Asia/Tokyo",
  });
});
afterEach(cleanup);

it("旧模式不增加日历控件，也不调用新预览接口", () => {
  render(<ScheduledTaskPopup {...props} />);
  expect(screen.queryByText("允许非工作日执行")).not.toBeInTheDocument();
  expect(
    screen.queryByRole("button", { name: "月末工作日" }),
  ).not.toBeInTheDocument();
  expect(cronCalendarApi.preview).not.toHaveBeenCalled();
});

it("新版保留用户时区，缺省不允许非工作日，并从服务器取得预览", async () => {
  render(<ScheduledTaskPopup {...props} managedContext={context} />);
  expect(await screen.findByText(/Asia\/Tokyo/)).toBeInTheDocument();
  expect(
    screen.getByRole("checkbox", { name: "允许非工作日执行" }),
  ).not.toBeChecked();
  await waitFor(() =>
    expect(cronCalendarApi.preview).toHaveBeenCalledWith(
      context,
      expect.objectContaining({
        cron: "00 09 * * *",
        timezone: "Asia/Tokyo",
        run_on_non_workday: false,
      }),
      expect.any(AbortSignal),
    ),
  );
  await screen.findByText(/2026/);
  fireEvent.click(screen.getByRole("checkbox", { name: "允许非工作日执行" }));
  fireEvent.click(screen.getByRole("button", { name: "确认" }));
  await waitFor(() =>
    expect(props.onConfirm).toHaveBeenCalledWith(
      "00 09 * * *",
      expect.any(Object),
      undefined,
      0,
      {
        type: "cron",
        cron: "00 09 * * *",
        timezone: "Asia/Tokyo",
        run_on_non_workday: true,
      },
    ),
  );
});

it("快捷入口隐藏假期配置，仍按用户时区提交非工作日不执行", async () => {
  const confirm = vi.fn().mockResolvedValue(undefined);
  render(
    <ScheduledTaskPopup
      {...props}
      onConfirm={confirm}
      managedContext={context}
      showCalendarOptions={false}
    />,
  );
  await screen.findByText(/Asia\/Tokyo/);
  expect(
    screen.queryByRole("checkbox", { name: "允许非工作日执行" }),
  ).not.toBeInTheDocument();
  expect(
    screen.queryByRole("button", { name: "月末工作日" }),
  ).not.toBeInTheDocument();
  expect(screen.getByRole("button", { name: "每月" })).toBeInTheDocument();
  fireEvent.change(screen.getByRole("spinbutton", { name: "通知延迟数值" }), {
    target: { value: "30" },
  });
  fireEvent.click(screen.getByRole("button", { name: "确认" }));
  await waitFor(() =>
    expect(confirm).toHaveBeenCalledWith(
      "00 09 * * *",
      expect.any(Object),
      undefined,
      30,
      {
        type: "cron",
        cron: "00 09 * * *",
        timezone: "Asia/Tokyo",
        run_on_non_workday: false,
      },
    ),
  );
});

it("只有明确选择 LW 才使用北京时间，切回普通频次恢复用户时区", async () => {
  render(<ScheduledTaskPopup {...props} managedContext={context} />);
  await screen.findByText(/Asia\/Tokyo/);
  fireEvent.click(screen.getByRole("button", { name: "月末工作日" }));
  await screen.findByText(/Asia\/Shanghai/);
  await waitFor(() =>
    expect(cronCalendarApi.preview).toHaveBeenLastCalledWith(
      context,
      expect.objectContaining({
        cron: "00 09 LW * *",
        timezone: "Asia/Shanghai",
      }),
      expect.any(AbortSignal),
    ),
  );
  fireEvent.click(screen.getByRole("button", { name: "每天" }));
  await screen.findByText(/Asia\/Tokyo/);
});

it("预览失败显示不可用，不回退浏览器本地计算的首次执行时间", async () => {
  vi.mocked(cronCalendarApi.preview).mockRejectedValue(
    new Error("日历服务未确认"),
  );
  render(<ScheduledTaskPopup {...props} managedContext={context} />);
  expect(await screen.findByText(/预览暂不可用/)).toBeInTheDocument();
  expect(
    screen.queryByText(/今天.*首次执行|明天.*首次执行/),
  ).not.toBeInTheDocument();
});

it("预览包含未来日期兜底时不显示为全量权威日历", async () => {
  vi.mocked(cronCalendarApi.preview).mockResolvedValue({
    source: "api",
    preview_source: "fallback",
    next_run_times: ["2026-10-30T09:00:00+08:00"],
  });
  render(<ScheduledTaskPopup {...props} managedContext={context} />);
  expect(await screen.findByText(/含周末兜底判定/)).toBeInTheDocument();
});

it("缺失用户时区时不能悄悄采用浏览器时区创建", async () => {
  vi.mocked(userTimezoneApi.getUserTimezone).mockResolvedValue(
    {} as { timezone: string },
  );
  render(<ScheduledTaskPopup {...props} managedContext={context} />);
  expect(await screen.findByText(/排程时区读取失败/)).toBeInTheDocument();
  expect(screen.getByRole("button", { name: "确认" })).toBeDisabled();
  expect(cronCalendarApi.preview).not.toHaveBeenCalled();
});

it("未知结果关闭再打开时保留锁定的延迟和时区，不把重试变成另一份请求", async () => {
  const confirm = vi
    .fn()
    .mockRejectedValueOnce(new Error("网络未知"))
    .mockResolvedValue(undefined);
  const view = render(
    <ScheduledTaskPopup
      {...props}
      onConfirm={confirm}
      managedContext={context}
    />,
  );
  await screen.findByText(/Asia\/Tokyo/);
  fireEvent.change(screen.getByRole("spinbutton", { name: "通知延迟数值" }), {
    target: { value: "30" },
  });
  fireEvent.click(screen.getByRole("button", { name: "确认" }));
  await waitFor(() => expect(confirm).toHaveBeenCalledTimes(1));
  vi.mocked(userTimezoneApi.getUserTimezone).mockResolvedValue({
    timezone: "Europe/London",
  });
  view.rerender(
    <ScheduledTaskPopup
      {...props}
      open={false}
      settingsLocked
      onConfirm={confirm}
      managedContext={context}
    />,
  );
  view.rerender(
    <ScheduledTaskPopup
      {...props}
      settingsLocked
      onConfirm={confirm}
      managedContext={context}
    />,
  );
  await waitFor(() =>
    expect(
      screen.getByRole("spinbutton", { name: "通知延迟数值" }),
    ).toHaveValue(30),
  );
  expect(screen.getByText(/Asia\/Tokyo/)).toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "重试" }));
  await waitFor(() => expect(confirm).toHaveBeenCalledTimes(2));
  expect(confirm.mock.calls[1]).toEqual(confirm.mock.calls[0]);
});

it("新版普通月日期不存在时提示跳过，不承诺自动顺延到月末", async () => {
  render(<ScheduledTaskPopup {...props} managedContext={context} />);
  await screen.findByText(/Asia\/Tokyo/);
  fireEvent.click(screen.getByRole("button", { name: "每月" }));
  fireEvent.click(screen.getByRole("button", { name: "31" }));
  fireEvent.mouseOver(screen.getByText(/29\/30\/31日可能不存在/));
  expect(
    await screen.findByText(
      "部分月份不存在该日期，这些月份将跳过，不会自动顺延",
    ),
  ).toBeInTheDocument();
});
