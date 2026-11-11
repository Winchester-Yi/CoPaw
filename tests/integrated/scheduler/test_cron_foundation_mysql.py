# -*- coding: utf-8 -*-
"""在独立临时 MySQL 实例验证新表和日历租约，不连接已有业务数据库。"""

import asyncio
from datetime import date, datetime, timedelta, timezone
import os
from pathlib import Path
import shutil
import socket
import subprocess
import time
from uuid import uuid4

import pytest

aiomysql = pytest.importorskip("aiomysql")

from scheduler.app.services.cron.system.schema import (
    assert_system_schema_ready,
)
from scheduler.app.services.cron.workday_calendar import (
    CalendarService,
    CalendarStore,
)

pytestmark = pytest.mark.skipif(
    os.environ.get("CRON_TEST_PRIVATE_MYSQL") != "1",
    reason="只在显式允许时启动本机临时 MySQL，不连接目标环境",
)
ROOT = Path(__file__).resolve().parents[3]


@pytest.fixture(scope="module")
def private_mysql(tmp_path_factory):
    """启动仅监听回环地址的测试进程，结束时只终止该进程句柄。"""
    client = shutil.which("mysql")
    binary = (
        Path(client).with_name("mysqld.exe" if os.name == "nt" else "mysqld")
        if client
        else None
    )
    if binary is None or not binary.is_file():
        pytest.skip("本机没有可用于独立测试的 mysqld")
    root = tmp_path_factory.mktemp("cron_private_mysql")
    data = root / "data"
    initialized = subprocess.run(
        [
            str(binary),
            "--no-defaults",
            "--initialize-insecure",
            f"--basedir={binary.parent.parent}",
            f"--datadir={data}",
            "--console",
        ],
        capture_output=True,
        timeout=90,
        check=False,
    )
    assert initialized.returncode == 0, initialized.stderr.decode(
        errors="replace",
    )
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    with (
        (root / "server.log").open("wb") as log,
        subprocess.Popen(
            [
                str(binary),
                "--no-defaults",
                f"--basedir={binary.parent.parent}",
                f"--datadir={data}",
                "--bind-address=127.0.0.1",
                f"--port={port}",
                "--mysqlx=OFF",
                "--skip-log-bin",
                "--console",
            ],
            stdout=log,
            stderr=subprocess.STDOUT,
            creationflags=(
                subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
            ),
        ) as process,
    ):
        try:
            deadline = time.monotonic() + 45
            while time.monotonic() < deadline and process.poll() is None:
                try:
                    with socket.create_connection(
                        ("127.0.0.1", port),
                        timeout=0.2,
                    ):
                        break
                except OSError:
                    time.sleep(0.1)
            else:
                pytest.fail(
                    "临时 MySQL 未启动，日志：" + str(root / "server.log"),
                )
            yield {
                "host": "127.0.0.1",
                "port": port,
                "user": "root",
                "password": "",
                "charset": "utf8mb4",
                "autocommit": True,
            }
        finally:
            # Windows 的 mysqld 可能有守护子进程，先通过专用回环端口正常关闭。
            admin = binary.with_name(
                "mysqladmin.exe" if os.name == "nt" else "mysqladmin",
            )
            if process.poll() is None and admin.is_file():
                stopped = subprocess.run(
                    [
                        str(admin),
                        "--no-defaults",
                        "--protocol=tcp",
                        "--host=127.0.0.1",
                        f"--port={port}",
                        "--user=root",
                        "shutdown",
                    ],
                    capture_output=True,
                    timeout=20,
                    check=False,
                )
                if stopped.returncode == 0:
                    process.wait(timeout=20)
            if process.poll() is None:
                process.terminate()
                process.wait(timeout=20)


class TestDatabase:
    """每次操作从真实连接池领取连接，不伪造 MySQL 查询或锁行为。"""

    __test__ = False

    def __init__(self, pool):
        self.pool = pool

    @property
    def is_connected(self):
        """按真实测试连接池状态实现生产查询边界。"""
        return not self.pool.closed

    def acquire(self):
        """允许生产事务适配器使用真实连接池验证行锁竞争。"""
        return self.pool.acquire()

    async def execute(self, query, params=None):
        async with self.pool.acquire() as conn:
            async with conn.cursor() as cursor:
                await cursor.execute(query, params)
                return cursor.rowcount

    async def fetch_all(self, query, params=None):
        async with self.pool.acquire() as conn:
            async with conn.cursor(aiomysql.DictCursor) as cursor:
                await cursor.execute(query, params)
                return await cursor.fetchall()

    async def fetch_one(self, query, params=None):
        rows = await self.fetch_all(query, params)
        return rows[0] if rows else None


