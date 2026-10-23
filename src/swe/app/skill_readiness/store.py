# -*- coding: utf-8 -*-
"""技能就绪检查数据库存储。"""

from __future__ import annotations

import json
import logging
import hashlib
import uuid
from collections import defaultdict
from datetime import datetime
from typing import Any

from .models import (
    SkillReadinessCheckResult,
    SkillReadinessCheckSummary,
    SkillReadinessConfig,
    SkillReadinessConfigRecord,
    SkillReadinessOwner,
    SkillReadinessOwnerSnapshot,
    SkillReadinessRunProgress,
    SkillReadinessUserResult,
)

logger = logging.getLogger(__name__)

_UNAVAILABLE_PREFIX = "skill readiness storage unavailable"
_CONFIG_TABLE = "swe_skill_readiness_configs"
_RUN_TABLE = "swe_skill_readiness_runs"
_OWNER_SNAPSHOT_TABLE = "swe_skill_readiness_owner_snapshots"
_USER_RESULT_TABLE = "swe_skill_readiness_user_results"
_CHECK_RESULT_TABLE = "swe_skill_readiness_check_results"


class SkillReadinessStoreUnavailable(RuntimeError):
    """技能就绪检查存储不可用。"""


class SkillReadinessStore:
    """读写技能就绪检查配置、运行进度和结果。"""

    def __init__(self, db: Any | None = None):
        """初始化存储。"""
        self.db = db

    @property
    def is_available(self) -> bool:
        """返回当前数据库连接是否可用。"""
        return self.db is not None and bool(
            getattr(self.db, "is_connected", False),
        )

    def _require_db(self) -> Any:
        """校验数据库可用并返回连接对象。"""
        if not self.is_available:
            raise SkillReadinessStoreUnavailable(
                f"{_UNAVAILABLE_PREFIX}: db is not connected",
            )
        return self.db

    async def _call_db(
        self,
        operation: str,
        db_call: Any,
        *args: Any,
    ) -> Any:
        """执行数据库调用并统一包装底层异常。"""
        try:
            return await db_call(*args)
        except SkillReadinessStoreUnavailable:
            raise
        except Exception as exc:
            raise SkillReadinessStoreUnavailable(
                f"{_UNAVAILABLE_PREFIX}: {operation} failed: {exc}",
            ) from exc

    async def _execute_on_conn(
        self,
        conn: Any,
        query: str,
        params: tuple[Any, ...] | None = None,
    ) -> int:
        """在指定连接上执行 SQL，用于事务或命名锁保护的临界区。"""
        return await conn.execute(query, params)

    async def initialize(self) -> None:
        """幂等初始化技能就绪检查相关表和索引。"""
        db = self._require_db()
        for query in _CREATE_TABLE_QUERIES:
            await self._call_db("initialize tables", db.execute, query)

        for index in _INDEX_DEFINITIONS:
            await self._create_index_if_missing(db, *index)

    async def get_config(
        self,
        skill_id: str,
    ) -> SkillReadinessConfigRecord | None:
        """按 skill_id 查询就绪检查配置。"""
        db = self._require_db()
        row = await self._call_db(
            "fetch config",
            db.fetch_one,
            f"""
                SELECT skill_id, config_json, updated_at
                FROM {_CONFIG_TABLE}
                WHERE skill_id = %s
            """,
            (skill_id,),
        )
        if row is None:
            return None
        return self._row_to_config_record(row)

    async def get_owner_snapshot(
        self,
        source_id: str,
        skill_id: str,
    ) -> SkillReadinessOwnerSnapshot | None:
        """读取某个 source/skill 的最近一次 owner 查询快照。"""
        db = self._require_db()
        row = await self._call_db(
            "fetch owner snapshot",
            db.fetch_one,
            f"""
                SELECT source_id, skill_id, status, total_users, owner_users,
                    failed_users, failure_summary, owners_json, updated_at
                FROM {_OWNER_SNAPSHOT_TABLE}
                WHERE source_id = %s AND skill_id = %s
            """,
            (source_id, skill_id),
        )
        return None if row is None else self._row_to_owner_snapshot(row)

    async def mark_owner_lookup_running(
        self,
        source_id: str,
        skill_id: str,
    ) -> bool:
        """抢占 owner 刷新权；已有 running 刷新时返回 False。"""
        db = self._require_db()
        affected = await self._call_db(
            "mark owner lookup running",
            db.execute,
            f"""
                INSERT INTO {_OWNER_SNAPSHOT_TABLE} (
                    source_id, skill_id, status, owners_json
                )
                VALUES (%s, %s, 'running', '[]')
                ON DUPLICATE KEY UPDATE
                    status = 'running',
                    updated_at = updated_at
            """,
            (source_id, skill_id),
        )
        return int(affected or 0) > 0

    async def record_owner_snapshot(
        self,
        source_id: str,
        skill_id: str,
        *,
        status: str,
        total_users: int,
        owners: list[SkillReadinessOwner],
        failed_users: int = 0,
        failure_summary: str | None = None,
    ) -> None:
        """保存 owner 查询结果，供 overview 直接读取最新快照。"""
        db = self._require_db()
        await self._call_db(
            "record owner snapshot",
            db.execute,
            f"""
                INSERT INTO {_OWNER_SNAPSHOT_TABLE} (
                    source_id, skill_id, status, total_users, owner_users,
                    failed_users, failure_summary, owners_json
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                ON DUPLICATE KEY UPDATE
                    status = VALUES(status),
                    total_users = VALUES(total_users),
                    owner_users = VALUES(owner_users),
                    failed_users = VALUES(failed_users),
                    failure_summary = VALUES(failure_summary),
                    owners_json = VALUES(owners_json),
                    updated_at = CURRENT_TIMESTAMP
            """,
            (
                source_id,
                skill_id,
                status,
                max(total_users, 0),
                len(owners),
                max(failed_users, 0),
                failure_summary,
                _dump_json(
                    [owner.model_dump(mode="json") for owner in owners],
                ),
            ),
        )

    async def create_run(
        self,
        source_id: str,
        skill_id: str,
        config_snapshot: SkillReadinessConfig,
    ) -> SkillReadinessRunProgress:
        """创建新的 running 运行记录。"""
        db = self._require_db()
        run_id = uuid.uuid4().hex
        await self._call_db(
            "create run",
            db.execute,
            f"""
                INSERT INTO {_RUN_TABLE} (
                    run_id, source_id, skill_id, status, total_users,
                    completed_users, failed_users, config_snapshot,
                    failure_summary, started_at
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, CURRENT_TIMESTAMP)
            """,
            (
                run_id,
                source_id,
                skill_id,
                "running",
                0,
                0,
                0,
                _dump_json(config_snapshot.model_dump(mode="json")),
                None,
            ),
        )
        run = await self._get_run(run_id)
        if run is None:
            raise ValueError(
                f"skill readiness run not found after create: {run_id}",
            )
        return run

    async def get_running_run(
        self,
        source_id: str,
        skill_id: str,
    ) -> SkillReadinessRunProgress | None:
        """查询指定 source/skill 最新的 running 运行。"""
        db = self._require_db()
        row = await self._call_db(
            "fetch running run",
            db.fetch_one,
            f"""
                SELECT run_id, source_id, skill_id, status, total_users,
                    completed_users, failed_users, failure_summary, created_at,
                    started_at, completed_at, updated_at
                FROM {_RUN_TABLE}
                WHERE source_id = %s
                    AND skill_id = %s
                    AND status = 'running'
                ORDER BY created_at DESC
                LIMIT 1
            """,
            (source_id, skill_id),
        )
        return None if row is None else self._row_to_run_progress(row)

    async def get_or_create_running_run(
        self,
        source_id: str,
        skill_id: str,
        config_snapshot: SkillReadinessConfig,
    ) -> tuple[SkillReadinessRunProgress, bool]:
        """复用正在运行的 run，不存在时创建新 run。"""
        db = self._require_db()
        lock_name = _run_lock_name(source_id, skill_id)
        try:
            async with db.named_lock(lock_name, timeout=10, exact_name=True):
                running_run = await self.get_running_run(source_id, skill_id)
                if running_run is not None:
                    return running_run, True

                run = await self.create_run(
                    source_id,
                    skill_id,
                    config_snapshot,
                )
                return run, False
        except SkillReadinessStoreUnavailable:
            raise
        except Exception as exc:
            raise SkillReadinessStoreUnavailable(
                f"{_UNAVAILABLE_PREFIX}: get or create run failed: {exc}",
            ) from exc

    async def get_latest_run(
        self,
        source_id: str,
        skill_id: str,
    ) -> SkillReadinessRunProgress | None:
        """查询指定 source/skill 最新的一次运行。"""
        db = self._require_db()
        row = await self._call_db(
            "fetch latest run",
            db.fetch_one,
            f"""
                SELECT run_id, source_id, skill_id, status, total_users,
                    completed_users, failed_users, failure_summary, created_at,
                    started_at, completed_at, updated_at
                FROM {_RUN_TABLE}
                WHERE source_id = %s
                    AND skill_id = %s
                ORDER BY created_at DESC
                LIMIT 1
            """,
            (source_id, skill_id),
        )
        return None if row is None else self._row_to_run_progress(row)

    async def get_run(
        self,
        run_id: str,
    ) -> SkillReadinessRunProgress | None:
        """按 run_id 查询运行进度。"""
        return await self._get_run(run_id)

    async def update_run_progress(
        self,
        run_id: str,
        total_users: int | None = None,
        completed_delta: int = 0,
        failed_delta: int = 0,
        status: str | None = None,
        failure_summary: str | None = None,
        completed_at: datetime | None = None,
    ) -> SkillReadinessRunProgress:
        """更新运行进度并返回最新进度。"""
        db = self._require_db()
        assignments: list[str] = []
        params: list[Any] = []
        if total_users is not None:
            assignments.append("total_users = %s")
            params.append(total_users)
        if completed_delta:
            assignments.append("completed_users = completed_users + %s")
            params.append(completed_delta)
        if failed_delta:
            assignments.append("failed_users = failed_users + %s")
            params.append(failed_delta)
        if status is not None:
            assignments.append("status = %s")
            params.append(status)
        if failure_summary is not None:
            assignments.append("failure_summary = %s")
            params.append(failure_summary)
        if completed_at is not None:
            assignments.append("completed_at = %s")
            params.append(completed_at)

        assignments.append("updated_at = CURRENT_TIMESTAMP")
        params.append(run_id)
        await self._call_db(
            "update run progress",
            db.execute,
            f"""
                UPDATE {_RUN_TABLE}
                SET {", ".join(assignments)}
                WHERE run_id = %s
            """,
            tuple(params),
        )
        run = await self._get_run(run_id)
        if run is None:
            raise ValueError(
                f"skill readiness run not found after update: {run_id}",
            )
        return run

    async def _update_run_progress_on_conn(
        self,
        conn: Any,
        run_id: str,
        total_users: int | None = None,
        completed_delta: int = 0,
        failed_delta: int = 0,
        status: str | None = None,
        failure_summary: str | None = None,
        completed_at: datetime | None = None,
    ) -> None:
        """在当前事务连接上更新运行进度。"""
        assignments: list[str] = []
        params: list[Any] = []
        if total_users is not None:
            assignments.append("total_users = %s")
            params.append(total_users)
        if completed_delta:
            assignments.append("completed_users = completed_users + %s")
            params.append(completed_delta)
        if failed_delta:
            assignments.append("failed_users = failed_users + %s")
            params.append(failed_delta)
        if status is not None:
            assignments.append("status = %s")
            params.append(status)
        if failure_summary is not None:
            assignments.append("failure_summary = %s")
            params.append(failure_summary)
        if completed_at is not None:
            assignments.append("completed_at = %s")
            params.append(completed_at)

        assignments.append("updated_at = CURRENT_TIMESTAMP")
        params.append(run_id)
        await self._execute_on_conn(
            conn,
            f"""
                UPDATE {_RUN_TABLE}
                SET {", ".join(assignments)}
                WHERE run_id = %s
            """,
            tuple(params),
        )

    async def record_user_result(
        self,
        run_id: str,
        user_result: SkillReadinessUserResult,
    ) -> SkillReadinessRunProgress:
        """写入用户聚合结果并替换该用户的检查明细。"""
        db = self._require_db()
        try:
            async with db.transaction() as conn:
                await self._execute_on_conn(
                    conn,
                    f"""
                        INSERT INTO {_USER_RESULT_TABLE} (
                            run_id, user_id, user_name, bbk_id,
                            aggregate_status, summary, duration_ms
                        )
                        VALUES (%s, %s, %s, %s, %s, %s, %s)
                        ON DUPLICATE KEY UPDATE
                            user_name = VALUES(user_name),
                            bbk_id = VALUES(bbk_id),
                            aggregate_status = VALUES(aggregate_status),
                            summary = VALUES(summary),
                            duration_ms = VALUES(duration_ms),
                            updated_at = CURRENT_TIMESTAMP
                    """,
                    (
                        run_id,
                        user_result.user_id,
                        user_result.user_name,
                        user_result.bbk_id,
                        user_result.aggregate_status,
                        user_result.summary,
                        user_result.duration_ms,
                    ),
                )
                await self._execute_on_conn(
                    conn,
                    f"""
                        DELETE FROM {_CHECK_RESULT_TABLE}
                        WHERE run_id = %s AND user_id = %s
                    """,
                    (run_id, user_result.user_id),
                )
                for check in user_result.checks:
                    await self._insert_check_result_on_conn(
                        conn,
                        run_id,
                        user_result.user_id,
                        check,
                    )
                await self._update_run_progress_on_conn(
                    conn,
                    run_id,
                    completed_delta=1,
                    failed_delta=(
                        1 if user_result.aggregate_status == "abnormal" else 0
                    ),
                )
        except SkillReadinessStoreUnavailable:
            raise
        except Exception as exc:
            raise SkillReadinessStoreUnavailable(
                f"{_UNAVAILABLE_PREFIX}: record user result failed: {exc}",
            ) from exc

        run = await self._get_run(run_id)
        if run is None:
            raise ValueError(
                f"skill readiness run not found after update: {run_id}",
            )
        return run

    async def get_check_summaries(
        self,
        run_id: str,
    ) -> list[SkillReadinessCheckSummary]:
        """按 check 维度汇总一次运行的结果。"""
        db = self._require_db()
        rows = await self._call_db(
            "fetch check summaries",
            db.fetch_all,
            f"""
                SELECT check_name, display_name, COUNT(*) AS total,
                    SUM(CASE WHEN status = 'pass' THEN 1 ELSE 0 END) AS pass_count,
                    SUM(CASE WHEN status = 'fail' THEN 1 ELSE 0 END) AS fail_count,
                    SUM(CASE WHEN status = 'skip' THEN 1 ELSE 0 END) AS skip_count
                FROM {_CHECK_RESULT_TABLE}
                WHERE run_id = %s
                GROUP BY check_name, display_name
                ORDER BY check_name ASC
            """,
            (run_id,),
        )
        return [
            SkillReadinessCheckSummary(
                check_name=row["check_name"],
                display_name=row["display_name"],
                total=int(row.get("total") or 0),
                pass_count=int(row.get("pass_count") or 0),
                fail_count=int(row.get("fail_count") or 0),
                skip_count=int(row.get("skip_count") or 0),
            )
            for row in rows
        ]

    async def list_user_results(
        self,
        run_id: str,
        page: int = 1,
        page_size: int = 20,
        status: str = "all",
        check_name: str | None = None,
        check_status: str | None = None,
    ) -> tuple[list[SkillReadinessUserResult], int]:
        """分页查询用户结果，支持按聚合状态或 check 明细过滤。"""
        db = self._require_db()
        page = max(page, 1)
        page_size = min(max(page_size, 1), 100)
        where_sql, params = self._build_user_result_filters(
            run_id,
            status,
            check_name,
            check_status,
        )
        count_row = await self._call_db(
            "count user results",
            db.fetch_one,
            f"""
                SELECT COUNT(*) AS total
                FROM {_USER_RESULT_TABLE} ur
                WHERE {where_sql}
            """,
            tuple(params),
        )
        total = int((count_row or {}).get("total") or 0)
        if total == 0:
            return [], 0

        offset = (page - 1) * page_size
        user_rows = await self._call_db(
            "list user results",
            db.fetch_all,
            f"""
                SELECT run_id, user_id, user_name, bbk_id, aggregate_status,
                    summary, duration_ms, created_at, updated_at
                FROM {_USER_RESULT_TABLE} ur
                WHERE {where_sql}
                ORDER BY
                    CASE WHEN aggregate_status = 'abnormal' THEN 0 ELSE 1 END,
                    updated_at DESC,
                    user_id ASC
                LIMIT %s OFFSET %s
            """,
            tuple(params + [page_size, offset]),
        )
        if not user_rows:
            return [], total

        user_ids = [row["user_id"] for row in user_rows]
        checks_by_user = await self._fetch_checks_for_users(
            db,
            run_id,
            user_ids,
        )
        return [
            self._row_to_user_result(
                row,
                checks_by_user.get(row["user_id"], []),
            )
            for row in user_rows
        ], total

    async def _get_run(self, run_id: str) -> SkillReadinessRunProgress | None:
        """按 run_id 查询运行进度。"""
        db = self._require_db()
        row = await self._call_db(
            "fetch run",
            db.fetch_one,
            f"""
                SELECT run_id, source_id, skill_id, status, total_users,
                    completed_users, failed_users, failure_summary, created_at,
                    started_at, completed_at, updated_at
                FROM {_RUN_TABLE}
                WHERE run_id = %s
            """,
            (run_id,),
        )
        return None if row is None else self._row_to_run_progress(row)

    async def _insert_check_result(
        self,
        db: Any,
        run_id: str,
        user_id: str,
        check: SkillReadinessCheckResult,
    ) -> None:
        """写入单条 check 明细。"""
        await self._call_db(
            "insert check result",
            db.execute,
            f"""
                INSERT INTO {_CHECK_RESULT_TABLE} (
                    run_id, user_id, check_name, display_name, status,
                    message, details_json, duration_ms
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                run_id,
                user_id,
                check.check_name,
                check.display_name,
                check.status,
                check.message,
                _dump_json(check.details),
                check.duration_ms,
            ),
        )

    async def _insert_check_result_on_conn(
        self,
        conn: Any,
        run_id: str,
        user_id: str,
        check: SkillReadinessCheckResult,
    ) -> None:
        """在当前事务连接上写入单条 check 明细。"""
        await self._execute_on_conn(
            conn,
            f"""
                INSERT INTO {_CHECK_RESULT_TABLE} (
                    run_id, user_id, check_name, display_name, status,
                    message, details_json, duration_ms
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                run_id,
                user_id,
                check.check_name,
                check.display_name,
                check.status,
                check.message,
                _dump_json(check.details),
                check.duration_ms,
            ),
        )

    async def _fetch_checks_for_users(
        self,
        db: Any,
        run_id: str,
        user_ids: list[str],
    ) -> dict[str, list[SkillReadinessCheckResult]]:
        """批量读取分页用户的全部 check 明细。"""
        placeholders = ", ".join(["%s"] * len(user_ids))
        rows = await self._call_db(
            "fetch user checks",
            db.fetch_all,
            f"""
                SELECT user_id, check_name, display_name, status, message,
                    details_json, duration_ms
                FROM {_CHECK_RESULT_TABLE}
                WHERE run_id = %s
                    AND user_id IN ({placeholders})
                ORDER BY user_id ASC, id ASC
            """,
            tuple([run_id] + user_ids),
        )
        checks_by_user: dict[str, list[SkillReadinessCheckResult]] = (
            defaultdict(list)
        )
        for row in rows:
            checks_by_user[row["user_id"]].append(
                self._row_to_check_result(row),
            )
        return checks_by_user

    async def _create_index_if_missing(
        self,
        db: Any,
        index_name: str,
        table_name: str,
        columns_sql: str,
    ) -> None:
        """MySQL 不统一支持 CREATE INDEX IF NOT EXISTS，先查后建。"""
        row = await self._call_db(
            "probe index",
            db.fetch_one,
            """
                SELECT COUNT(1) AS total
                FROM information_schema.statistics
                WHERE table_schema = DATABASE()
                    AND table_name = %s
                    AND index_name = %s
            """,
            (table_name, index_name),
        )
        if int((row or {}).get("total") or 0) > 0:
            return
        try:
            await self._call_db(
                "create index",
                db.execute,
                f"CREATE INDEX {index_name} ON {table_name} ({columns_sql})",
            )
        except SkillReadinessStoreUnavailable as exc:
            if _is_duplicate_index_error(exc):
                return
            raise

    def _build_user_result_filters(
        self,
        run_id: str,
        status: str,
        check_name: str | None,
        check_status: str | None,
    ) -> tuple[str, list[Any]]:
        """构造用户结果查询过滤条件。"""
        filters = ["ur.run_id = %s"]
        params: list[Any] = [run_id]
        if status != "all":
            filters.append("ur.aggregate_status = %s")
            params.append(status)
        if check_name is not None or check_status is not None:
            check_filters = [
                "cr.run_id = ur.run_id",
                "cr.user_id = ur.user_id",
            ]
            if check_name is not None:
                check_filters.append("cr.check_name = %s")
                params.append(check_name)
            if check_status is not None:
                check_filters.append("cr.status = %s")
                params.append(check_status)
            filters.append(
                "EXISTS ("
                f"SELECT 1 FROM {_CHECK_RESULT_TABLE} cr "
                f"WHERE {' AND '.join(check_filters)}"
                ")",
            )
        return " AND ".join(filters), params

    def _row_to_config_record(
        self,
        row: dict[str, Any],
    ) -> SkillReadinessConfigRecord:
        """将数据库行解析为配置记录。"""
        try:
            raw_config = row.get("config_json", row.get("config_text"))
            config_data = (
                json.loads(raw_config)
                if isinstance(raw_config, str)
                else raw_config
            )
            config = SkillReadinessConfig.model_validate(config_data)
        except (TypeError, ValueError) as exc:
            raise ValueError(
                f"invalid skill readiness config for {row.get('skill_id')}: {exc}",
            ) from exc

        return SkillReadinessConfigRecord(
            skill_id=row["skill_id"],
            config=config,
            updated_at=row.get("updated_at"),
        )

    def _row_to_owner_snapshot(
        self,
        row: dict[str, Any],
    ) -> SkillReadinessOwnerSnapshot:
        """将 owner 快照行解析为领域模型。"""
        raw_owners = row.get("owners_json")
        try:
            owner_data = (
                json.loads(raw_owners)
                if isinstance(raw_owners, str)
                else raw_owners
            ) or []
        except (TypeError, ValueError):
            owner_data = []
        owners = [
            SkillReadinessOwner.model_validate(item)
            for item in owner_data
            if isinstance(item, dict)
        ]
        return SkillReadinessOwnerSnapshot(
            source_id=row["source_id"],
            skill_id=row["skill_id"],
            status=row.get("status") or "idle",
            total_users=int(row.get("total_users") or 0),
            owner_users=int(row.get("owner_users") or len(owners)),
            failed_users=int(row.get("failed_users") or 0),
            failure_summary=row.get("failure_summary"),
            owners=owners,
            updated_at=row.get("updated_at"),
        )

    def _row_to_run_progress(
        self,
        row: dict[str, Any],
    ) -> SkillReadinessRunProgress:
        """将数据库行解析为运行进度。"""
        return SkillReadinessRunProgress(
            run_id=row["run_id"],
            source_id=row["source_id"],
            skill_id=row["skill_id"],
            status=row["status"],
            total_users=int(row.get("total_users") or 0),
            completed_users=int(row.get("completed_users") or 0),
            failed_users=int(row.get("failed_users") or 0),
            failure_summary=row.get("failure_summary"),
            created_at=row.get("created_at"),
            started_at=row.get("started_at"),
            completed_at=row.get("completed_at"),
            updated_at=row.get("updated_at"),
        )

    def _row_to_user_result(
        self,
        row: dict[str, Any],
        checks: list[SkillReadinessCheckResult],
    ) -> SkillReadinessUserResult:
        """将用户行和 check 明细合成为用户结果。"""
        return SkillReadinessUserResult(
            user_id=row["user_id"],
            user_name=row.get("user_name"),
            bbk_id=row.get("bbk_id"),
            aggregate_status=row["aggregate_status"],
            summary=row.get("summary") or "",
            duration_ms=int(row.get("duration_ms") or 0),
            checks=checks,
        )

    def _row_to_check_result(
        self,
        row: dict[str, Any],
    ) -> SkillReadinessCheckResult:
        """将数据库行解析为单项 check 结果。"""
        raw_details = row.get("details_json")
        try:
            details = (
                json.loads(raw_details)
                if isinstance(raw_details, str)
                else raw_details
            ) or {}
        except (TypeError, ValueError):
            details = {"raw": raw_details}
        return SkillReadinessCheckResult(
            check_name=row["check_name"],
            display_name=row["display_name"],
            status=row["status"],
            message=row.get("message") or "",
            details=details,
            duration_ms=int(row.get("duration_ms") or 0),
        )


