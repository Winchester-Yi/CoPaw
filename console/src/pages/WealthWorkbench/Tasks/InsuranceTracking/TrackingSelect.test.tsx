import {
  cleanup,
  fireEvent,
  render,
  screen,
  within,
} from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import { TrackingSelect } from "./TrackingSelect";

afterEach(cleanup);
it("在弹窗中展开组件库下拉并保留数值类型", async () => {
  const onChange = vi.fn();
  render(
    <dialog open>
      <TrackingSelect
        aria-label="触达方式"
        value={0}
        onChange={onChange}
        options={[
          { value: 0, label: "企微" },
          { value: 1, label: "招乎" },
        ]}
      />
    </dialog>,
  );
  fireEvent.mouseDown(screen.getByRole("combobox"));
  const dialog = screen.getByRole("dialog");
  fireEvent.click(await within(dialog).findByText("招乎"));
  expect(onChange.mock.calls[0][0]).toBe(1);
});
it("经营场景保留字符串 ID", async () => {
  const onChange = vi.fn();
  render(
    <TrackingSelect<string>
      aria-label="经营场景"
      onChange={onChange}
      options={[{ value: "skill-a", label: "保险场景" }]}
    />,
  );
  fireEvent.mouseDown(screen.getByRole("combobox"));
  fireEvent.click(await screen.findByText("保险场景"));
  expect(onChange.mock.calls[0][0]).toBe("skill-a");
});