@pytest.fixture
async def mysql_db(private_mysql):
    """仅在刚初始化的专用实例创建随机数据库并执行人工 DDL。"""
    conn = await aiomysql.connect(**private_mysql)
    database = "cron_test_" + uuid4().hex
    async with conn.cursor() as cursor:
        await cursor.execute(
            f"CREATE DATABASE `{database}` CHARACTER SET utf8mb4 COLLATE utf8mb4_bin",
        )
    conn.close()
    pool = await aiomysql.create_pool(
        **private_mysql,
        db=database,
        minsize=2,
        maxsize=4,
    )
    db = TestDatabase(pool)
    try:
        for name in ("cron_workday_calendar.sql", "cron_system_tasks.sql"):
            sql = (ROOT / "scripts/sql" / name).read_text(encoding="utf-8")
            for statement in sql.split(";"):
                if statement.strip():
                    await db.execute(statement)
        yield db
    finally:
        pool.close()
        await pool.wait_closed()


@pytest.mark.asyncio
async def test_private_mysql_ddl_and_binary_system_identity(mysql_db):
    """真实执行三表 DDL，并验证来源级空身份唯一和大小写隔离。"""
    await assert_system_schema_ready(mysql_db)
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    insert = (
        "INSERT INTO swe_cron_system_jobs "
        "(job_id,scope_kind,source_id,tenant_id,agent_id,task_type,desired_config_json,operation_id,created_at,updated_at) "
        "VALUES (%s,'source',%s,'','','task_session_cleanup','{}',%s,%s,%s)"
    )
    await mysql_db.execute(
        insert,
        (str(uuid4()), "bank", str(uuid4()), now, now),
    )
    with pytest.raises(aiomysql.IntegrityError):
        await mysql_db.execute(
            insert,
            (str(uuid4()), "bank", str(uuid4()), now, now),
        )
    await mysql_db.execute(
        insert,
        (str(uuid4()), "BANK", str(uuid4()), now, now),
    )
    rows = await mysql_db.fetch_all(
        "SELECT source_id FROM swe_cron_system_jobs",
    )
    assert {row["source_id"] for row in rows} == {"bank", "BANK"}


@pytest.mark.asyncio
async def test_private_mysql_calendar_coalesces_connections_and_fences_old_writer(
    mysql_db,
):
    """多连接只刷新一次，过期旧令牌不能覆盖新租约结果。"""
    now = datetime(2026, 10, 20, tzinfo=timezone.utc)
    fetched = []

    async def fetch(day):
        fetched.append(day)
        await asyncio.sleep(0.03)
        return 1

    first = CalendarStore(mysql_db)
    second = CalendarStore(mysql_db)
    services = [
        CalendarService(store=store, fetcher=fetch, clock=lambda: now)
        for store in (first, second)
    ]
    day = date(2026, 10, 24)
    result = await asyncio.gather(
        *(service.get_day(day) for service in services),
    )
    assert fetched == [day]
    assert all(row["source"] == "api" and row["is_workday"] for row in result)
    assert result[0]["expires_at"] == now + timedelta(days=1)
    stale_day = date(2026, 10, 25)
    assert await first.claim(stale_day, "old", now)
    later = now + timedelta(seconds=11)
    assert await second.claim(stale_day, "new", later)
    value = {
        "is_workday": False,
        "source": "api",
        "expires_at": later + timedelta(days=1),
    }
    await first.save(stale_day, value, "old", later)
    assert await first.get(stale_day, later) is None
    await second.save(stale_day, value, "new", later)
    assert not (await first.get(stale_day, later))["is_workday"]


@pytest.mark.asyncio
async def test_private_mysql_concurrent_source_refresh_uses_one_stable_identity(
    mysql_db,
):
    """真实两连接同时创建来源任务，不能因缺行间隙锁而死锁或建两条。"""
    from scheduler.app.services.cron.managed.identity import CallerIdentity
    from scheduler.app.services.cron.system.management_store import (
        SystemManagementStore,
    )

    store = SystemManagementStore(mysql_db)
    first = CallerIdentity(
        "alice",
        "bank",
        "default",
        "alice",
        is_manager=True,
    )
    second = CallerIdentity("bob", "bank", "research", "bob", is_manager=True)
    results = await asyncio.gather(
        store.refresh(first, "cleanup", "first"),
        store.refresh(second, "cleanup", "second"),
    )
    assert results[0]["job_id"] == results[1]["job_id"]
    rows = await mysql_db.fetch_all("SELECT * FROM swe_cron_system_jobs")
    assert len(rows) == 1 and rows[0]["generation"] == 2


