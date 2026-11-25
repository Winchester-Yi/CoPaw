# -*- coding: utf-8 -*-
"""补跑接口的真实 SQLite 事务结构，仅用于本地测试。"""

from datetime import timedelta, timezone
from uuid import uuid4

from scheduler.app.services.cron.managed.executions import (
    ExecutionRequest,
    ExecutionStore,
)
from scheduler.app.services.cron.managed.identity import CallerIdentity
from scheduler.app.services.cron.managed.management import ManagementService
from tests.unit.scheduler.test_managed_cron_callbacks import CallbackDatabase
from tests.unit.scheduler.test_managed_cron_management import (
    ResourceClient,
    create_payload,
)
from tests.unit.scheduler.test_managed_cron_dispatch_state import ModePlatform
from swe.app.crons.managed_results import ExecutionWriter, ExecutionOutcome


class DailyRetryDatabase(CallbackDatabase):
    def __init__(self):
        super().__init__()
        self.conn.executescript("""
            CREATE TABLE swe_cron_failure_retry_calls (
                call_id TEXT PRIMARY KEY, source_id TEXT COLLATE NOCASE,
                business_date TEXT, window_start TEXT, cutoff_at TEXT,
                max_queue_order INTEGER, queue_cursor INTEGER DEFAULT 0,
                status TEXT, scanned_count INTEGER DEFAULT 0,
                queued_count INTEGER DEFAULT 0, skipped_count INTEGER DEFAULT 0,
                failed_count INTEGER DEFAULT 0, error_code TEXT,
                lease_owner TEXT DEFAULT '', lease_until TEXT,
                version INTEGER DEFAULT 1, created_at TEXT, started_at TEXT,
                finished_at TEXT, updated_at TEXT
            );
        """)


