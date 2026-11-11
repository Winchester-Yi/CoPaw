async (page) => {
  const root = "http://127.0.0.1:5198";
  await page.addInitScript(() => sessionStorage.setItem("swe-iframe-context", JSON.stringify({ state: {
    userId: "preview-manager", source: "preview-bank", manager: true, initialized: true,
  }, version: 0 })));
  const now = new Date().toISOString();
  const batch = { batch_id: "preview-batch", job_id: "preview-parent", job_name: "客户经营日报",
    tenant_id: "preview-manager", source_id: "preview-bank", agent_id: "default", status: "failed",
    member_count: 2, done_count: 2, failed_count: 1, success_count: 1, skipped_count: 0,
    active_count: 0, waiting_count: 0, completion_rate: 1, logical_fire_at: now, physical_fire_at: now, created_at: now };
  const member = { member_id: "member", job_id: "preview-child", execution_id: "preview-execution", job_name: "客户报告",
    tenant_id: "preview-user", member_role: "child", member_order: 0, business_attempt: 2,
    status: "success", settled_status: "error", error_message: "子任务执行失败：客户报告生成失败",
    provider_id: "default", model_id: "deepseek-v4-flash", dispatch_priority: {}, viewer_heat_score: 0,
    started_at: now, finished_at: now, not_before_at: now };
  const groups = [{ failure_type: "configuration", label: "配置错误", count: 30, details: [
    { message: "workflow sapId does not match target user", count: 12 }, { message: "workflow 缺少用户信息：userId", count: 18 },
  ] }, { failure_type: "subtask_failed", label: "子任务失败", count: 26, details: [
    { message: "子任务执行失败：客户报告生成失败", count: 25 }, { message: "子任务报告写入失败", count: 1 },
  ] }];
  const events = [{ event_id: "event", event_type: "retry_skipped", execution_id: "preview-execution", actor_id: "preview-user", occurred_at: now,
    details: { call_id: "preview-call", decision: "skipped", reason: "cron_daily_retry_already_running",
      context: { description: "本次跳过重试的完整说明，用于验证悬停后能够自动换行并读取原本被截断的详情。".repeat(8), retry_count: 2 } } }];
  await page.route(root + "/api/**", async route => {
    const path = new URL(route.request().url()).pathname;
    let data = [];
    if (path === "/api/auth/status") data = { enabled: false };
    else if (path.endsWith("/workers")) data = { source_id: "preview-bank", policies: [], current_capacity: [], capacity_events: [], capacity_events_next_cursor: null };
    else if (path.endsWith("/failure-summary")) data = { total: 56, groups, queried_at: now };
    else if (path === "/api/monitor/cron/managed-dispatch/batches") data = { items: [batch], total: 58, page: 1, page_size: 4,
      stats: { total_batches: 58, total_members: 214, done_members: 214, failed_members: 56, running_batches: 0 } };
    else if (path.startsWith("/api/monitor/cron/managed-dispatch/batches/")) data = { batch, member_total: 2, events, event_total: 1,
      members: [member, { ...member, member_id: "member-success", member_order: 1, job_id: "child-success", job_name: "已完成客户报告", settled_status: "success", error_message: "" }] };
    else if (path.endsWith("/failures")) data = { counts: [], items: [], matched_count: 0, has_more: false };
    await route.fulfill({ json: data });
  });
  await page.setViewportSize({ width: 1920, height: 1080 });
  await page.goto(root + "/monitor/cron-batch-dispatch-new");
  await page.evaluate(async () => {
    const { useIframeStore } = await import("/src/stores/iframeStore.ts");
    useIframeStore.getState().setContext({ userId: "preview-manager", source: "preview-bank", manager: true });
  });
}