@pytest.fixture
async def swe_mysql_db(private_mysql, mysql_db):
    """第二套真实SWE生产连接池连同一专用库，验证跨服务事务而非SQLite近似。"""
    from swe.database.config import DatabaseConfig
    from swe.database.connection import DatabaseConnection

    name = await mysql_db.fetch_one("SELECT DATABASE() AS name")
    config = DatabaseConfig(
        host=private_mysql["host"],
        port=private_mysql["port"],
        user="root",
        password="",
        database=name["name"],
        min_connections=2,
        max_connections=4,
    )
    swe = DatabaseConnection(config)
    await swe.connect()
    try:
        yield swe
    finally:
        await swe.close()


@pytest.mark.asyncio
async def test_private_mysql_system_callbacks_and_swe_start_share_one_fence(
    mysql_db,
    swe_mysql_db,
):
    """真实多连接：两回调只占一个身份，两SWE竞争只开始一次；核实围栏拒绝迟到结果。"""
    from scheduler.app.services.cron.managed.identity import CallerIdentity
    from scheduler.app.services.cron.system.executions import (
        SystemExecutionStore,
    )
    from scheduler.app.services.cron.system.management_store import (
        SystemManagementStore,
    )
    from scheduler.app.services.cron.system.platform import PROTOCOL
    from swe.app.crons.system_results import SystemResultWriter

    now = [datetime.now(timezone.utc)]
    admin = CallerIdentity(
        "alice",
        "bank",
        "default",
        "alice",
        is_manager=True,
    )
    management = SystemManagementStore(mysql_db, clock=lambda: now[0])
    row = await management.refresh(admin, "heartbeat", "save")
    claimed = await management.claim(row["job_id"], "sync")
    config = {
        "enabled": True,
        "cron": "0 9 * * *",
        "timezone": "Asia/Shanghai",
        "timeout_seconds": 5,
    }
    await management.prepare(claimed, config)
    await management.finish(claimed, "123", config)
    job = await management.get(row["job_id"])
    body = {
        "job_id": row["job_id"],
        "task_type": "heartbeat",
        "callback_protocol": PROTOCOL,
        "operation_id": job["effective_operation_id"],
    }
    scheduler = SystemExecutionStore(mysql_db, clock=lambda: now[0])
    triggers = await asyncio.gather(
        scheduler.accept(admin, body),
        scheduler.accept(admin, body),
    )
    assert sorted(run["status"] for run in triggers) == ["pending", "skipped"]
    first = next(run for run in triggers if run["accepted"])
    run = await scheduler.claim_dispatch(first["execution_id"], "dispatch")
    writer = SystemResultWriter(swe_mysql_db, clock=lambda: now[0])
    await writer.accept(run["execution_id"], row["job_id"], "dispatch")
    starts = await asyncio.gather(
        writer.start(run["execution_id"], row["job_id"], "one", config),
        writer.start(run["execution_id"], row["job_id"], "two", config),
    )
    assert sum(start is not None for start in starts) == 1
    owner = next(start["run_owner"] for start in starts if start is not None)
    now[0] += timedelta(seconds=126)
    assert await scheduler.mark_unknown(run["execution_id"])
    unknown = await scheduler.get(run["execution_id"])
    assert (await scheduler.accept(admin, body))[
        "reason"
    ] == "system_result_unconfirmed"
    await scheduler.confirm_ended(
        admin,
        row["job_id"],
        run["execution_id"],
        {
            "expected_version": unknown["version"],
            "request_id": "confirm",
            "confirmed_ended": True,
            "reason": "已人工核实实际进程结束",
        },
    )
    newer = await scheduler.accept(admin, body)
    assert newer["accepted"]
    assert not await writer.finish(
        run["execution_id"],
        row["job_id"],
        owner,
        "success",
    )
    assert (await management.get(row["job_id"]))[
        "active_execution_id"
    ] == newer["execution_id"]


