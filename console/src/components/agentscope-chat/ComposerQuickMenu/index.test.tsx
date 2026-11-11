import React from "react";
import { readFileSync } from "node:fs";
import path from "node:path";
import {
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import ComposerQuickMenu, {
  ComposerQuickMenuItem,
  ComposerQuickMenuSubmenu,
} from "./index";
import styles from "./index.module.less";

const stylesheet = readFileSync(
  path.join(
    process.cwd(),
    "src/components/agentscope-chat/ComposerQuickMenu/index.module.less",
  ),
  "utf8",
);

describe("ComposerQuickMenu", () => {
  afterEach(() => {
    cleanup();
    vi.restoreAllMocks();
  });

  it("opens and closes the menu from the plus trigger", async () => {
    render(
      <ComposerQuickMenu triggerLabel="快捷操作">
        <ComposerQuickMenuItem label="上传文件" />
      </ComposerQuickMenu>,
    );

    expect(screen.queryByText("上传文件")).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "快捷操作" }));

    expect(await screen.findByText("上传文件")).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "快捷操作" }));

    await waitFor(() => {
      expect(screen.queryByText("上传文件")).not.toBeInTheDocument();
    });
  });

  it("runs the item handler and closes the menu after click", async () => {
    const onClick = vi.fn();

    render(
      <ComposerQuickMenu triggerLabel="快捷操作">
        <ComposerQuickMenuItem label="计划模式" onClick={onClick} />
      </ComposerQuickMenu>,
    );

    fireEvent.click(screen.getByRole("button", { name: "快捷操作" }));
    fireEvent.click(await screen.findByText("计划模式"));

    expect(onClick).toHaveBeenCalledTimes(1);

    await waitFor(() => {
      expect(screen.queryByText("计划模式")).not.toBeInTheDocument();
    });
  });

  it("closes an already open menu when the trigger becomes disabled", async () => {
    const { rerender } = render(
      <ComposerQuickMenu triggerLabel="快捷操作">
        <ComposerQuickMenuItem label="计划模式" />
      </ComposerQuickMenu>,
    );

    fireEvent.click(screen.getByRole("button", { name: "快捷操作" }));
    expect(await screen.findByText("计划模式")).toBeInTheDocument();

    rerender(
      <ComposerQuickMenu triggerLabel="快捷操作" disabled>
        <ComposerQuickMenuItem label="计划模式" />
      </ComposerQuickMenu>,
    );

    await waitFor(() => {
      expect(screen.queryByText("计划模式")).not.toBeInTheDocument();
    });
  });

  it("renders the menu in a portal so overlays do not clip it", async () => {
    const { container } = render(
      <div style={{ overflow: "hidden" }}>
        <ComposerQuickMenu triggerLabel="快捷操作">
          <ComposerQuickMenuItem label="上传文件" />
        </ComposerQuickMenu>
      </div>,
    );

    fireEvent.click(screen.getByRole("button", { name: "快捷操作" }));

    const menuItem = await screen.findByText("上传文件");
    expect(container).not.toContainElement(menuItem);
    expect(document.body).toContainElement(menuItem);
  });

  it("opens submenu items without closing the root menu", async () => {
    render(
      <ComposerQuickMenu triggerLabel="快捷操作">
        <ComposerQuickMenuSubmenu label="模式">
          <ComposerQuickMenuItem label="计划模式" />
        </ComposerQuickMenuSubmenu>
      </ComposerQuickMenu>,
    );

    fireEvent.click(screen.getByRole("button", { name: "快捷操作" }));
    fireEvent.click(await screen.findByRole("button", { name: "模式" }));

    expect(await screen.findByText("计划模式")).toBeInTheDocument();
    expect(screen.getByText("模式")).toBeInTheDocument();
  });

  it("opens a wide submenu to the left near the viewport edge", async () => {
    vi.spyOn(HTMLElement.prototype, "getBoundingClientRect").mockImplementation(
      function (this: HTMLElement) {
        if (this.classList.contains(styles.submenu)) {
          return {
            bottom: 40,
            height: 40,
            left: 900,
            right: 1000,
            top: 0,
            width: 100,
          } as DOMRect;
        }
        return {
          bottom: 240,
          height: 240,
          left: 0,
          right: 240,
          top: 0,
          width: 240,
        } as DOMRect;
      },
    );

    render(
      <ComposerQuickMenu triggerLabel="快捷操作">
        <ComposerQuickMenuSubmenu label="专家" panelWidth="240px">
          <ComposerQuickMenuItem label="专家一号" />
        </ComposerQuickMenuSubmenu>
      </ComposerQuickMenu>,
    );

    fireEvent.click(screen.getByRole("button", { name: "快捷操作" }));
    fireEvent.click(await screen.findByRole("button", { name: "专家" }));

    await waitFor(() => {
      expect(screen.getByRole("menu")).toHaveAttribute(
        "data-opens-left",
        "true",
      );
    });
  });

  it("moves a tall submenu into the viewport near the bottom", async () => {
    vi.spyOn(HTMLElement.prototype, "getBoundingClientRect").mockImplementation(
      function (this: HTMLElement) {
        if (this.classList.contains(styles.submenu)) {
          return {
            top: 700,
            bottom: 740,
            left: 100,
            right: 200,
            width: 100,
            height: 40,
          } as DOMRect;
        }
        return {
          top: 700,
          bottom: 1000,
          left: 208,
          right: 448,
          width: 240,
          height: 300,
        } as DOMRect;
      },
    );
    render(
      <ComposerQuickMenu triggerLabel="快捷操作">
        <ComposerQuickMenuSubmenu label="专家">
          <ComposerQuickMenuItem label="专家一号" />
        </ComposerQuickMenuSubmenu>
      </ComposerQuickMenu>,
    );
    fireEvent.click(screen.getByRole("button", { name: "快捷操作" }));
    fireEvent.click(await screen.findByRole("button", { name: "专家" }));
    expect(screen.getByRole("menu")).toHaveStyle({ top: "-248px" });
  });

  it.each(["resize", "scroll"])(
    "recalculates submenu placement after its parent moves on %s",
    async (eventName) => {
      let triggerTop = 600;
      vi.spyOn(
        HTMLElement.prototype,
        "getBoundingClientRect",
      ).mockImplementation(function (this: HTMLElement) {
        if (this.classList.contains(styles.trigger)) {
          return {
            top: triggerTop,
            bottom: triggerTop + 32,
            left: 100,
            right: 132,
            width: 32,
            height: 32,
          } as DOMRect;
        }
        if (this.classList.contains(styles.submenu)) {
          const parent = this.closest(`.${styles.panel}`) as HTMLElement;
          const top = Number.parseFloat(parent.style.top) + 32;
          return {
            top,
            bottom: top + 40,
            left: 100,
            right: 200,
            width: 100,
            height: 40,
          } as DOMRect;
        }
        if (this.classList.contains(styles.submenuPanel)) {
          return {
            top: 0,
            bottom: 300,
            left: 208,
            right: 448,
            width: 240,
            height: 300,
          } as DOMRect;
        }
        return {
          top: 0,
          bottom: 100,
          left: 0,
          right: 152,
          width: 152,
          height: 100,
        } as DOMRect;
      });
      const originalHeight = window.innerHeight;
      Object.defineProperty(window, "innerHeight", {
        configurable: true,
        value: 768,
      });
      try {
        render(
          <ComposerQuickMenu triggerLabel="快捷操作">
            <ComposerQuickMenuSubmenu label="专家">
              <ComposerQuickMenuItem label="专家一号" />
            </ComposerQuickMenuSubmenu>
          </ComposerQuickMenu>,
        );
        fireEvent.click(screen.getByRole("button", { name: "快捷操作" }));
        fireEvent.click(screen.getByRole("button", { name: "专家" }));
        triggerTop = 300;
        Object.defineProperty(window, "innerHeight", {
          configurable: true,
          value: 500,
        });
        fireEvent(window, new Event(eventName));
        await waitFor(() =>
          expect(screen.getByRole("menu")).toHaveStyle({ top: "-36px" }),
        );
      } finally {
        Object.defineProperty(window, "innerHeight", {
          configurable: true,
          value: originalHeight,
        });
      }
    },
  );

  it("allows externally handled items to render with hover affordance", () => {
    render(<ComposerQuickMenuItem interactive label="上传文件" />);

    expect(screen.getByText("上传文件").closest(`.${styles.item}`)).toHaveClass(
      styles.itemClickable,
    );
    expect(
      screen.queryByRole("button", { name: "上传文件" }),
    ).not.toBeInTheDocument();
  });

  it("keeps Ant Upload trigger layers full width for row-sized hover", () => {
    expect(stylesheet).toContain(":global(.ant-upload-select)");
    expect(stylesheet).toContain(":global(.ant-upload-select > span)");
  });

  it("keeps keyboard focus visible on submenu triggers", () => {
    expect(stylesheet).toContain("> .item:focus-visible");
  });

  it("moves the submenu hover bridge to the matching side", () => {
    expect(stylesheet).toContain('[data-opens-left="true"]');
  });
});
