async (page) => {
  const root = "http://127.0.0.1:5183";
  const longName = "客户经营日报与财富规划任务名称超长展示验证".repeat(4);
  const parent = {
    id: "93bc0888-eb5c-5fc9-ad52-a230b9491953", name: longName, enabled: true,
    tenant_id: "preview-user", source_id: "preview-bank", agent_id: "default",
    definition_version: 3, management_version: 8, state_version: 2,
    management_owner: "scheduler", management_status: "ready", deleted: false,
    dispatch_state: "batch_enabled", run_date_range: { start_date: "2026-11-01", end_date: "2026-12-01" },
    schedule: { type: "cron", cron: "0 10 * * *", timezone: "Asia/Shanghai" },
    task_type: "text", text: "浏览器本地验收，不访问真实业务库",
    runtime: { max_concurrency: 1, timeout_seconds: 7200, misfire_grace_seconds: 300 },
    dispatch: { type: "channel", channel: "console", mode: "final", target: { user_id: "preview-user", session_id: "preview-session" } },
    meta: {}, state: { last_status: "success", last_run_at: "2026-11-03T02:00:00" },
  };
  const child = { ...parent, id: "child-preview-id-with-long-text-12345678901234567890", name: "分发子任务", meta: { broadcast_source_job_id: parent.id } };
  const failed = { ...parent, id: "failed-preview", name: "已过期且人工停用的批任务", enabled: false, dispatch_state: "batch_paused", run_date_range: { start_date: "2026-10-01", end_date: "2026-10-31" }, management_status: "update_failed", state: { last_status: "error", last_run_at: "2026-11-03T01:00:00", last_error: "workflow sapId does not match target user\n完整失败详情，支持换行展示。" } };
  await page.addInitScript(() => {
    sessionStorage.setItem("swe-iframe-context", JSON.stringify({ state: { userId: "preview-user", source: "preview-bank", manager: true, initialized: true }, version: 0 }));
  });
  await page.unroute("**/api/**");
  await page.unroute(root + "/api/**");
  await page.route(root + "/api/**", async (route) => {
    const url = new URL(route.request().url());
    const path = url.pathname;
    let body = [];
    if (path.endsWith("/auth/status")) body = { enabled: false };
    else if (path.endsWith("/broadcast/children")) body = { items: [child], next_cursor: null };
    else if (path.endsWith("/dispatch-state")) body = { job_id: parent.id, state: "batch_enabled", version: 8, effective_state: "batch_enabled", sync_status: "completed", completed_count: 2, pending_count: 0, failed_count: 0 };
    else if (path.endsWith("/priority")) body = { priority: { user_ids: [], branch_ids: [] }, management_version: 8 };
    else if (path.endsWith("/run-date-range")) body = { operation: { status: "completed", operation_id: "preview-date", job_id: parent.id } };
    else if (path === "/api/scheduler/cron/jobs") body = { items: [parent, child, failed], next_cursor: null };
    else if (path.includes("/scheduler/cron/jobs/")) body = { ...parent, spec: parent };
    else if (path.includes("timezone")) body = { timezone: "Asia/Shanghai" };
    else if (path.includes("skills")) body = { skills: [] };
    await route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(body) });
  });
  await page.setViewportSize({ width: 1680, height: 1050 });
  await page.goto(root + "/scheduler-cron-jobs");
  await page.evaluate(async () => {
    const module = await import("/src/stores/iframeStore.ts");
    module.useIframeStore.getState().setContext({ userId: "preview-user", source: "preview-bank", manager: true });
  });
  await page.getByRole("columnheader", { name: "最近执行结果" }).waitFor();
  if (await page.getByRole("columnheader").count() !== 8) throw new Error("expected 8 columns");
  await page.screenshot({ path: "../output/playwright/managed-cron-ui-list-20261103.png", fullPage: true });
  const parentRow = page.getByRole("row").filter({ has: page.getByRole("button", { name: `更多操作 ${longName}`, exact: true }) });
  await parentRow.locator('[class*="identityText"]').first().hover();
  await page.getByRole("tooltip").filter({ hasText: longName }).waitFor();
  await page.screenshot({ path: "../output/playwright/managed-cron-ui-name-hover-20261103.png", fullPage: true });
  await parentRow.locator('[class*="identityText"]').nth(1).hover();
  await page.getByRole("tooltip").filter({ hasText: parent.id }).waitFor();
  await page.getByText("失败", { exact: true }).hover();
  await page.getByRole("tooltip").filter({ hasText: "workflow sapId" }).waitFor();
  await page.screenshot({ path: "../output/playwright/managed-cron-ui-error-20261103.png", fullPage: true });
  await page.getByRole("button", { name: "更多操作 分发子任务", exact: true }).click();
  await page.getByRole("menuitem", { name: "任务设置", exact: true }).waitFor();
  if (await page.getByRole("menuitem", { name: "暂停本任务", exact: true }).count()) throw new Error("duplicate child pause");
  await page.screenshot({ path: "../output/playwright/managed-cron-ui-child-menu-20261103.png", fullPage: true });
  await page.getByRole("columnheader", { name: "管理状态", exact: true }).click();
  await page.getByRole("menuitem", { name: "任务设置", exact: true }).waitFor({ state: "hidden" });
  await page.getByRole("button", { name: `更多操作 ${longName}`, exact: true }).click();
  await page.getByRole("menuitem", { name: "任务设置", exact: true }).click();
  await page.getByPlaceholder("开始日期").waitFor();
  if (!await page.locator("input#name").isDisabled()) throw new Error("parent content must be read-only");
  if (await page.getByPlaceholder("开始日期").isDisabled()) throw new Error("date must remain editable");
  await page.screenshot({ path: "../output/playwright/managed-cron-ui-settings-20261103.png", fullPage: true });
  await page.getByRole("tab", { name: "调度设置", exact: true }).click();
  await page.getByText("批次内优先策略", { exact: true }).waitFor();
  await page.screenshot({ path: "../output/playwright/managed-cron-ui-scheduling-20261103.png", fullPage: true });
  return { columns: 8, parentDatesEditable: true, childMenuSimplified: true, errorDetailsWithTime: true };
}
