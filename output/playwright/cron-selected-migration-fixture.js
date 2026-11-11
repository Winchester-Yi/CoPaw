async (page) => {
  const root = "http://127.0.0.1:5198";
  await page.addInitScript(() => {
    sessionStorage.setItem("swe-iframe-context", JSON.stringify({ state: {
      userId: "preview-manager", source: "preview-bank", manager: true,
      isSuperManager: false, initialized: true,
    }, version: 0 }));
  });
  const job = {
    id: "original-parent-93bc0888-eb5c-5fc9-ad52-a230b9491953", name: "每日客户经营报告",
    tenant_id: "preview-manager", source_id: "preview-bank", enabled: true,
    schedule: { type: "cron", cron: "0 9 * * *", timezone: "Asia/Shanghai" },
    task_type: "text", text: "生成报告", meta: { creator_user_id: "preview-manager" },
    dispatch: { channel: "console", target: { user_id: "preview-manager", session_id: "original-session" } },
    state: { last_status: "success" }, runtime: { max_concurrency: 1, timeout_seconds: 7200, misfire_grace_seconds: 300 },
  };
  const progress = { operation_id: "preview-migration-operation", status: "awaiting_confirmation", root_count: 1, member_count: 4,
    groups: [{ job_id: job.id, status: "preview_ready", member_count: 4, warnings: [] }] };
  await page.route(root + "/api/**", async route => {
    const path = new URL(route.request().url()).pathname;
    let body = [];
    if (path === "/api/auth/status") body = { enabled: false };
    else if (path.endsWith("/cron/jobs")) body = [job, { ...job, id: "original-child", name: "客户报告子任务", meta: { ...job.meta, broadcast_source_job_id: job.id } }];
    else if (path === "/api/scheduler/cron/migrations" && route.request().method() === "POST") body = progress;
    else if (path === "/api/scheduler/cron/migrations") body = { items: [progress] };
    else if (path.endsWith("/confirm")) { progress.status = "completed"; progress.groups[0].status = "completed"; body = progress; }
    else if (path.includes("/scheduler/cron/migrations/")) body = progress;
    else if (path.includes("timezone")) body = { timezone: "Asia/Shanghai" };
    else if (path.includes("skills")) body = { skills: [] };
    await route.fulfill({ json: body });
  });
  await page.setViewportSize({ width: 1500, height: 1000 });
  await page.goto(root + "/cron-jobs");
  await page.evaluate(async () => {
    const module = await import("/src/stores/iframeStore.ts");
    module.useIframeStore.getState().setContext({ userId: "preview-manager", source: "preview-bank", manager: true });
  });
}
