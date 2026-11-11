import {
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import dayjs from "dayjs";
import { TrackingDatePicker } from "./TrackingDatePicker";
import { trackingNow } from "./trackingDate";
vi.stubGlobal(
  "ResizeObserver",
  class {
    observe() {}
    disconnect() {}
    unobserve() {}
  },
);
afterEach(cleanup);
it("当前日期和时间按本地格式生成，不转UTC", () => {
  expect(trackingNow()).toBe(dayjs().format("YYYY-MM-DD"));
  expect(trackingNow(true)).toBe(dayjs().format("YYYY-MM-DDTHH:mm"));
});
it("使用非原生日期输入和明确中文placeholder，空值不显示浏览器格式占位", () => {
  const { rerender } = render(<TrackingDatePicker aria-label="接触日期" />);
  expect(screen.getByLabelText("接触日期")).toHaveProperty("type", "text");
  expect(screen.getByLabelText("接触日期")).toHaveAttribute("readonly");
  expect(screen.getByPlaceholderText("请选择接触日期")).toHaveValue("");
  rerender(
    <TrackingDatePicker
      withTime
      aria-label="触达时间"
      value="2026-11-05T14:30"
    />,
  );
  expect(screen.getByLabelText("触达时间")).toHaveValue("2026-11-05 14:30");
});
it("日历挂在当前dialog内，选中日期回传原有字符串格式", async () => {
  const onChange = vi.fn();
  const { container } = render(
    <dialog open>
      <TrackingDatePicker
        aria-label="接触日期"
        value="2026-11-05"
        onChange={onChange}
      />
    </dialog>,
  );
  fireEvent.click(screen.getByLabelText("接触日期"));
  await waitFor(() =>
    expect(
      container.querySelector("dialog .ant-picker-dropdown"),
    ).not.toBeNull(),
  );
  const date = container.querySelector('[title="2026-11-06"]')!;
  fireEvent.click(date);
  expect(onChange).toHaveBeenCalledWith("2026-11-06");
});

it("日历打开时Esc只关闭日历，并阻止原生dialog的默认取消行为", async () => {
  const { container } = render(
    <dialog open>
      <TrackingDatePicker aria-label="接触日期" value="2026-11-05" />
    </dialog>,
  );
  const input = screen.getByLabelText("接触日期");
  fireEvent.click(input);
  await waitFor(() =>
    expect(container.querySelector(".ant-picker-dropdown")).not.toBeNull(),
  );
  const event = new KeyboardEvent("keydown", {
    key: "Escape",
    bubbles: true,
    cancelable: true,
  });
  fireEvent(input, event);
  expect(event.defaultPrevented).toBe(true);
  expect(container.querySelector("dialog")).toHaveAttribute("open");
});