async def _user_query_tables(db):
    """仅在本测试新建的随机私有库补最小既有用户表，绝不执行目标环境DDL。"""
    from scheduler.app.services.cron.managed.external_query import STATE_FIELDS

    identity = ",".join(
        f"{key} VARCHAR(128) NOT NULL"
        for key in ("tenant_id", "source_id", "agent_id", "job_id")
    )
    await db.execute(
        f"CREATE TABLE swe_cron_jobs_v2 ({identity},scope_id VARCHAR(512),name TEXT,task_type VARCHAR(16),enabled TINYINT,cron_expr VARCHAR(255),timezone VARCHAR(64),spec_json JSON,creator_user_id VARCHAR(128),management_owner VARCHAR(32),deleted_at DATETIME,PRIMARY KEY(tenant_id,source_id,agent_id,job_id)) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin",
    )
    dates = {
        "auto_paused_at",
        "last_run_at",
        "last_prefetch_at",
        "last_scheduled_run_at",
    }
    numeric = {"has_scheduled_result", "unread_execution_count"}
    fields = ",".join(
        f"{key} {'DATETIME' if key in dates else 'BIGINT' if key in numeric else 'TEXT'} NULL"
        for key in STATE_FIELDS
    )
    await db.execute(
        f"CREATE TABLE swe_cron_job_states ({identity},{fields},state_version BIGINT NOT NULL DEFAULT 1,updated_at DATETIME,PRIMARY KEY(tenant_id,source_id,agent_id,job_id)) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin",
    )
    spec = '{"task_type":"text","text":"demo","schedule":{"cron":"0 9 * * *","timezone":"Asia/Shanghai"},"dispatch":{"target":{"user_id":"alice","session_id":"s1"}}}'
    await db.execute(
        "INSERT INTO swe_cron_jobs_v2 VALUES ('alice','bank','default','job-1','scope','demo','text',1,'0 9 * * *','Asia/Shanghai',%s,'alice','scheduler',NULL)",
        (spec,),
    )
    await db.execute(
        "INSERT INTO swe_cron_job_states (tenant_id,source_id,agent_id,job_id,task_session_id,has_scheduled_result,latest_scheduled_preview,last_scheduled_run_at,unread_execution_count) VALUES ('alice','bank','default','job-1','s1',1,'old','2026-08-01',8)",
    )


@pytest.mark.asyncio
async def test_private_mysql_external_query_only_writes_calendar_cache(
    mysql_db,
    swe_mysql_db,
    monkeypatch,
):
    from fastapi import FastAPI
    from httpx import ASGITransport, AsyncClient
    from scheduler.app.routers import external_cron
    from scheduler.app.database import connection
    from scheduler.app.services.cron import workday_calendar

    await _user_query_tables(mysql_db)
    now = datetime(2026, 10, 20, tzinfo=timezone.utc)

    async def fetch(_):
        return 1

    service = CalendarService(
        store=CalendarStore(mysql_db),
        fetcher=fetch,
        clock=lambda: now,
    )
    monkeypatch.setattr(
        workday_calendar,
        "_calendar_service",
        service,
    )
    monkeypatch.setattr(connection, "get_db_connection", lambda: mysql_db)
    app = FastAPI()
    app.include_router(external_cron.router, prefix="/api")
    before_jobs = await mysql_db.fetch_all("SELECT * FROM swe_cron_jobs_v2")
    before_states = await mysql_db.fetch_all(
        "SELECT * FROM swe_cron_job_states",
    )
    async with AsyncClient(
        transport=ASGITransport(app),
        base_url="http://scheduler",
    ) as http:

        response = await http.get(
            "/api/scheduler/external/cron/jobs",
            headers={"X-Tenant-Id": "alice", "X-Source-Id": "bank"},
        )
        assert response.status_code == 200
        result = response.json()
    assert len(result) == 1 and len(result[0]["state"]["next_run_times"]) == 3
    assert await mysql_db.fetch_all("SELECT * FROM swe_workday_calendar")
    assert (
        await mysql_db.fetch_all("SELECT * FROM swe_cron_jobs_v2")
        == before_jobs
    )
    assert (
        await mysql_db.fetch_all("SELECT * FROM swe_cron_job_states")
        == before_states
    )


@pytest.mark.asyncio
async def test_private_mysql_session_cleanup_cas_preserves_newer_results(
    mysql_db,
    swe_mysql_db,
):
    from types import SimpleNamespace
    from swe.app.crons.managed_session_cleanup import ManagedTaskSessionCleaner

    await _user_query_tables(mysql_db)
    row = await mysql_db.fetch_one("SELECT * FROM swe_cron_job_states")
    cleaner = ManagedTaskSessionCleaner(
        swe_mysql_db,
        clock=lambda: datetime(2026, 10, 20, tzinfo=timezone.utc),
    )
    cutoff = datetime(2026, 9, 20, tzinfo=timezone.utc)
    await cleaner._clear_expired_state(
        row,
        SimpleNamespace(has_result=False),
        cutoff,
    )
    cleared = await mysql_db.fetch_one("SELECT * FROM swe_cron_job_states")
    assert (
        cleared["unread_execution_count"] == 0
        and cleared["state_version"] == 2
    )
    await mysql_db.execute(
        "UPDATE swe_cron_job_states SET state_version=3,has_scheduled_result=1,unread_execution_count=9,last_scheduled_run_at='2026-10-19'",
    )
    await cleaner._clear_expired_state(
        row,
        SimpleNamespace(has_result=False),
        cutoff,
    )
    newer = await mysql_db.fetch_one("SELECT * FROM swe_cron_job_states")
    assert newer["state_version"] == 3 and newer["unread_execution_count"] == 9
