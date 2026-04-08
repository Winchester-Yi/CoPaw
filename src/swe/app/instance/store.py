# -*- coding: utf-8 -*-
"""Instance management database store."""

import json
import logging
from typing import Any, Optional, Union

from .models import (
    Instance,
    InstanceStatus,
    InstanceWithUsage,
    OperationLog,
    Source,
    SourceWithStats,
    UserAllocation,
    UserAllocationStatus,
)

logger = logging.getLogger(__name__)


def calculate_warning_level(current: int, max_val: int) -> str:
    """Calculate warning level based on usage percentage."""
    if max_val <= 0:
        return "critical"
    percent = (current / max_val) * 100
    if percent >= 100:
        return "critical"
    if percent >= 80:
        return "warning"
    return "normal"


class InstanceStore:
    """Store for instance management operations."""

    def __init__(self, db: Optional[Any] = None):
        """Initialize store.

        Args:
            db: Database connection (TDSQLConnection)
        """
        self.db = db
        self._use_db = db is not None and db.is_connected

    async def initialize(self) -> None:
        """Initialize store."""
        if self.db is not None and self.db.is_connected:
            self._use_db = True
            logger.info("InstanceStore initialized with database")
        else:
            self._use_db = False
            logger.info("InstanceStore initialized without database")

    # ==================== Source operations (从 swe_instance_info 聚合) ====================

    async def get_source(self, source_id: str) -> Optional[Source]:
        """Get source by ID from instance table."""
        if self._use_db:
            query = """
                SELECT source_id, MIN(created_at) as created_at
                FROM swe_instance_info
                WHERE source_id = %s
                GROUP BY source_id
            """
            row = await self.db.fetch_one(query, (source_id,))
            if row:
                return Source(
                    source_id=row["source_id"],
                    source_name=source_id,  # source_name 直接使用 source_id
                    created_at=row["created_at"],
                )
        return None

    async def get_sources(self) -> list[Source]:
        """Get all sources from instance table."""
        if self._use_db:
            query = """
                SELECT source_id, MIN(created_at) as created_at
                FROM swe_instance_info
                GROUP BY source_id
                ORDER BY created_at
            """
            rows = await self.db.fetch_all(query)
            return [
                Source(
                    source_id=row["source_id"],
                    source_name=row["source_id"],  # source_name 直接使用 source_id
                    created_at=row["created_at"],
                )
                for row in rows
            ]
        return []

    async def get_sources_with_stats(self) -> list[SourceWithStats]:
        """Get all sources with statistics from instance table."""
        if self._use_db:
            query = """
                SELECT i.source_id,
                       MIN(i.created_at) as created_at,
                       COUNT(DISTINCT i.instance_id) as total_instances,
                       COUNT(DISTINCT CASE WHEN i.status = 'active' THEN i.instance_id END) as active_instances,
                       COUNT(DISTINCT u.id) as total_users
                FROM swe_instance_info i
                LEFT JOIN swe_instance_user u ON i.source_id = u.source_id
                GROUP BY i.source_id
                ORDER BY created_at
            """
            rows = await self.db.fetch_all(query)
            return [
                SourceWithStats(
                    source_id=row["source_id"],
                    source_name=row["source_id"],  # source_name 直接使用 source_id
                    created_at=row["created_at"],
                    total_instances=row["total_instances"] or 0,
                    active_instances=row["active_instances"] or 0,
                    total_users=row["total_users"] or 0,
                )
                for row in rows
            ]
        return []

    # ==================== Instance operations ====================

    async def create_instance(
        self,
        instance_id: str,
        source_id: str,
        instance_name: str,
        instance_url: str,
        max_users: int = 100,
        bbk_id: Optional[str] = None,
    ) -> Instance:
        """Create a new instance."""
        if self._use_db:
            query = """
                INSERT INTO swe_instance_info (instance_id, source_id, bbk_id, instance_name, instance_url, max_users)
                VALUES (%s, %s, %s, %s, %s, %s)
            """
            await self.db.execute(
                (
                    query,
                    (
                        instance_id,
                        source_id,
                        bbk_id,
                        instance_name,
                        instance_url,
                        max_users,
                    ),
                ),
            )
        return Instance(
            instance_id=instance_id,
            source_id=source_id,
            bbk_id=bbk_id,
            instance_name=instance_name,
            instance_url=instance_url,
            max_users=max_users,
        )

    async def get_instance(self, instance_id: str) -> Optional[Instance]:
        """Get instance by ID."""
        if self._use_db:
            query = "SELECT * FROM swe_instance_info WHERE instance_id = %s"
            row = await self.db.fetch_one(query, (instance_id,))
            if row:
                return Instance(
                    instance_id=row["instance_id"],
                    source_id=row["source_id"],
                    bbk_id=row["bbk_id"],
                    instance_name=row["instance_name"],
                    instance_url=row["instance_url"],
                    max_users=row["max_users"],
                    status=InstanceStatus(row["status"]),
                    created_at=row["created_at"],
                )
        return None

    async def get_instance_with_usage(
        self,
        instance_id: str,
    ) -> Optional[InstanceWithUsage]:
        """Get instance with usage statistics."""
        if self._use_db:
            query = """
                SELECT i.*,
                       (SELECT COUNT(*) FROM swe_instance_user WHERE instance_id = i.instance_id AND status = 'active') as current_users
                FROM swe_instance_info i
                WHERE i.instance_id = %s
            """
            row = await self.db.fetch_one(query, (instance_id,))
            if row:
                current_users = row["current_users"] or 0
                max_users = row["max_users"] or 100
                usage_percent = (
                    (current_users / max_users * 100) if max_users > 0 else 0
                )
                return InstanceWithUsage(
                    instance_id=row["instance_id"],
                    source_id=row["source_id"],
                    bbk_id=row["bbk_id"],
                    instance_name=row["instance_name"],
                    instance_url=row["instance_url"],
                    max_users=max_users,
                    status=InstanceStatus(row["status"]),
                    created_at=row["created_at"],
                    current_users=current_users,
                    usage_percent=round(usage_percent, 2),
                    warning_level=calculate_warning_level(
                        current_users,
                        max_users,
                    ),
                    source_name=row["source_id"],  # source_name 直接使用 source_id
                    bbk_name=row.get("bbk_name"),
                )
        return None

    async def get_instances(
        self,
        source_id: Optional[str] = None,
        status: Optional[str] = None,
    ) -> list[InstanceWithUsage]:
        """Get instances with optional filters."""
        if self._use_db:
            where_clauses = []
            params: list[str] = []

            if source_id:
                where_clauses.append("i.source_id = %s")
                params.append(source_id)
            if status:
                where_clauses.append("i.status = %s")
                params.append(status)

            where_sql = " AND ".join(where_clauses) if where_clauses else "1=1"

            query = f"""
                SELECT i.*,
                       (SELECT COUNT(*) FROM swe_instance_user WHERE instance_id = i.instance_id AND status = 'active') as current_users
                FROM swe_instance_info i
                WHERE {where_sql}
                ORDER BY i.created_at
            """
            rows = await self.db.fetch_all(query, tuple(params))
            instances = []
            for row in rows:
                current_users = row["current_users"] or 0
                max_users = row["max_users"] or 100
                usage_percent = (
                    (current_users / max_users * 100) if max_users > 0 else 0
                )
                instances.append(
                    InstanceWithUsage(
                        instance_id=row["instance_id"],
                        source_id=row["source_id"],
                        bbk_id=row["bbk_id"],
                        instance_name=row["instance_name"],
                        instance_url=row["instance_url"],
                        max_users=max_users,
                        status=InstanceStatus(row["status"]),
                        created_at=row["created_at"],
                        current_users=current_users,
                        usage_percent=round(usage_percent, 2),
                        warning_level=calculate_warning_level(
                            current_users,
                            max_users,
                        ),
                        source_name=row[
                            "source_id"
                        ],  # source_name 直接使用 source_id
                        bbk_name=row.get("bbk_name"),
                    ),
                )
            return instances
        return []

    async def get_available_instances(
        self,
        source_id: str,
    ) -> list[InstanceWithUsage]:
        """Get available instances for allocation."""
        if self._use_db:
            query = """
                SELECT i.*,
                       (SELECT COUNT(*) FROM swe_instance_user WHERE instance_id = i.instance_id AND status = 'active') as current_users
                FROM swe_instance_info i
                WHERE i.source_id = %s AND i.status = 'active'
                ORDER BY i.created_at
            """
            rows = await self.db.fetch_all(query, (source_id,))
            return self._rows_to_instances_with_usage(rows)
        return []

    def _rows_to_instances_with_usage(
        self,
        rows: list[dict],
    ) -> list[InstanceWithUsage]:
        """Convert database rows to InstanceWithUsage list."""
        instances = []
        for row in rows:
            current_users = row["current_users"] or 0
            max_users = row["max_users"] or 100
            usage_percent = (
                (current_users / max_users * 100) if max_users > 0 else 0
            )
            instances.append(
                InstanceWithUsage(
                    instance_id=row["instance_id"],
                    source_id=row["source_id"],
                    bbk_id=row["bbk_id"],
                    instance_name=row["instance_name"],
                    instance_url=row["instance_url"],
                    max_users=max_users,
                    status=InstanceStatus(row["status"]),
                    created_at=row["created_at"],
                    current_users=current_users,
                    usage_percent=round(usage_percent, 2),
                    warning_level=calculate_warning_level(
                        current_users,
                        max_users,
                    ),
                    source_name=row["source_id"],  # source_name 直接使用 source_id
                    bbk_name=row.get("bbk_name"),
                ),
            )
        return instances

    async def update_instance(
        self,
        instance_id: str,
        instance_name: Optional[str] = None,
        instance_url: Optional[str] = None,
        max_users: Optional[int] = None,
        status: Optional[InstanceStatus] = None,
    ) -> bool:
        """Update instance."""
        if self._use_db:
            updates = []
            params: list[Union[str, int]] = []

            if instance_name is not None:
                updates.append("instance_name = %s")
                params.append(instance_name)
            if instance_url is not None:
                updates.append("instance_url = %s")
                params.append(instance_url)
            if max_users is not None:
                updates.append("max_users = %s")
                params.append(max_users)
            if status is not None:
                updates.append("status = %s")
                params.append(status.value)

            if not updates:
                return False

            params.append(instance_id)
            query = f"UPDATE swe_instance_info SET {', '.join(updates)} WHERE instance_id = %s"
            result = await self.db.execute(query, tuple(params))
            return result > 0
        return False

    async def delete_instance(self, instance_id: str) -> bool:
        """Delete instance."""
        if self._use_db:
            # Check if instance has users
            query = (
                "SELECT COUNT(*) as cnt FROM swe_instance_user "
                "WHERE instance_id = %s AND status = 'active'"
            )
            row = await self.db.fetch_one(query, (instance_id,))
            if row and row["cnt"] > 0:
                return False
            query = "DELETE FROM swe_instance_info WHERE instance_id = %s"
            result = await self.db.execute(query, (instance_id,))
            return result > 0
        return False

    # ==================== User allocation operations ====================

    async def create_allocation(
        self,
        user_id: str,
        source_id: str,
        instance_id: str,
    ) -> UserAllocation:
        """Create user allocation."""
        if self._use_db:
            query = """
                INSERT INTO swe_instance_user (user_id, source_id, instance_id)
                VALUES (%s, %s, %s)
            """
            await self.db.execute(query, (user_id, source_id, instance_id))
        return UserAllocation(
            user_id=user_id,
            source_id=source_id,
            instance_id=instance_id,
        )

    async def get_allocation(
        self,
        user_id: str,
        source_id: str,
    ) -> Optional[UserAllocation]:
        """Get user allocation by user_id and source_id."""
        if self._use_db:
            query = """
                SELECT u.*, i.instance_name, i.instance_url
                FROM swe_instance_user u
                LEFT JOIN swe_instance_info i ON u.instance_id = i.instance_id
                WHERE u.user_id = %s AND u.source_id = %s AND u.status = 'active'
            """
            row = await self.db.fetch_one(query, (user_id, source_id))

            if row:
                return UserAllocation(
                    id=row["id"],
                    user_id=row["user_id"],
                    source_id=row["source_id"],
                    instance_id=row["instance_id"],
                    allocated_at=row["allocated_at"],
                    status=UserAllocationStatus(row["status"]),
                    source_name=row["source_id"],  # source_name 直接使用 source_id
                    instance_name=row["instance_name"],
                    instance_url=row["instance_url"],
                )
        return None

    async def get_allocations(
        self,
        user_id: Optional[str] = None,
        source_id: Optional[str] = None,
        instance_id: Optional[str] = None,
        page: int = 1,
        page_size: int = 20,
    ) -> tuple[list[UserAllocation], int]:
        """Get allocations with filters and pagination."""
        if self._use_db:
            where_clauses = ["u.status = 'active'"]
            params: list[Union[str, int]] = []

            if user_id:
                where_clauses.append("u.user_id LIKE %s")
                params.append(f"%{user_id}%")
            if source_id:
                where_clauses.append("u.source_id = %s")
                params.append(source_id)
            if instance_id:
                where_clauses.append("u.instance_id = %s")
                params.append(instance_id)

            where_sql = " AND ".join(where_clauses)

            # Count query
            count_query = f"""
                SELECT COUNT(*) as total FROM swe_instance_user u WHERE {where_sql}
            """
            count_row = await self.db.fetch_one(count_query, tuple(params))
            total = count_row["total"] if count_row else 0

            # Data query
            offset = (page - 1) * page_size
            query = f"""
                SELECT u.*, i.instance_name, i.instance_url
                FROM swe_instance_user u
                LEFT JOIN swe_instance_info i ON u.instance_id = i.instance_id
                WHERE {where_sql}
                ORDER BY u.allocated_at DESC
                LIMIT %s OFFSET %s
            """
            params.extend([page_size, offset])
            rows = await self.db.fetch_all(query, tuple(params))

            allocations = [
                UserAllocation(
                    id=row["id"],
                    user_id=row["user_id"],
                    source_id=row["source_id"],
                    instance_id=row["instance_id"],
                    allocated_at=row["allocated_at"],
                    status=UserAllocationStatus(row["status"]),
                    source_name=row["source_id"],  # source_name 直接使用 source_id
                    instance_name=row["instance_name"],
                    instance_url=row["instance_url"],
                )
                for row in rows
            ]

            return allocations, total
        return [], 0

    async def get_user_ids(self) -> list[str]:
        """Get all unique user IDs from allocations.

        Returns:
            List of unique user IDs
        """
        if self._use_db:
            query = (
                "SELECT DISTINCT user_id FROM swe_instance_user "
                "WHERE status = 'active' ORDER BY user_id"
            )
            rows = await self.db.fetch_all(query)
            return [row["user_id"] for row in rows]
        return []

    async def update_allocation_instance(
        self,
        user_id: str,
        source_id: str,
        new_instance_id: str,
    ) -> bool:
        """Update user's allocated instance."""
        if self._use_db:
            query = """
                UPDATE swe_instance_user
                SET instance_id = %s, status = 'migrated'
                WHERE user_id = %s AND source_id = %s AND status = 'active'
            """
            result = await self.db.execute(
                query,
                (new_instance_id, user_id, source_id),
            )
            return result > 0
        return False

    async def delete_allocation(
        self,
        user_id: str,
        source_id: str,
    ) -> bool:
        """Delete user allocation."""
        if self._use_db:
            query = "DELETE FROM swe_instance_user WHERE user_id = %s AND source_id = %s"
            result = await self.db.execute(query, (user_id, source_id))
            return result > 0
        return False

    # ==================== Log operations ====================

    async def create_log(
        self,
        action: str,
        target_type: str,
        target_id: str,
        old_value: Optional[dict] = None,
        new_value: Optional[dict] = None,
        operator: Optional[str] = None,
    ) -> None:
        """Create operation log."""
        if self._use_db:
            query = """
                INSERT INTO swe_instance_log (action, target_type, target_id, old_value, new_value, operator)
                VALUES (%s, %s, %s, %s, %s, %s)
            """
            await self.db.execute(
                query,
                (
                    action,
                    target_type,
                    target_id,
                    json.dumps(old_value) if old_value else None,
                    json.dumps(new_value) if new_value else None,
                    operator,
                ),
            )

    async def get_logs(
        self,
        action: Optional[str] = None,
        target_type: Optional[str] = None,
        target_id: Optional[str] = None,
        page: int = 1,
        page_size: int = 20,
    ) -> tuple[list[OperationLog], int]:
        """Get logs with filters and pagination."""
        if self._use_db:
            where_clauses = []
            params: list[Union[str, int]] = []

            if action:
                where_clauses.append("action = %s")
                params.append(action)
            if target_type:
                where_clauses.append("target_type = %s")
                params.append(target_type)
            if target_id:
                where_clauses.append("target_id LIKE %s")
                params.append(f"%{target_id}%")

            where_sql = " AND ".join(where_clauses) if where_clauses else "1=1"

            # Count query
            count_query = f"SELECT COUNT(*) as total FROM swe_instance_log WHERE {where_sql}"
            count_row = await self.db.fetch_one(count_query, tuple(params))
            total = count_row["total"] if count_row else 0

            # Data query
            offset = (page - 1) * page_size
            query = f"""
                SELECT * FROM swe_instance_log
                WHERE {where_sql}
                ORDER BY created_at DESC
                LIMIT %s OFFSET %s
            """
            params.extend([page_size, offset])
            rows = await self.db.fetch_all(query, tuple(params))

            logs = [
                OperationLog(
                    id=row["id"],
                    action=row["action"],
                    target_type=row["target_type"],
                    target_id=row["target_id"],
                    old_value=json.loads(row["old_value"])
                    if row["old_value"]
                    else None,
                    new_value=json.loads(row["new_value"])
                    if row["new_value"]
                    else None,
                    operator=row["operator"],
                    created_at=row["created_at"],
                )
                for row in rows
            ]

            return logs, total
        return [], 0

    # ==================== Statistics ====================

    async def get_overview_stats(self) -> dict:
        """Get overview statistics."""
        if self._use_db:
            stats = {}

            # Instance stats
            query = """
                SELECT
                    COUNT(*) as total,
                    SUM(CASE WHEN status = 'active' THEN 1 ELSE 0 END) as active
                FROM swe_instance_info
            """
            row = await self.db.fetch_one(query)
            stats["total_instances"] = row["total"] if row else 0
            stats["active_instances"] = row["active"] if row else 0

            # Total users
            query = "SELECT COUNT(*) as total FROM swe_instance_user WHERE status = 'active'"
            row = await self.db.fetch_one(query)
            stats["total_users"] = row["total"] if row else 0

            # Warning/Critical instances
            query = """
                SELECT i.instance_id, i.max_users,
                       (SELECT COUNT(*) FROM swe_instance_user WHERE instance_id = i.instance_id AND status = 'active') as current_users
                FROM swe_instance_info i
                WHERE i.status = 'active'
            """
            rows = await self.db.fetch_all(query)
            warning_count = 0
            critical_count = 0
            for r in rows:
                level = calculate_warning_level(
                    r["current_users"] or 0,
                    r["max_users"] or 100,
                )
                if level == "warning":
                    warning_count += 1
                elif level == "critical":
                    critical_count += 1
            stats["warning_instances"] = warning_count
            stats["critical_instances"] = critical_count

            return stats
        return {
            "total_instances": 0,
            "total_users": 0,
            "active_instances": 0,
            "warning_instances": 0,
            "critical_instances": 0,
        }
