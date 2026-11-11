import React from "react";
import {
  act,
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import SystemConfigPage from "./index";
import { useIframeStore } from "@/stores/iframeStore";
import { useSourceSystemConfigStore } from "@/stores/sourceSystemConfigStore";

const mocks = vi.hoisted(() => ({
  sourceSystemConfigApi: {
    getCurrent: vi.fn(),
    updateCurrent: vi.fn(),
    deleteCurrent: vi.fn(),
  },
  messageApi: {
    success: vi.fn(),
    error: vi.fn(),
  },
  syncStatus: vi.fn(),
}));

vi.mock("@/api/modules/sourceSystemConfig", () => ({
  sourceSystemConfigApi: mocks.sourceSystemConfigApi,
}));

vi.mock("@/hooks/useAppMessage", () => ({
  useAppMessage: () => ({
    message: mocks.messageApi,
  }),
}));

vi.mock("@/components/SystemCronSyncStatus", () => ({
  SystemCronSyncStatus: (props: unknown) => {
    mocks.syncStatus(props);
    return <div data-testid="system-sync-status" />;
  },
}));

describe("SystemConfigPage", () => {
  const loadEffectiveConfig = vi.fn().mockResolvedValue(undefined);

  it("已保存的来源配置与失败同步回执分开，回执不写入配置正文", async () => {
    const submission = {
      submission_status: "failed",
      items: [
        {
          task_type: "task_session_cleanup",
          submission_status: "failed",
          request_id: "cleanup-save",
        },
      ],
    };
    mocks.sourceSystemConfigApi.updateCurrent.mockResolvedValue({
      source_id: "portal",
      config: {},
      version: 2,
      is_default: false,
      scheduler_sync: submission,
    });
    render(<SystemConfigPage />);
    await screen.findByText("定时任务设置");
    fireEvent.click(screen.getByRole("button", { name: "common.save" }));
    await waitFor(() =>
      expect(mocks.syncStatus).toHaveBeenCalledWith(
        expect.objectContaining({ submission, scope: "source" }),
      ),
    );
    expect(mocks.sourceSystemConfigApi.updateCurrent).toHaveBeenCalledWith({
      config: {},
    });
  });

  function createDeferred<T>() {
    let resolve!: (value: T) => void;
    let reject!: (reason?: unknown) => void;
    const promise = new Promise<T>((nextResolve, nextReject) => {
      resolve = nextResolve;
      reject = nextReject;
    });
    return { promise, resolve, reject };
  }

  function getSwitchByTitle(title: string) {
    const switchTitle = screen.getByText(title);
    const switchRow = switchTitle.closest<HTMLElement>(
      "div[class*='switchRow']",
    );
    if (!switchRow) {
      throw new Error(`Switch row not found: ${title}`);
    }
    return within(switchRow).getByRole("switch");
  }

  function getTaskProgressSwitch() {
    return getSwitchByTitle("任务进度步骤条");
  }

  function getZhaohuToolGuardNotificationSwitch() {
    return getSwitchByTitle("Tool Guard 审批招乎通知");
  }

  function getCronUnreadAutoPauseSwitch() {
    return getSwitchByTitle("定时任务未读自动暂停");
  }

  function getCronSkipWeekendZhaohuSwitch() {
    return getSwitchByTitle("非工作日不发招呼完成通知");
  }

  function getArchiveMaintenanceSwitch() {
    return getSwitchByTitle("文件归档维护");
  }

  function getToolResultCompactSwitch() {
    return getSwitchByTitle("启用工具结果压缩");
  }

  function seedEffectiveConfig(config: Record<string, unknown> = {}) {
    useSourceSystemConfigStore.setState({
      config: {
        source_id: "portal",
        config,
        version: 1,
        is_default: false,
        stale: false,
        updated_by: "alice",
        updated_at: "2026-05-21 10:00:00",
      },
      sourceId: "portal",
    });
  }

  afterEach(() => {
    cleanup();
  });

  beforeEach(() => {
    loadEffectiveConfig.mockReset();
    loadEffectiveConfig.mockResolvedValue(undefined);
    mocks.sourceSystemConfigApi.getCurrent.mockReset();
    mocks.sourceSystemConfigApi.updateCurrent.mockReset();
    mocks.sourceSystemConfigApi.deleteCurrent.mockReset();
    mocks.messageApi.success.mockReset();
    mocks.messageApi.error.mockReset();
    useIframeStore.getState().clearContext();
    useIframeStore.getState().setContext({
      source: "portal",
      manager: true,
    });
    useSourceSystemConfigStore.setState({
      config: null,
      sourceId: null,
      loading: false,
      error: null,
      requestSeq: 0,
      loadEffectiveConfig,
    });
    mocks.sourceSystemConfigApi.getCurrent.mockResolvedValue({
      source_id: "portal",
      config: {},
      version: 0,
      is_default: true,
      updated_by: null,
      updated_at: null,
    });
  });

  it("renders 403 state for non-manager access", async () => {
    useIframeStore.getState().setContext({
      manager: false,
      isSuperManager: false,
    });

    render(<SystemConfigPage />);

    expect(await screen.findByText("403")).toBeTruthy();
    expect(screen.getByText("仅管理员可访问当前系统配置页面。")).toBeTruthy();
    expect(mocks.sourceSystemConfigApi.getCurrent).not.toHaveBeenCalled();
  });

  it("uses 系统 wording across the system config page", async () => {
    render(<SystemConfigPage />);

    expect(await screen.findByText("当前系统")).toBeTruthy();
    expect(screen.getByText("系统特性配置")).toBeTruthy();
    expect(screen.queryByText("当前 Source")).toBeNull();
    expect(screen.queryByText(/Source/)).toBeNull();
  });

  it("saves expert discovery and reloads it only for its source", async () => {
    const enabledRecord = {
      source_id: "portal",
      config: {
        feature_switches: { subagent_expert_discovery_enabled: true },
      },
      version: 1,
      is_default: false,
      updated_by: "alice",
      updated_at: null,
    };
    mocks.sourceSystemConfigApi.updateCurrent.mockResolvedValue(enabledRecord);
    const first = render(<SystemConfigPage />);
    await waitFor(() => {
      expect(getSwitchByTitle("允许自主选择我的专家")).toBeEnabled();
    });
    expect(getSwitchByTitle("允许自主选择我的专家")).toHaveAttribute(
      "aria-checked",
      "false",
    );
    fireEvent.click(getSwitchByTitle("允许自主选择我的专家"));
    fireEvent.click(screen.getByRole("button", { name: "common.save" }));
    await waitFor(() => {
      expect(mocks.sourceSystemConfigApi.updateCurrent).toHaveBeenCalledWith({
        config: enabledRecord.config,
      });
    });
    first.unmount();
    mocks.sourceSystemConfigApi.getCurrent.mockResolvedValue(enabledRecord);
    render(<SystemConfigPage />);
    await waitFor(() => {
      expect(getSwitchByTitle("允许自主选择我的专家")).toHaveAttribute(
        "aria-checked",
        "true",
      );
    });
    mocks.sourceSystemConfigApi.getCurrent.mockResolvedValue({
      ...enabledRecord,
      source_id: "workspace",
      config: {},
      version: 0,
    });
    act(() => {
      useIframeStore.getState().setContext({ source: "workspace" });
    });
    await waitFor(() => {
      expect(getSwitchByTitle("允许自主选择我的专家")).toBeEnabled();
      expect(getSwitchByTitle("允许自主选择我的专家")).toHaveAttribute(
        "aria-checked",
        "false",
      );
    });
  });

  it("loads current-source config and saves switch changes", async () => {
    mocks.sourceSystemConfigApi.updateCurrent.mockResolvedValue({
      source_id: "portal",
      config: {
        feature_switches: {
          chat_task_progress_enabled: false,
        },
      },
      version: 1,
      is_default: false,
      updated_by: "alice",
      updated_at: "2026-05-20 22:00:00",
    });

    render(<SystemConfigPage />);

    await waitFor(() => {
      expect(screen.queryAllByText("继承默认值").length).toBeGreaterThan(0);
    });

    fireEvent.click(getTaskProgressSwitch());
    fireEvent.click(screen.getByRole("button", { name: "common.save" }));

    await waitFor(() => {
      expect(mocks.sourceSystemConfigApi.updateCurrent).toHaveBeenCalledWith({
        config: {
          feature_switches: {
            chat_task_progress_enabled: false,
          },
        },
      });
    });
    expect(loadEffectiveConfig).toHaveBeenCalledWith("portal");
    expect(mocks.messageApi.success).toHaveBeenCalled();
  });

  it("keeps a dirty draft until the manager confirms a source switch", async () => {
    render(<SystemConfigPage />);

    await screen.findByText("当前系统");
    fireEvent.click(getTaskProgressSwitch());

    act(() => {
      useIframeStore.getState().setContext({ source: "workspace" });
    });

    expect(await screen.findByText("切换系统前保存修改？")).toBeTruthy();
    expect(useIframeStore.getState().source).toBe("portal");

    fireEvent.click(screen.getByRole("button", { name: "继续编辑" }));
    expect(screen.getByText("存在未保存修改")).toBeTruthy();
    expect(useIframeStore.getState().source).toBe("portal");
  });

  it("edits a capability from its detail drawer", async () => {
    render(<SystemConfigPage />);

    fireEvent.click(await screen.findByRole("button", { name: /对话与执行/ }));

    const drawer = await screen.findByRole("dialog");
    fireEvent.click(
      within(drawer).getByRole("button", { name: "新增提示词片段" }),
    );
    const promptSegment = within(drawer).getByLabelText("提示词片段 1");
    fireEvent.change(promptSegment, { target: { value: "保持简洁" } });
    await waitFor(() => {
      expect(screen.getByText("存在未保存修改")).toBeTruthy();
    });
  });

  it("shows grouped context and page-level save guidance in the editor drawer", async () => {
    render(<SystemConfigPage />);

    fireEvent.click(await screen.findByRole("button", { name: /安全与审批/ }));

    const drawer = await screen.findByRole("dialog");
    expect(within(drawer).getByText("访问防护")).toBeTruthy();
    expect(
      within(drawer).getByText("所有修改仍会通过页面底部统一保存。"),
    ).toBeTruthy();
    expect(within(drawer).getByText("高影响")).toBeTruthy();
  });

  it("saves zhaohu Tool Guard approval notification switch changes", async () => {
    mocks.sourceSystemConfigApi.updateCurrent.mockResolvedValue({
      source_id: "portal",
      config: {
        approval_notifications: {
          zhaohu_tool_guard_enabled: true,
        },
      },
      version: 1,
      is_default: false,
      updated_by: "alice",
      updated_at: "2026-05-20 22:00:00",
    });

    render(<SystemConfigPage />);

    expect(await screen.findByText("Tool Guard 审批招乎通知")).toBeTruthy();

    fireEvent.click(getZhaohuToolGuardNotificationSwitch());
    fireEvent.click(screen.getByRole("button", { name: "common.save" }));

    await waitFor(() => {
      expect(mocks.sourceSystemConfigApi.updateCurrent).toHaveBeenCalledWith({
        config: {
          approval_notifications: {
            zhaohu_tool_guard_enabled: true,
          },
        },
      });
    });
    expect(loadEffectiveConfig).toHaveBeenCalledWith("portal");
  });

  it("saves cron unread auto pause settings", async () => {
    mocks.sourceSystemConfigApi.updateCurrent.mockResolvedValue({
      source_id: "portal",
      config: {
        cron_unread_auto_pause: {
          enabled: false,
          threshold: 12,
        },
      },
      version: 1,
      is_default: false,
      updated_by: "alice",
      updated_at: "2026-05-20 22:00:00",
    });

    render(<SystemConfigPage />);

    expect(await screen.findByText("定时任务未读自动暂停")).toBeTruthy();

    fireEvent.change(screen.getByDisplayValue("10"), {
      target: { value: "12" },
    });
    fireEvent.click(getCronUnreadAutoPauseSwitch());
    fireEvent.click(screen.getByRole("button", { name: "common.save" }));

    await waitFor(() => {
      expect(mocks.sourceSystemConfigApi.updateCurrent).toHaveBeenCalledWith({
        config: {
          cron_unread_auto_pause: {
            enabled: false,
            threshold: 12,
          },
        },
      });
    });
  });

  it("saves cron weekend zhaohu suppression setting", async () => {
    mocks.sourceSystemConfigApi.updateCurrent.mockResolvedValue({
      source_id: "portal",
      config: {
        cron_notifications: {
          skip_weekend_zhaohu_enabled: true,
        },
      },
      version: 1,
      is_default: false,
      updated_by: "alice",
      updated_at: "2026-05-20 22:00:00",
    });

    render(<SystemConfigPage />);

    const scheduledTaskCardTitle = await screen.findByText("定时任务设置");
    const scheduledTaskCard = scheduledTaskCardTitle.closest(".ant-card");
    const switchTitle = await screen.findByText("非工作日不发招呼完成通知");
    expect(switchTitle.closest(".ant-card")).toBe(scheduledTaskCard);

    fireEvent.click(getCronSkipWeekendZhaohuSwitch());
    fireEvent.click(screen.getByRole("button", { name: "common.save" }));

    await waitFor(() => {
      expect(mocks.sourceSystemConfigApi.updateCurrent).toHaveBeenCalledWith({
        config: {
          cron_notifications: {
            skip_weekend_zhaohu_enabled: true,
          },
        },
      });
    });
    expect(loadEffectiveConfig).toHaveBeenCalledWith("portal");
  });

  it("saves cron task session cleanup settings", async () => {
    mocks.sourceSystemConfigApi.getCurrent.mockResolvedValueOnce({
      source_id: "portal",
      config: {
        provider_policy: { default_model: "qwen-max" },
        cron_task_session_cleanup: {
          enabled: true,
          retention_days: 30,
          cron: "0 1 * * *",
          unknown_retained: "yes",
        },
      },
      version: 1,
      is_default: false,
      updated_by: "alice",
      updated_at: "2026-05-20 22:00:00",
    });
    mocks.sourceSystemConfigApi.updateCurrent.mockResolvedValue({
      source_id: "portal",
      config: {
        provider_policy: { default_model: "qwen-max" },
        cron_task_session_cleanup: {
          enabled: true,
          retention_days: 45,
          cron: "30 2 * * *",
          unknown_retained: "yes",
        },
      },
      version: 2,
      is_default: false,
      updated_by: "alice",
      updated_at: "2026-05-21 10:00:00",
    });

    render(<SystemConfigPage />);

    const scheduledTaskCardTitle = await screen.findByText("定时任务设置");
    const scheduledTaskCard = scheduledTaskCardTitle.closest(".ant-card");
    expect(screen.getByText("定时任务未读自动暂停").closest(".ant-card")).toBe(
      scheduledTaskCard,
    );
    expect(screen.getByText("定时任务会话历史清理").closest(".ant-card")).toBe(
      scheduledTaskCard,
    );
    expect(screen.getByText("01:00")).toBeTruthy();

    fireEvent.change(screen.getByDisplayValue("30"), {
      target: { value: "45" },
    });
    fireEvent.mouseDown(screen.getByRole("combobox", { name: "每日运行时间" }));
    fireEvent.click(await screen.findByText("02:30"));
    fireEvent.click(screen.getByRole("button", { name: "common.save" }));

    await waitFor(() => {
      expect(mocks.sourceSystemConfigApi.updateCurrent).toHaveBeenCalledWith({
        config: {
          provider_policy: { default_model: "qwen-max" },
          cron_task_session_cleanup: {
            enabled: true,
            retention_days: 45,
            cron: "30 2 * * *",
            unknown_retained: "yes",
          },
        },
      });
    });
  });

  it("saves archive maintenance settings", async () => {
    mocks.sourceSystemConfigApi.getCurrent.mockResolvedValueOnce({
      source_id: "portal",
      config: {
        provider_policy: { default_model: "qwen-max" },
        archive_maintenance: {
          enabled: true,
          cron: "0 3 * * *",
          unknown_retained: "yes",
        },
      },
      version: 1,
      is_default: false,
      updated_by: "alice",
      updated_at: "2026-05-20 22:00:00",
    });
    mocks.sourceSystemConfigApi.updateCurrent.mockResolvedValue({
      source_id: "portal",
      config: {
        provider_policy: { default_model: "qwen-max" },
        archive_maintenance: {
          enabled: false,
          cron: "30 3 * * *",
          unknown_retained: "yes",
        },
      },
      version: 2,
      is_default: false,
      updated_by: "alice",
      updated_at: "2026-05-21 10:00:00",
    });

    render(<SystemConfigPage />);

    expect(await screen.findByText("文件归档维护")).toBeTruthy();
    expect(screen.getByText("03:00")).toBeTruthy();

    fireEvent.mouseDown(
      screen.getByRole("combobox", { name: "归档维护每日运行时间" }),
    );
    fireEvent.click(await screen.findByText("03:30"));
    fireEvent.click(getArchiveMaintenanceSwitch());
    fireEvent.click(screen.getByRole("button", { name: "common.save" }));

    await waitFor(() => {
      expect(mocks.sourceSystemConfigApi.updateCurrent).toHaveBeenCalledWith({
        config: {
          provider_policy: { default_model: "qwen-max" },
          archive_maintenance: {
            enabled: false,
            cron: "30 3 * * *",
            unknown_retained: "yes",
          },
        },
      });
    });
  });

  it("saves tool result compact values while preserving unknown raw keys", async () => {
    mocks.sourceSystemConfigApi.getCurrent.mockResolvedValueOnce({
      source_id: "portal",
      config: {
        provider_policy: { default_model: "qwen-max" },
        tool_result_compact: {
          recent_max_bytes: 12000,
          unknown_retained: "yes",
        },
      },
      version: 1,
      is_default: false,
      updated_by: "alice",
      updated_at: "2026-05-20 22:00:00",
    });
    mocks.sourceSystemConfigApi.updateCurrent.mockResolvedValue({
      source_id: "portal",
      config: {
        provider_policy: { default_model: "qwen-max" },
        tool_result_compact: {
          enabled: false,
          recent_max_bytes: 16000,
          unknown_retained: "yes",
        },
      },
      version: 2,
      is_default: false,
      updated_by: "alice",
      updated_at: "2026-05-21 10:00:00",
    });

    render(<SystemConfigPage />);

    expect(await screen.findByText("工具输出控制")).toBeTruthy();

    fireEvent.click(getToolResultCompactSwitch());
    fireEvent.change(screen.getByDisplayValue("12000"), {
      target: { value: "16000" },
    });
    fireEvent.click(screen.getByRole("button", { name: "common.save" }));

    await waitFor(() => {
      expect(mocks.sourceSystemConfigApi.updateCurrent).toHaveBeenCalledWith({
        config: {
          provider_policy: { default_model: "qwen-max" },
          tool_result_compact: {
            enabled: false,
            recent_max_bytes: 16000,
            unknown_retained: "yes",
          },
        },
      });
    });
    expect(loadEffectiveConfig).toHaveBeenCalledWith("portal");
  });

  it("saves system prompt injections", async () => {
    mocks.sourceSystemConfigApi.getCurrent.mockResolvedValueOnce({
      source_id: "portal",
      config: {
        system_prompt_injections: ["source prompt"],
      },
      version: 1,
      is_default: false,
      updated_by: "alice",
      updated_at: "2026-05-21 10:00:00",
    });
    mocks.sourceSystemConfigApi.updateCurrent.mockResolvedValue({
      source_id: "portal",
      config: {
        system_prompt_injections: ["source prompt", "runtime rule"],
      },
      version: 2,
      is_default: false,
      updated_by: "alice",
      updated_at: "2026-05-21 11:00:00",
    });

    render(<SystemConfigPage />);

    expect(await screen.findByLabelText("提示词片段 1")).toHaveValue(
      "source prompt",
    );
    fireEvent.click(screen.getByRole("button", { name: "新增提示词片段" }));
    fireEvent.change(screen.getByLabelText("提示词片段 2"), {
      target: { value: "runtime rule" },
    });
    fireEvent.click(screen.getByRole("button", { name: "common.save" }));

    await waitFor(() => {
      expect(mocks.sourceSystemConfigApi.updateCurrent).toHaveBeenCalledWith({
        config: {
          system_prompt_injections: ["source prompt", "runtime rule"],
        },
      });
    });
  });

  it("saves model call policy overrides and restores inheritance", async () => {
    seedEffectiveConfig();
    mocks.sourceSystemConfigApi.getCurrent.mockResolvedValueOnce({
      source_id: "portal",
      config: {
        provider_policy: { default_model: "qwen-max" },
        query_retry: {
          enabled: true,
          max_retries: 2,
        },
        llm_rate_limiter: {
          llm_chat_max_concurrent: 1,
        },
      },
      version: 1,
      is_default: false,
      updated_by: "alice",
      updated_at: "2026-05-21 10:00:00",
    });
    mocks.sourceSystemConfigApi.updateCurrent.mockResolvedValue({
      source_id: "portal",
      config: {
        provider_policy: { default_model: "qwen-max" },
        llm_rate_limiter: {
          llm_chat_max_concurrent: 1,
          llm_max_qpm: 12,
        },
      },
      version: 2,
      is_default: false,
      updated_by: "alice",
      updated_at: "2026-05-21 11:00:00",
    });

    render(<SystemConfigPage />);

    expect(await screen.findByText("模型调用策略")).toBeTruthy();
    expect(screen.getByText("查询重试")).toBeTruthy();
    expect(screen.getByText("LLM 并发限流")).toBeTruthy();

    fireEvent.click(screen.getAllByRole("button", { name: "恢复继承" })[0]);
    fireEvent.change(screen.getByDisplayValue("100"), {
      target: { value: "12" },
    });
    fireEvent.click(screen.getByRole("button", { name: "common.save" }));

    await waitFor(() => {
      expect(mocks.sourceSystemConfigApi.updateCurrent).toHaveBeenCalledWith({
        config: {
          provider_policy: { default_model: "qwen-max" },
          llm_rate_limiter: {
            llm_chat_max_concurrent: 1,
            llm_max_qpm: 12,
          },
        },
      });
    });
  });

  it("enables model call policy override without saving page defaults implicitly", async () => {
    seedEffectiveConfig();
    mocks.sourceSystemConfigApi.updateCurrent.mockResolvedValue({
      source_id: "portal",
      config: {
        query_retry: {
          enabled: false,
          max_retries: 3,
          backoff_base: 2,
          backoff_cap: 30,
        },
      },
      version: 1,
      is_default: false,
      updated_by: "alice",
      updated_at: "2026-05-21 11:00:00",
    });

    render(<SystemConfigPage />);

    expect(await screen.findByText("模型调用策略")).toBeTruthy();
    expect(screen.getAllByText("继承 Agent 运行配置").length).toBeGreaterThan(
      0,
    );

    fireEvent.click(screen.getAllByRole("button", { name: "启用覆盖" })[0]);
    fireEvent.click(screen.getByRole("button", { name: "common.save" }));

    await waitFor(() => {
      expect(mocks.sourceSystemConfigApi.updateCurrent).toHaveBeenCalledWith({
        config: {
          query_retry: {
            enabled: false,
            max_retries: 3,
            backoff_base: 2,
            backoff_cap: 30,
          },
        },
      });
    });
  });

  it("copies effective model call policy values when enabling override", async () => {
    seedEffectiveConfig({
      query_retry: {
        enabled: true,
        max_retries: 5,
        backoff_base: 1.5,
        backoff_cap: 60,
      },
    });
    mocks.sourceSystemConfigApi.updateCurrent.mockResolvedValue({
      source_id: "portal",
      config: {
        query_retry: {
          enabled: true,
          max_retries: 5,
          backoff_base: 1.5,
          backoff_cap: 60,
        },
      },
      version: 1,
      is_default: false,
      updated_by: "alice",
      updated_at: "2026-05-21 11:00:00",
    });

    render(<SystemConfigPage />);

    expect(await screen.findByText("模型调用策略")).toBeTruthy();

    fireEvent.click(screen.getAllByRole("button", { name: "启用覆盖" })[0]);
    fireEvent.click(screen.getByRole("button", { name: "common.save" }));

    await waitFor(() => {
      expect(mocks.sourceSystemConfigApi.updateCurrent).toHaveBeenCalledWith({
        config: {
          query_retry: {
            enabled: true,
            max_retries: 5,
            backoff_base: 1.5,
            backoff_cap: 60,
          },
        },
      });
    });
  });

  it("waits for effective config before enabling model call policy override", async () => {
    const effectiveDeferred = createDeferred<void>();
    loadEffectiveConfig.mockReturnValueOnce(effectiveDeferred.promise);
    mocks.sourceSystemConfigApi.updateCurrent.mockResolvedValue({
      source_id: "portal",
      config: {
        query_retry: {
          enabled: true,
          max_retries: 5,
          backoff_base: 1.5,
          backoff_cap: 60,
        },
      },
      version: 1,
      is_default: false,
      updated_by: "alice",
      updated_at: "2026-05-21 11:00:00",
    });

    render(<SystemConfigPage />);

    expect(await screen.findByText("模型调用策略")).toBeTruthy();
    expect(
      screen.getAllByRole("button", { name: "启用覆盖" })[0],
    ).toBeDisabled();

    act(() => {
      useSourceSystemConfigStore.setState({
        config: {
          source_id: "portal",
          config: {
            query_retry: {
              enabled: true,
              max_retries: 5,
              backoff_base: 1.5,
              backoff_cap: 60,
            },
          },
          version: 1,
          is_default: false,
          stale: false,
          updated_by: "alice",
          updated_at: "2026-05-21 10:00:00",
        },
        sourceId: "portal",
      });
    });
    await act(async () => {
      effectiveDeferred.resolve(undefined);
      await effectiveDeferred.promise;
    });

    await waitFor(() => {
      expect(
        screen.getAllByRole("button", { name: "启用覆盖" })[0],
      ).toBeEnabled();
    });

    fireEvent.click(screen.getAllByRole("button", { name: "启用覆盖" })[0]);
    fireEvent.click(screen.getByRole("button", { name: "common.save" }));

    await waitFor(() => {
      expect(mocks.sourceSystemConfigApi.updateCurrent).toHaveBeenCalledWith({
        config: {
          query_retry: {
            enabled: true,
            max_retries: 5,
            backoff_base: 1.5,
            backoff_cap: 60,
          },
        },
      });
    });
  }, 15_000);

  it("uses the tool output override for new and recent output without a file-read field", async () => {
    mocks.sourceSystemConfigApi.getCurrent.mockResolvedValueOnce({
      source_id: "portal",
      config: {
        provider_policy: { default_model: "qwen-max" },
        tool_result_compact: {
          recent_max_bytes: 12000,
        },
        file_read_truncation: {
          enabled: true,
          max_bytes: 12000,
        },
      },
      version: 1,
      is_default: false,
      updated_by: "alice",
      updated_at: "2026-05-21 10:00:00",
    });
    mocks.sourceSystemConfigApi.updateCurrent.mockResolvedValue({
      source_id: "portal",
      config: {
        provider_policy: { default_model: "qwen-max" },
        tool_result_compact: {
          recent_max_bytes: 16000,
        },
      },
      version: 2,
      is_default: false,
      updated_by: "alice",
      updated_at: "2026-05-21 11:00:00",
    });

    render(<SystemConfigPage />);

    expect(await screen.findByText("工具输出控制")).toBeTruthy();
    expect(
      screen.getByText(
        "recent_max_bytes 同时控制新产生和近期的工具输出；old_max_bytes 仅控制历史工具输出。",
      ),
    ).toBeTruthy();
    expect(screen.queryByText("文件读取截断")).toBeNull();

    fireEvent.change(screen.getByDisplayValue("12000"), {
      target: { value: "16000" },
    });
    fireEvent.click(screen.getByRole("button", { name: "common.save" }));

    await waitFor(() => {
      expect(mocks.sourceSystemConfigApi.updateCurrent).toHaveBeenCalledWith({
        config: {
          provider_policy: { default_model: "qwen-max" },
          tool_result_compact: {
            recent_max_bytes: 16000,
          },
        },
      });
    });
  });

  it("blocks invalid tool result compact thresholds before saving", async () => {
    mocks.sourceSystemConfigApi.updateCurrent.mockResolvedValue({
      source_id: "portal",
      config: {
        tool_result_compact: {
          recent_max_bytes: 4000,
        },
      },
      version: 1,
      is_default: false,
      updated_by: "alice",
      updated_at: "2026-05-21 10:00:00",
    });

    render(<SystemConfigPage />);

    expect(await screen.findByText("工具输出控制")).toBeTruthy();

    fireEvent.change(screen.getByDisplayValue("50000"), {
      target: { value: "1000" },
    });
    fireEvent.click(screen.getByRole("button", { name: "common.save" }));

    await waitFor(() => {
      expect(mocks.messageApi.error).toHaveBeenCalledWith(
        "近期结果预览字节数不能小于旧结果预览字节数",
      );
    });
    expect(mocks.sourceSystemConfigApi.updateCurrent).not.toHaveBeenCalled();

    expect(screen.getByRole("button", { name: "common.save" })).toBeEnabled();
    fireEvent.change(screen.getByDisplayValue("1000"), {
      target: { value: "4000" },
    });
    fireEvent.click(screen.getByRole("button", { name: "common.save" }));

    await waitFor(() => {
      expect(mocks.sourceSystemConfigApi.updateCurrent).toHaveBeenCalledWith({
        config: {
          tool_result_compact: {
            recent_max_bytes: 4000,
          },
        },
      });
    });
  }, 15_000);

  it("confirms restoring defaults before clearing explicit config", async () => {
    mocks.sourceSystemConfigApi.getCurrent
      .mockResolvedValueOnce({
        source_id: "portal",
        config: {
          feature_switches: {
            chat_task_progress_enabled: false,
          },
        },
        version: 2,
        is_default: false,
        updated_by: "alice",
        updated_at: "2026-05-20 22:00:00",
      })
      .mockResolvedValueOnce({
        source_id: "portal",
        config: {},
        version: 0,
        is_default: true,
        updated_by: null,
        updated_at: null,
      });
    mocks.sourceSystemConfigApi.deleteCurrent.mockResolvedValue({
      deleted: true,
    });

    render(<SystemConfigPage />);

    expect(await screen.findByText("存在显式覆盖")).toBeTruthy();

    fireEvent.click(screen.getByRole("button", { name: "恢复默认设置" }));

    const dialog = await screen.findByRole("dialog");
    expect(within(dialog).getByText("恢复当前系统的默认设置？")).toBeTruthy();
    expect(mocks.sourceSystemConfigApi.deleteCurrent).not.toHaveBeenCalled();

    fireEvent.click(
      within(dialog).getByRole("button", { name: "恢复默认设置" }),
    );

    await waitFor(() => {
      expect(mocks.sourceSystemConfigApi.deleteCurrent).toHaveBeenCalledTimes(
        1,
      );
    });
    expect(loadEffectiveConfig).toHaveBeenCalledWith("portal");
    expect(await screen.findByText("继承默认值")).toBeTruthy();
  });

  it("keeps unsaved changes when default restoration is cancelled", async () => {
    mocks.sourceSystemConfigApi.getCurrent.mockResolvedValue({
      source_id: "portal",
      config: {
        feature_switches: {
          chat_task_progress_enabled: false,
        },
      },
      version: 2,
      is_default: false,
      updated_by: "alice",
      updated_at: "2026-05-20 22:00:00",
    });

    render(<SystemConfigPage />);

    await screen.findByText("存在显式覆盖");
    fireEvent.click(getTaskProgressSwitch());
    fireEvent.click(screen.getByRole("button", { name: "恢复默认设置" }));

    const dialog = await screen.findByRole("dialog");
    fireEvent.click(within(dialog).getByRole("button", { name: /取\s*消/ }));

    expect(mocks.sourceSystemConfigApi.deleteCurrent).not.toHaveBeenCalled();
    expect(screen.getByText("存在未保存修改")).toBeTruthy();
  });

  it("closes default restoration confirmation when the active source changes", async () => {
    mocks.sourceSystemConfigApi.getCurrent
      .mockResolvedValueOnce({
        source_id: "portal",
        config: {
          feature_switches: {
            chat_task_progress_enabled: false,
          },
        },
        version: 2,
        is_default: false,
        updated_by: "alice",
        updated_at: "2026-05-20 22:00:00",
      })
      .mockResolvedValueOnce({
        source_id: "workspace",
        config: {},
        version: 0,
        is_default: true,
        updated_by: null,
        updated_at: null,
      });

    render(<SystemConfigPage />);

    await screen.findByText("存在显式覆盖");
    fireEvent.click(screen.getByRole("button", { name: "恢复默认设置" }));
    expect(await screen.findByRole("dialog")).toBeTruthy();

    act(() => {
      useIframeStore.getState().setContext({ source: "workspace" });
    });

    await waitFor(() => {
      expect(screen.queryByRole("dialog")).toBeNull();
    });
    expect(mocks.sourceSystemConfigApi.deleteCurrent).not.toHaveBeenCalled();
  });

  it("clears stale draft and blocks save when the next source load fails", async () => {
    mocks.sourceSystemConfigApi.getCurrent
      .mockResolvedValueOnce({
        source_id: "portal",
        config: {
          feature_switches: {
            chat_task_progress_enabled: false,
          },
        },
        version: 2,
        is_default: false,
        updated_by: "alice",
        updated_at: "2026-05-20 22:00:00",
      })
      .mockRejectedValueOnce(new Error("retail load failed"));

    render(<SystemConfigPage />);

    await waitFor(() => {
      expect(getTaskProgressSwitch()).toHaveAttribute("aria-checked", "false");
    });

    act(() => {
      useIframeStore.getState().setContext({
        source: "retail",
      });
    });

    expect(await screen.findByText("当前系统配置请求失败")).toBeTruthy();
    expect(getTaskProgressSwitch()).toHaveAttribute("aria-checked", "true");
    expect(screen.getByRole("button", { name: "common.save" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "恢复默认设置" })).toBeDisabled();

    fireEvent.click(screen.getByRole("button", { name: "common.save" }));
    expect(mocks.sourceSystemConfigApi.updateCurrent).not.toHaveBeenCalled();
  });

  it("ignores stale save responses after switching to another source", async () => {
    const saveDeferred = createDeferred<{
      source_id: string;
      config: Record<string, unknown>;
      version: number;
      is_default: boolean;
      updated_by: string | null;
      updated_at: string | null;
    }>();
    mocks.sourceSystemConfigApi.getCurrent
      .mockResolvedValueOnce({
        source_id: "portal",
        config: {
          feature_switches: {
            chat_task_progress_enabled: false,
          },
        },
        version: 1,
        is_default: false,
        updated_by: "alice",
        updated_at: "2026-05-20 22:00:00",
      })
      .mockResolvedValueOnce({
        source_id: "retail",
        config: {},
        version: 0,
        is_default: true,
        updated_by: null,
        updated_at: null,
      });
    mocks.sourceSystemConfigApi.updateCurrent.mockReturnValueOnce(
      saveDeferred.promise,
    );

    render(<SystemConfigPage />);

    await waitFor(() => {
      expect(getTaskProgressSwitch()).toHaveAttribute("aria-checked", "false");
    });
    loadEffectiveConfig.mockClear();

    fireEvent.click(screen.getByRole("button", { name: "common.save" }));

    act(() => {
      useIframeStore.getState().setContext({
        source: "retail",
      });
    });

    await waitFor(() => {
      expect(screen.getAllByText("retail").length).toBeGreaterThan(0);
      expect(getTaskProgressSwitch()).toHaveAttribute("aria-checked", "true");
    });

    await act(async () => {
      saveDeferred.resolve({
        source_id: "portal",
        config: {
          feature_switches: {
            chat_task_progress_enabled: false,
          },
        },
        version: 2,
        is_default: false,
        updated_by: "alice",
        updated_at: "2026-05-21 10:00:00",
      });
      await saveDeferred.promise;
    });

    await waitFor(() => {
      expect(screen.getAllByText("retail").length).toBeGreaterThan(0);
      expect(getTaskProgressSwitch()).toHaveAttribute("aria-checked", "true");
    });
    expect(loadEffectiveConfig).not.toHaveBeenCalledWith("portal");
  });
});
