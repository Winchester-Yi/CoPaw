# -*- coding: utf-8 -*-
"""当日补跑的任务范围，受理后随调用记录冻结。"""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from .batch_follow import FOLLOWED_PARENT, PARENT_DEFINITION_JOIN
from .batch_members import MEMBERS, RECEIPTS
from .identity import identity_value
from .repository import IDENTITY_FIELDS

FILTERED_QUEUED = "filtered_queued"
FILTERED_RUNNING = "filtered_running"
FOLLOWED_BATCH = "followed_batch"


class DailyRetryFilters(BaseModel):
    model_config = ConfigDict(extra="forbid")
    task_scope: Literal["all", "batch", "ordinary", "followed_batch"] = "all"
    job_ids: list[str] | None = Field(default=None, max_length=1000)

    @field_validator("job_ids")
    @classmethod
    def validate_job_ids(cls, values):
        if values is None:
            return None
        return list(dict.fromkeys(identity_value(value) for value in values))


_MEMBER_MATCH = "m.current_execution_id=r.execution_id AND " + " AND ".join(
    f"m.{field} COLLATE utf8mb4_bin=r.{field}" for field in IDENTITY_FIELDS
)
_HAS_MEMBER = f"EXISTS (SELECT 1 FROM {MEMBERS} m WHERE {_MEMBER_MATCH})"
_PARENT_STATE_MATCH = " AND ".join(
    f"s.{field}=b.{field}" for field in IDENTITY_FIELDS
)
_HAS_FOLLOWED_BATCH = (
    f"EXISTS (SELECT 1 FROM {MEMBERS} m JOIN {RECEIPTS} b "
    "ON b.batch_id=m.batch_id AND b.source_id COLLATE utf8mb4_bin=m.source_id "
    f"JOIN swe_cron_job_states s ON {_PARENT_STATE_MATCH} "
    f"JOIN swe_cron_jobs_v2 p ON {PARENT_DEFINITION_JOIN} "
    f"WHERE {_MEMBER_MATCH} AND b.kind='batch' AND b.status='accepted' "
    f"AND {FOLLOWED_PARENT})"
)


def scan_filter(filters):
    if filters is None:
        return "", ()
    if filters.job_ids == []:
        return " AND 1=0", ()
    clauses, params = [], ()
    if filters.task_scope == "batch":
        clauses.append(f"(r.kind='batch' OR {_HAS_MEMBER})")
    elif filters.task_scope == FOLLOWED_BATCH:
        clauses.append(_HAS_FOLLOWED_BATCH)
    elif filters.task_scope == "ordinary":
        clauses.append(f"r.kind='ordinary' AND NOT {_HAS_MEMBER}")
    if filters.job_ids is not None:
        ids_sql, params = _job_ids_filter(filters)
        clauses.append(ids_sql)
    return "".join(f" AND {clause}" for clause in clauses), params


def _job_ids_filter(filters):
    ids = tuple(filters.job_ids)
    marks = ",".join(["%s"] * len(ids))
    direct = f"r.job_id COLLATE utf8mb4_bin IN ({marks})"
    if filters.task_scope == "ordinary":
        return direct, ids
    parent = (
        f"EXISTS (SELECT 1 FROM {MEMBERS} m JOIN {RECEIPTS} b "
        "ON b.batch_id=m.batch_id AND b.source_id COLLATE utf8mb4_bin=m.source_id "
        f"WHERE {_MEMBER_MATCH} AND b.kind='batch' AND b.status='accepted' "
        f"AND b.job_id COLLATE utf8mb4_bin IN ({marks}))"
    )
    return f"({direct} OR {parent})", (*ids, *ids)


def matches_locked_scope(filters, row, member, receipt):
    if filters is None:
        return True
    kind = "batch" if member is not None else "ordinary"
    scope = (
        "batch" if filters.task_scope == FOLLOWED_BATCH else filters.task_scope
    )
    if scope not in {"all", kind}:
        return False
    ids = filters.job_ids
    return (
        ids is None
        or row["job_id"] in ids
        or (receipt is not None and receipt["job_id"] in ids)
    )
