import React from "react";
import {
  act,
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import {
  ActivePlanModeButton,
  PlanModeMenuItem,
  getPlanModeEnabled,
  getScopedPlanModeEnabled,
  buildPlanModeMeta,
  persistPlanModeState,
  preparePlanModeSubmit,
  resolveActivePlanModeSession,
} from "./planMode";
import quickMenuStyles from "@/components/agentscope-chat/ComposerQuickMenu/index.module.less";

describe("Plan Mode frontend helpers", () => {
  it("omits server-owned Goal draft authority from chat patches", () => {
    expect(
      buildPlanModeMeta(
        {
          goal_draft_subagent_delegation_enabled: true,
          title: "preserved",
        },
        true,
      ),
    ).toEqual({ title: "preserved", plan_mode_enabled: true });
  });
  it("omits server-owned expert identity from mode patches", () => {
    expect(
      buildPlanModeMeta(
        {
          expert_main_binding: { revision: 2, expert_id: "expert" },
          title: "preserved",
        },
        true,
      ),
    ).toEqual({ title: "preserved", plan_mode_enabled: true });
  });
  afterEach(() => {
    cleanup();
    vi.useRealTimers();
  });

  it("initializes the toggle from ChatSpec meta", () => {
    render(
      <PlanModeMenuItem
        enabled={getPlanModeEnabled({
          meta: { plan_mode_enabled: true },
        })}
        label="计划模式"
        onChange={vi.fn()}
      />,
    );

    expect(screen.getByRole("switch", { name: "计划模式" })).toHaveAttribute(
      "aria-checked",
      "true",
    );
  });

  it("persists toggle changes through the provided handler", async () => {
    const onChange = vi.fn();

    render(
      <PlanModeMenuItem enabled={false} label="计划模式" onChange={onChange} />,
    );

    fireEvent.click(screen.getByRole("switch", { name: "计划模式" }));

    await waitFor(() => {
      expect(onChange).toHaveBeenCalledWith(true);
    });
  });

  it("can render as a compact submenu item without an icon", () => {
    render(
      <PlanModeMenuItem
        enabled={false}
        label="计划"
        showIcon={false}
        onChange={vi.fn()}
      />,
    );

    expect(screen.getByText("计划")).toBeInTheDocument();
    expect(screen.getByText("计划")).toHaveClass(quickMenuStyles.label);
    expect(screen.queryByLabelText("ordered-list")).not.toBeInTheDocument();
    expect(
      screen.getByText("计划").closest(`.${quickMenuStyles.item}`),
    ).toHaveClass(quickMenuStyles.itemClickable);
  });

  it("does not dispatch toggle changes when the menu item is disabled", async () => {
    const onChange = vi.fn();

    render(
      <PlanModeMenuItem
        enabled={false}
        disabled
        label="计划模式"
        onChange={onChange}
      />,
    );

    expect(screen.getByRole("switch", { name: "计划模式" })).toBeDisabled();
    fireEvent.click(screen.getByRole("switch", { name: "计划模式" }));

    await waitFor(() => {
      expect(onChange).not.toHaveBeenCalled();
    });
  });

  it("renders nothing when the active Plan Mode button is initially disabled", () => {
    const onDisable = vi.fn();
    render(
      <ActivePlanModeButton
        enabled={false}
        label="计划模式"
        onDisable={onDisable}
      />,
    );

    expect(
      screen.queryByRole("button", { name: "计划模式" }),
    ).not.toBeInTheDocument();
  });

  it("renders the active Plan Mode button when enabled and dispatches disable clicks", async () => {
    const onDisable = vi.fn();
    render(
      <ActivePlanModeButton enabled label="计划模式" onDisable={onDisable} />,
    );

    fireEvent.click(screen.getByRole("button", { name: "计划模式" }));

    await waitFor(() => {
      expect(onDisable).toHaveBeenCalledTimes(1);
    });
  });

  it("renders a closable active Goal Mode button without the ordered-list icon", async () => {
    const onDisable = vi.fn();
    render(
      <ActivePlanModeButton
        enabled
        label="目标模式"
        showIcon={false}
        onDisable={onDisable}
      />,
    );

    const button = screen.getByRole("button", { name: "目标模式" });
    expect(button).toBeInTheDocument();
    expect(screen.queryByLabelText("ordered-list")).not.toBeInTheDocument();

    fireEvent.click(button);
    await waitFor(() => expect(onDisable).toHaveBeenCalledTimes(1));
  });

  it("keeps the active Plan Mode button mounted while exiting, then unmounts it", () => {
    vi.useFakeTimers();
    const onDisable = vi.fn();
    const { rerender } = render(
      <ActivePlanModeButton enabled label="计划模式" onDisable={onDisable} />,
    );

    rerender(
      <ActivePlanModeButton
        enabled={false}
        label="计划模式"
        onDisable={onDisable}
      />,
    );

    const exitingButton = screen.getByRole("button", { name: "计划模式" });
    expect(exitingButton).toHaveAttribute("data-plan-mode-exiting", "true");
    expect(exitingButton).toBeDisabled();

    act(() => {
      vi.advanceTimersByTime(180);
    });

    expect(
      screen.queryByRole("button", { name: "计划模式" }),
    ).not.toBeInTheDocument();
  });

  it("clears exiting state when active Plan Mode is enabled again", () => {
    vi.useFakeTimers();
    const onDisable = vi.fn();
    const { rerender } = render(
      <ActivePlanModeButton enabled label="计划模式" onDisable={onDisable} />,
    );

    rerender(
      <ActivePlanModeButton
        enabled={false}
        label="计划模式"
        onDisable={onDisable}
      />,
    );
    rerender(
      <ActivePlanModeButton enabled label="计划模式" onDisable={onDisable} />,
    );

    const button = screen.getByRole("button", { name: "计划模式" });
    expect(button).not.toHaveAttribute("data-plan-mode-exiting", "true");
    expect(button).not.toBeDisabled();

    act(() => {
      vi.advanceTimersByTime(180);
    });

    expect(
      screen.getByRole("button", { name: "计划模式" }),
    ).toBeInTheDocument();
  });

  it("renders the active Plan Mode button after starting disabled", () => {
    const onDisable = vi.fn();
    const { rerender } = render(
      <ActivePlanModeButton
        enabled={false}
        label="计划模式"
        onDisable={onDisable}
      />,
    );

    rerender(
      <ActivePlanModeButton enabled label="计划模式" onDisable={onDisable} />,
    );

    expect(
      screen.getByRole("button", { name: "计划模式" }),
    ).toBeInTheDocument();
  });

  it("turns /plan with text into a planning request", async () => {
    const persistPlanMode = vi.fn(async () => {});
    const setPlanModeEnabled = vi.fn();

    const result = await preparePlanModeSubmit(
      {
        query: "/plan investigate this bug",
        fileList: [],
      },
      {
        planModeEnabled: false,
        persistPlanMode,
        setPlanModeEnabled,
      },
    );

    expect(persistPlanMode).toHaveBeenCalledWith(true);
    expect(setPlanModeEnabled).not.toHaveBeenCalled();
    expect(result).toMatchObject({
      query: "investigate this bug",
      biz_params: { mode: "plan" },
    });
  });

  it("turns /plan alone into a persisted state change without a request", async () => {
    const persistPlanMode = vi.fn(async () => {});
    const setPlanModeEnabled = vi.fn();

    const result = await preparePlanModeSubmit(
      {
        query: "/plan",
        fileList: [],
      },
      {
        planModeEnabled: false,
        persistPlanMode,
        setPlanModeEnabled,
      },
    );

    expect(persistPlanMode).toHaveBeenCalledWith(true);
    expect(setPlanModeEnabled).not.toHaveBeenCalled();
    expect(result).toEqual({
      shouldSubmit: false,
      clearInput: true,
    });
  });

  it("adds explicit normal mode metadata when Plan Mode is disabled", async () => {
    const result = await preparePlanModeSubmit(
      {
        query: "hello",
        fileList: [],
        biz_params: { user_prompt_params: { source: "test" } },
      },
      {
        planModeEnabled: false,
        persistPlanMode: vi.fn(async () => {}),
      },
    );

    expect(result).toMatchObject({
      query: "hello",
      biz_params: {
        mode: "normal",
        user_prompt_params: { source: "test" },
      },
    });
  });

  it("ignores local Plan Mode state from a different chat scope", () => {
    expect(
      getScopedPlanModeEnabled({
        metadataEnabled: false,
        localState: { scopeKey: "chat-a", enabled: true },
        scopeKey: "chat-b",
      }),
    ).toBe(false);
  });

  it("uses local Plan Mode state for aliased chat scopes", () => {
    expect(
      getScopedPlanModeEnabled({
        metadataEnabled: false,
        localState: {
          scopeKey: "",
          aliases: ["1780458341751000", "chat-real-created"],
          enabled: true,
        },
        scopeKey: "1780458341751000",
      }),
    ).toBe(true);
  });

  it("preserves explicit card-submitted request mode", async () => {
    const result = await preparePlanModeSubmit(
      {
        query: "Execute plan",
        fileList: [],
        biz_params: {
          mode: "normal",
          plan_interaction_response: {
            decision: "execute",
          },
        },
      },
      {
        planModeEnabled: true,
        persistPlanMode: vi.fn(async () => {}),
      },
    );

    expect(result).toMatchObject({
      biz_params: {
        mode: "normal",
        plan_interaction_response: {
          decision: "execute",
        },
      },
    });
  });

  it("resolves the active Plan Mode session across id aliases without leaking between chats", () => {
    const sessions = [
      {
        id: "chat-enabled",
        realId: "real-enabled",
        sessionId: "logical-enabled",
        meta: { plan_mode_enabled: true },
      },
      {
        id: "chat-disabled",
        session_id: "logical-disabled",
        meta: { plan_mode_enabled: false },
      },
    ];

    expect(
      resolveActivePlanModeSession(sessions, ["real-enabled"])?.meta
        ?.plan_mode_enabled,
    ).toBe(true);
    expect(
      resolveActivePlanModeSession(sessions, ["logical-enabled"])?.meta
        ?.plan_mode_enabled,
    ).toBe(true);
    expect(
      resolveActivePlanModeSession(sessions, ["logical-disabled"])?.meta
        ?.plan_mode_enabled,
    ).toBe(false);
  });

  it("prefers earlier current-session candidates over stale session ids", () => {
    const sessions = [
      {
        id: "chat-old",
        sessionId: "logical-old",
        meta: { plan_mode_enabled: true },
      },
      {
        id: "chat-current",
        sessionId: "logical-current",
        meta: { plan_mode_enabled: false },
      },
    ];

    expect(
      resolveActivePlanModeSession(sessions, ["chat-current", "chat-old"])?.id,
    ).toBe("chat-current");
  });

  it("persists Plan Mode changes through chat metadata updates", async () => {
    const session = {
      id: "chat-1",
      meta: {
        plan_mode_enabled: true,
        title: "demo",
        session_title_edited: true,
      },
    };
    const ensureChatId = vi.fn(async () => "chat-1");
    const updateChat = vi.fn(async () => ({
      meta: { plan_mode_enabled: false, title: "demo" },
    }));
    const updateSession = vi.fn(async () => undefined);
    const setPlanModeEnabled = vi.fn();

    await persistPlanModeState({
      enabled: false,
      session,
      ensureChatId,
      updateChat,
      updateSession,
      setPlanModeEnabled,
    });

    expect(setPlanModeEnabled).toHaveBeenNthCalledWith(1, false);
    expect(ensureChatId).toHaveBeenCalledWith(session, {
      plan_mode_enabled: false,
      title: "demo",
    });
    expect(updateChat).toHaveBeenCalledWith("chat-1", {
      meta: { plan_mode_enabled: false },
    });
    expect(updateSession).toHaveBeenCalledWith({
      id: "chat-1",
      meta: {
        plan_mode_enabled: false,
        title: "demo",
      },
    });
  });

  it("rolls back local Plan Mode state when persistence fails", async () => {
    const session = {
      id: "chat-1",
      meta: { plan_mode_enabled: true },
    };
    const setPlanModeEnabled = vi.fn();
    const onPersistError = vi.fn();

    await expect(
      persistPlanModeState({
        enabled: false,
        session,
        ensureChatId: vi.fn(async () => "chat-1"),
        updateChat: vi.fn(async () => {
          throw new Error("persist failed");
        }),
        updateSession: vi.fn(async () => undefined),
        setPlanModeEnabled,
        onPersistError,
      }),
    ).rejects.toThrow("persist failed");

    expect(setPlanModeEnabled).toHaveBeenNthCalledWith(1, false);
    expect(setPlanModeEnabled).toHaveBeenNthCalledWith(2, true);
    expect(onPersistError).toHaveBeenCalledTimes(1);
  });
});