def _dump_json(value: Any) -> str:
    """序列化 JSON 字段，保持数据库中可读且稳定。"""
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _run_lock_name(source_id: str, skill_id: str) -> str:
    """生成不超过 MySQL GET_LOCK 限制的稳定锁名。"""
    digest = hashlib.sha256(
        f"{source_id}:{skill_id}".encode("utf-8"),
    ).hexdigest()
    return f"swe_skill_ready:{digest[:48]}"


def _is_duplicate_index_error(exc: BaseException) -> bool:
    """识别多实例并发建索引时的重复索引错误。"""
    current: BaseException | None = exc
    while current is not None:
        args = getattr(current, "args", ())
        if args and args[0] == 1061:
            return True
        message = str(current).lower()
        if "duplicate key name" in message or "already exists" in message:
            return True
        current = current.__cause__
    return False


_CREATE_TABLE_QUERIES = (
    f"""
        CREATE TABLE IF NOT EXISTS {_CONFIG_TABLE} (
            skill_id VARCHAR(200) PRIMARY KEY,
            config_json TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                ON UPDATE CURRENT_TIMESTAMP
        )
    """,
    f"""
        CREATE TABLE IF NOT EXISTS {_RUN_TABLE} (
            run_id VARCHAR(64) PRIMARY KEY,
            source_id VARCHAR(128) NOT NULL,
            skill_id VARCHAR(200) NOT NULL,
            status VARCHAR(20) NOT NULL,
            total_users INT NOT NULL DEFAULT 0,
            completed_users INT NOT NULL DEFAULT 0,
            failed_users INT NOT NULL DEFAULT 0,
            config_snapshot TEXT NOT NULL,
            failure_summary TEXT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            started_at TIMESTAMP NULL DEFAULT NULL,
            completed_at TIMESTAMP NULL DEFAULT NULL,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                ON UPDATE CURRENT_TIMESTAMP
        )
    """,
    f"""
        CREATE TABLE IF NOT EXISTS {_OWNER_SNAPSHOT_TABLE} (
            source_id VARCHAR(128) NOT NULL,
            skill_id VARCHAR(200) NOT NULL,
            status VARCHAR(20) NOT NULL,
            total_users INT NOT NULL DEFAULT 0,
            owner_users INT NOT NULL DEFAULT 0,
            failed_users INT NOT NULL DEFAULT 0,
            failure_summary TEXT NULL,
            owners_json MEDIUMTEXT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                ON UPDATE CURRENT_TIMESTAMP,
            PRIMARY KEY (source_id, skill_id)
        )
    """,
    f"""
        CREATE TABLE IF NOT EXISTS {_USER_RESULT_TABLE} (
            run_id VARCHAR(64) NOT NULL,
            user_id VARCHAR(128) NOT NULL,
            user_name VARCHAR(200) NULL,
            bbk_id VARCHAR(128) NULL,
            aggregate_status VARCHAR(20) NOT NULL,
            summary TEXT NULL,
            duration_ms INT NOT NULL DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                ON UPDATE CURRENT_TIMESTAMP,
            PRIMARY KEY (run_id, user_id)
        )
    """,
    f"""
        CREATE TABLE IF NOT EXISTS {_CHECK_RESULT_TABLE} (
            id BIGINT AUTO_INCREMENT PRIMARY KEY,
            run_id VARCHAR(64) NOT NULL,
            user_id VARCHAR(128) NOT NULL,
            check_name VARCHAR(128) NOT NULL,
            display_name VARCHAR(200) NOT NULL,
            status VARCHAR(20) NOT NULL,
            message TEXT NULL,
            details_json TEXT NULL,
            duration_ms INT NOT NULL DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """,
)

_INDEX_DEFINITIONS = (
    (
        "idx_skill_readiness_runs_state",
        _RUN_TABLE,
        "source_id, skill_id, status",
    ),
    (
        "idx_skill_readiness_runs_created",
        _RUN_TABLE,
        "source_id, skill_id, created_at",
    ),
    (
        "idx_skill_readiness_user_status",
        _USER_RESULT_TABLE,
        "run_id, aggregate_status",
    ),
    (
        "idx_skill_readiness_check_lookup",
        _CHECK_RESULT_TABLE,
        "run_id, check_name, status, user_id",
    ),
    (
        "idx_skill_readiness_check_user",
        _CHECK_RESULT_TABLE,
        "run_id, user_id, id",
    ),
)
