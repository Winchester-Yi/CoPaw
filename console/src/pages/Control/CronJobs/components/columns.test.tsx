import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, render, screen } from "@testing-library/react";
import type { ReactElement } from "react";
import type { TFunction } from "i18next";
import type { CronJobSpecOutput } from "@/api/types";
import {
  createColumns,
  getBroadcastParentInfo,
  isBroadcastChildJob,
} from "./columns";

function buildCronJob(
  overrides: Partial<CronJobSpecOutput> = {},
): CronJobSpecOutput {
  return {
    id: "job-1",
    name: "test job",
    enabled: true,
    schedule: {
      type: "cron",
      cron: "0 9 * * *",
      timezone: "UTC",
    },
    task_type: "agent",
    request: {
      input: [{ role: "user", content: [{ type: "text", text: "hello" }] }],
      session_id: "session-1",
      user_id: "user-1",
    },
    dispatch: {
      type: "channel",
      channel: "console",
      target: {
        user_id: "user-1",
        session_id: "session-1",
      },
      mode: "final",
    },
    runtime: {
      max_concurrency: 1,
    },
    meta: {},
    ...overrides,
  };
}

function buildHandlers(overrides = {}) {
  return {
    onToggleEnabled: vi.fn(),
    onExecuteNow: vi.fn(),
    onBroadcast: vi.fn(),
    onManageChildren: vi.fn(),
    onBatchConfigure: vi.fn(),
    onEdit: vi.fn(),
    onDelete: vi.fn(),
    onCopySuccess: vi.fn(),
    onCopyError: vi.fn(),
    executionModelOptions: [],
    tenantDefaultModelLabel: "Tenant default",
    t: ((key: string) => key) as TFunction,
    ...overrides,
  };
}