class DailyRetryScenario:
    """只替换模型/平台边界，管理、队列和 SWE 历史使用真实服务。"""

    def __init__(self, db, clock):
        self.db = db
        self.clock = clock
        self.caller = CallerIdentity("alice", "bank", "default", "alice")
        self.management = ManagementService(
            db,
            ResourceClient(),
            ModePlatform(),
        )

    async def run(
        self,
        *,
        caller=None,
        job_id=None,
        kind="ordinary",
        status="error",
        detail="model rate limit",
        category="model_rate_limit",
    ):
        caller = caller or self.caller
        if job_id is None:
            created = await self.management.create(
                caller,
                create_payload(
                    uuid4().hex,
                    task_type="text",
                    text="original content",
                ),
            )
            job_id = created["job_id"]
        row, _ = await ExecutionStore(self.db).enqueue(
            caller,
            job_id,
            ExecutionRequest(
                kind,
                uuid4().hex,
                scheduled_at=self.clock().replace(tzinfo=timezone.utc),
                not_before_at=self.clock().replace(tzinfo=timezone.utc),
            ),
        )
        if status == "queued":
            return row
        return await self.finish(
            row,
            status=status,
            detail=detail,
            category=category,
        )

    async def finish(
        self,
        row,
        *,
        status="error",
        detail="model rate limit",
        category="model_rate_limit",
    ):
        from scheduler.app.services.cron.managed.repository import (
            IDENTITY_FIELDS,
        )

        await self.db.execute(
            "UPDATE swe_cron_execution_runs SET not_before_at=%s WHERE execution_id=%s",
            (self.clock() - timedelta(seconds=1), row["execution_id"]),
        )
        key = tuple(row[name] for name in IDENTITY_FIELDS)
        writer = ExecutionWriter(self.db)
        await writer.accept(row["execution_id"], key)
        assert await writer.start(row["execution_id"], key, "test-owner")
        await writer.complete(
            row["execution_id"],
            key,
            "test-owner",
            ExecutionOutcome(
                status,
                preview="report" if status == "success" else "",
                error_code=(
                    "cron_execution_failed" if status == "error" else None
                ),
                execution_meta=(
                    {"failure_type": category, "error_detail": detail}
                    if status == "error"
                    else {}
                ),
            ),
        )
        if status == "success":
            from scheduler.app.services.cron.managed.settlement import (
                ExecutionSettlement,
            )

            current = await writer.get(row["execution_id"], key)
            await self.db.execute(
                "UPDATE swe_cron_executions SET async_status='success' WHERE id=%s",
                (current["history_execution_id"],),
            )
            await ExecutionSettlement(self.db, clock=self.clock).tick()
        return await writer.get(row["execution_id"], key)

    async def batch(
        self,
        *,
        attempts=1,
        children=False,
        fail_all_children=False,
    ):
        """通过真实批回调冻结关系，并用原重试服务推进业务尝试。"""
        from dataclasses import replace
        from scheduler.app.services.cron.managed.batch_retry import (
            BatchRetryService,
        )
        from scheduler.app.services.cron.managed.scheduling import (
            ManagedSchedulingService,
        )
        from tests.unit.scheduler.test_managed_batch_retry import retry_body
        from tests.unit.scheduler.test_managed_cron_callbacks import body

        payload = create_payload(
            uuid4().hex,
            task_type="text",
            text="batch content",
        )
        payload["dispatch_state"] = "batch_enabled"
        created = await self.management.create(self.caller, payload)
        operation = created["operation_id"]
        if children:
            from scheduler.app.services.cron.managed.distribution import (
                BroadcastStore,
            )
            from scheduler.app.services.cron.managed.distribution_worker import (
                BroadcastProcessor,
            )
            from tests.unit.scheduler.test_managed_cron_distribution import (
                payload as targets,
            )

            distribution = targets()
            distribution["enable_offset"] = False
            broadcast = await BroadcastStore(self.db).submit(
                self.caller,
                created["job_id"],
                distribution,
            )
            await BroadcastProcessor(
                self.db,
                self.management.resources,
                self.management.platform,
            ).process(broadcast["task_id"])
            operation = broadcast["task_id"]
        command = body(self.db, created, kind="batch")
        command["operation_id"] = operation
        command["scheduled_fire_at"] = (
            self.clock().replace(tzinfo=timezone.utc).isoformat()
        )
        accepted = await ManagedSchedulingService(
            self.db,
            clock=lambda: self.clock().replace(tzinfo=timezone.utc),
        ).accept(self.caller, command, "batch")
        batch_id = accepted["batch_id"]
        members = await self.db.fetch_all(
            "SELECT * FROM swe_cron_managed_batch_members WHERE batch_id=%s ORDER BY dispatch_order",
            (batch_id,),
        )
        target = members[-1] if children else members[0]
        for member in members:
            row = await self.db.fetch_one(
                "SELECT * FROM swe_cron_execution_runs WHERE execution_id=%s",
                (member["current_execution_id"],),
            )
            await self.finish(
                row,
                status=(
                    "error"
                    if member["member_id"] == target["member_id"]
                    or (fail_all_children and member["member_role"] == "child")
                    else "success"
                ),
            )
        manager = replace(self.caller, is_manager=True)
        retry = BatchRetryService(self.db)
        for _ in range(attempts - 1):
            preview = await retry.preview(manager, batch_id)
            await retry.retry(
                manager,
                batch_id,
                retry_body(preview, request_id=uuid4().hex),
            )
            member = await self.db.fetch_one(
                "SELECT * FROM swe_cron_managed_batch_members WHERE batch_id=%s AND member_id=%s",
                (batch_id, target["member_id"]),
            )
            row = await self.db.fetch_one(
                "SELECT * FROM swe_cron_execution_runs WHERE execution_id=%s",
                (member["current_execution_id"],),
            )
            await self.finish(row)
        member = await self.db.fetch_one(
            "SELECT * FROM swe_cron_managed_batch_members WHERE batch_id=%s AND member_id=%s",
            (batch_id, target["member_id"]),
        )
        row = await self.db.fetch_one(
            "SELECT * FROM swe_cron_execution_runs WHERE execution_id=%s",
            (member["current_execution_id"],),
        )
        return created, batch_id, row