describe("CronJobs columns", () => {
  it.each([
    [{}, true, true],
    [{ broadcast_dispatch_intents_enabled: false }, true, true],
    [{ broadcast_dispatch_intents_enabled: true }, true, false],
    [{ broadcast_dispatch_intents_enabled: true }, false, false],
    [
      {
        broadcast_dispatch_intents_enabled: true,
        broadcast_source_job_id: "parent",
      },
      true,
      true,
    ],
  ])(
    "gates batch configuration by saved parent mode, not job enabled",
    (meta, enabled, disabled) => {
      const handlers = buildHandlers();
      const job = buildCronJob({ meta, enabled });
      const action = createColumns(handlers).find(
        (column) => column.key === "action",
      );
      const node = action?.render?.(undefined, job, 0) as ReactElement<{
        children: ReactElement[];
      }>;
      expect(node.props.children).toHaveLength(3);
      const dropdown = node.props.children[2] as ReactElement<{
        menu: {
          items: {
            key: string;
            label: string;
            disabled: boolean;
            onClick: () => void;
          }[];
        };
      }>;
      const item = dropdown.props.menu.items.find(
        (entry) => entry.key === "batch_configuration",
      );
      expect(item?.label).toBe("批调度配置");
      expect(item?.disabled).toBe(disabled);
      item?.onClick();
      expect(handlers.onBatchConfigure).toHaveBeenCalledTimes(disabled ? 0 : 1);
    },
  );
  afterEach(() => {
    cleanup();
  });

  it("displays notification delay", () => {
    const columns = createColumns(buildHandlers());
    const column = columns.find((item) => item.key === "notification_delay");
    const job = buildCronJob({
      meta: {
        notification_delay_minutes: 120,
      },
    });

    expect(column?.render?.(undefined, job, 0)).toBe("2 小时");
  });

  it("extracts broadcast parent information from child task metadata", () => {
    const job = buildCronJob({
      meta: {
        broadcast_source_job_id: "parent-job",
        broadcast_source_job_name: "Parent Task",
        broadcast_source_tenant_id: "tenant-parent",
        broadcast_source_tenant_name: "Parent User",
        broadcast_source_bbk_id: "BBK001",
      },
    });

    expect(isBroadcastChildJob(job)).toBe(true);
    expect(getBroadcastParentInfo(job)).toEqual({
      sourceJobId: "parent-job",
      sourceJobName: "Parent Task",
      sourceTenantId: "tenant-parent",
      sourceTenantName: "Parent User",
      sourceBbkId: "BBK001",
    });
  });

  it("marks broadcast child tasks in the name column", () => {
    const columns = createColumns(buildHandlers());
    const column = columns.find((item) => item.key === "name");
    const job = buildCronJob({
      meta: {
        broadcast_source_job_id: "parent-job",
      },
    });

    render(<>{column?.render?.(undefined, job, 0)}</>);

    expect(screen.getByText("test job")).toBeTruthy();
    expect(screen.getByText("分发子任务")).toBeTruthy();
  });

  it("marks batch dispatch parent tasks and keeps their own cron text", () => {
    const columns = createColumns(buildHandlers());
    const column = columns.find((item) => item.key === "cron");
    const job = buildCronJob({
      meta: {
        broadcast_dispatch_intents_enabled: true,
        batch_dispatch_cron: "0 5 * * *",
        batch_dispatch_offset_minutes: 240,
      },
    });

    render(<>{column?.render?.(job.schedule.cron, job, 0)}</>);

    expect(screen.getByText("批调度")).toBeTruthy();
    expect(screen.getByText(/cronJobs\.cronTypeDaily/)).toBeTruthy();
    expect(screen.getByText(/09:00/)).toBeTruthy();
    expect(screen.queryByText(/05:00/)).toBeNull();
  });

  it("marks batch dispatch child tasks and keeps their own cron text", () => {
    const columns = createColumns(buildHandlers());
    const column = columns.find((item) => item.key === "cron");
    const job = buildCronJob({
      schedule: {
        type: "cron",
        cron: "30 10 * * *",
        timezone: "UTC",
      },
      meta: {
        broadcast_source_job_id: "parent-job",
        broadcast_dispatch_intents_enabled: true,
        batch_dispatch_cron: "0 5 * * *",
        batch_dispatch_offset_minutes: 240,
      },
    });

    render(<>{column?.render?.(job.schedule.cron, job, 0)}</>);

    expect(screen.getByText("批调度")).toBeTruthy();
    expect(screen.getByText(/cronJobs\.cronTypeDaily/)).toBeTruthy();
    expect(screen.getByText(/10:30/)).toBeTruthy();
    expect(screen.queryByText(/05:00/)).toBeNull();
  });

  it("restores the cron text when batch dispatch metadata is removed", () => {
    const columns = createColumns(buildHandlers());
    const column = columns.find((item) => item.key === "cron");
    const job = buildCronJob({
      meta: {},
    });

    render(<>{column?.render?.(job.schedule.cron, job, 0)}</>);

    expect(screen.getByText(/cronJobs\.cronTypeDaily/)).toBeTruthy();
    expect(screen.getByText(/09:00/)).toBeTruthy();
    expect(screen.queryByText("批调度")).toBeNull();
  });

  it("disables broadcast actions for broadcast child tasks", () => {
    const columns = createColumns(buildHandlers());
    const column = columns.find((item) => item.key === "action");
    const job = buildCronJob({
      meta: {
        broadcast_source_job_id: "parent-job",
      },
    });

    const actionNode = column?.render?.(undefined, job, 0) as ReactElement<{
      children: ReactElement[];
    }>;
    const children = actionNode.props.children;
    const dropdown = children[2] as ReactElement<{
      menu: { items: Array<{ disabled?: boolean; key?: string }> };
    }>;

    expect(dropdown.props.menu.items[0].disabled).toBe(true);
    expect(dropdown.props.menu.items[1].disabled).toBe(true);
    expect(
      dropdown.props.menu.items.some((item) => item.key === "batch_dispatch"),
    ).toBe(false);
  });

  it("does not show batch dispatch toggle action for parent tasks", () => {
    const columns = createColumns(buildHandlers());
    const column = columns.find((item) => item.key === "action");
    const job = buildCronJob();

    const actionNode = column?.render?.(undefined, job, 0) as ReactElement<{
      children: ReactElement[];
    }>;
    const children = actionNode.props.children;
    const dropdown = children[2] as ReactElement<{
      menu: { items: Array<{ key?: string; label?: string }> };
    }>;

    expect(dropdown.props.menu.items.map((item) => item.key)).toEqual([
      "broadcast",
      "broadcast_children",
      "batch_configuration",
      "edit",
      "delete",
    ]);
    expect(dropdown.props.menu.items.map((item) => item.label)).not.toContain(
      "启动批调度",
    );
    expect(dropdown.props.menu.items.map((item) => item.label)).not.toContain(
      "关闭批调度",
    );
  });
});
