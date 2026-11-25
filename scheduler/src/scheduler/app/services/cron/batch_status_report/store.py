# -*- coding: utf-8 -*-
"""Durable report collection and a single dispatch claim per message part."""

import json
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from uuid import uuid4
from zoneinfo import ZoneInfo

from ..managed.storage import transaction

REPORTS = "swe_cron_batch_status_reports"
DELIVERIES = "swe_cron_batch_status_report_deliveries"
COLLECTION_LEASE_SECONDS = 120
DELIVERY_CONFIRM_SECONDS = 75
COLLECTION_DELAYS = (15, 60)


def now_utc():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def iso(value):
    return value.replace(tzinfo=timezone.utc).isoformat() if value else None


def report_summary(row):
    return {
        "report_id": row["report_id"],
        "source_id": row["source_id"],
        "accepted_at": iso(row["created_at"]),
        "business_date": str(row["business_date"]),
        "status": row["status"],
    }


def recipient_status(states, report_status):
    if not states:
        return "not_started" if report_status == "failed" else "pending"
    if all(state == "pending" for state in states):
        return "pending"
    if any(state in {"pending", "sending"} for state in states):
        return "sending"
    if all(state == "sent" for state in states):
        return "sent"
    if "sent" in states:
        return "partial"
    if "outcome_unknown" in states:
        return "outcome_unknown"
    return "failed"


class ReportStore:
    def __init__(self, db):
        self.db = db

    async def ready(self):
        await self.db.fetch_all(f"SELECT report_id FROM {REPORTS} LIMIT 0")
        await self.db.fetch_all(
            f"SELECT delivery_id FROM {DELIVERIES} LIMIT 0",
        )

    async def submit(self, body):
        now = now_utc()
        business_date = (
            now.replace(tzinfo=timezone.utc)
            .astimezone(
                ZoneInfo("Asia/Shanghai"),
            )
            .date()
        )
        row = {
            "report_id": str(uuid4()),
            "source_id": body.source_id,
            "business_date": business_date,
            "created_at": now,
            "status": "queued",
        }
        await self.db.execute(
            f"INSERT INTO {REPORTS} (report_id,source_id,business_date,"
            "recipient_ids,status,available_at,created_at) "
            "VALUES (%s,%s,%s,%s,'queued',%s,%s)",
            (
                row["report_id"],
                body.source_id,
                business_date,
                json.dumps(body.recipient_ids),
                now,
                now,
            ),
        )
        return report_summary(row)

    async def report_for_source(self, source, report_id):
        return await self.db.fetch_one(
            f"SELECT report_id,source_id,created_at,business_date,status,"
            "snapshot_at,batch_count,error_code,recipient_ids "
            f"FROM {REPORTS} WHERE source_id=%s AND report_id=%s FOR SHARE",
            (source, report_id),
        )

    async def results(self, report):
        rows = await self.db.fetch_all(
            f"SELECT recipient_id,part_no,status,error_code FROM {DELIVERIES} "
            "WHERE report_id=%s ORDER BY recipient_id,part_no",
            (report["report_id"],),
        )
        by_recipient = defaultdict(list)
        for row in rows:
            by_recipient[row["recipient_id"]].append(row)
        recipients = []
        for employee in json.loads(report["recipient_ids"]):
            parts = by_recipient[employee]
            states = [part["status"] for part in parts]
            recipients.append(
                {
                    "recipient_id": employee,
                    "status": recipient_status(states, report["status"]),
                    "total_parts": len(parts),
                    "sent_parts": states.count("sent"),
                    "failed_parts": states.count("failed"),
                    "unknown_parts": states.count("outcome_unknown"),
                    "issues": [
                        {
                            key: part[key]
                            for key in ("part_no", "status", "error_code")
                        }
                        for part in parts
                        if part["status"] in {"failed", "outcome_unknown"}
                    ],
                },
            )
        return {
            **report_summary(report),
            "snapshot_at": iso(report["snapshot_at"]),
            "batch_count": report["batch_count"],
            "error_code": report["error_code"],
            "recipients": recipients,
        }

    async def collect_next(self):
        now = now_utc()
        due = "((status='queued' AND available_at<=%s) OR (status='collecting' AND lease_until<=%s))"
        row = await self.db.fetch_one(
            f"SELECT report_id,collection_attempts FROM {REPORTS} WHERE {due} "
            "ORDER BY available_at,report_id LIMIT 1",
            (now, now),
        )
        if not row:
            return None
        if row["collection_attempts"] > len(COLLECTION_DELAYS):
            await self.db.execute(
                f"UPDATE {REPORTS} SET status='failed',finished_at=%s,"
                f"error_code='cron_report_collection_expired' WHERE report_id=%s AND {due}",
                (now, row["report_id"], now, now),
            )
            return None
        owner = str(uuid4())
        changed = await self.db.execute(
            f"UPDATE {REPORTS} SET status='collecting',lease_token=%s,lease_until=%s,"
            "collection_attempts=collection_attempts+1 "
            f"WHERE report_id=%s AND collection_attempts=%s AND {due}",
            (
                owner,
                now + timedelta(seconds=COLLECTION_LEASE_SECONDS),
                row["report_id"],
                row["collection_attempts"],
                now,
                now,
            ),
        )
        if not changed:
            return None
        return await self.db.fetch_one(
            f"SELECT * FROM {REPORTS} WHERE report_id=%s AND lease_token=%s",
            (row["report_id"], owner),
        )

    async def collection_failed(self, report, error, retryable):
        now = now_utc()
        attempts = report["collection_attempts"]
        delay = (
            COLLECTION_DELAYS[attempts - 1]
            if retryable and attempts <= len(COLLECTION_DELAYS)
            else None
        )
        await self.db.execute(
            f"UPDATE {REPORTS} SET status=%s,available_at=%s,error_code=%s,finished_at=%s,"
            "lease_token=NULL,lease_until=NULL WHERE report_id=%s AND status='collecting' AND lease_token=%s",
            (
                "queued" if delay else "failed",
                now + timedelta(seconds=delay or 0),
                error,
                None if delay else now,
                report["report_id"],
                report["lease_token"],
            ),
        )

    async def freeze(self, report, snapshot, parts):
        batch_count = len(snapshot["batches"])
        has_issues = any(
            batch["status"] == "unavailable"
            or batch["subtasks"].get("incomplete")
            for batch in snapshot["batches"]
        )
        at = (
            datetime.fromisoformat(snapshot["snapshot_at"])
            .astimezone(timezone.utc)
            .replace(tzinfo=None)
        )
        async with transaction(self.db) as tx:
            changed = await tx.execute(
                f"UPDATE {REPORTS} SET status='sending',snapshot_at=%s,batch_count=%s,"
                "snapshot_json=%s,parts_json=%s,template_version=1,error_code=%s,lease_token=NULL,"
                "lease_until=NULL WHERE report_id=%s AND status='collecting' "
                "AND lease_token=%s AND lease_until>UTC_TIMESTAMP(6)",
                (
                    at,
                    batch_count,
                    json.dumps(snapshot, ensure_ascii=False),
                    json.dumps(parts, ensure_ascii=False),
                    "cron_report_snapshot_partial" if has_issues else None,
                    report["report_id"],
                    report["lease_token"],
                ),
            )
            if not changed:
                return
            for employee in json.loads(report["recipient_ids"]):
                for first in range(0, len(parts), 200):
                    values = [
                        (report["report_id"], employee, position, str(uuid4()))
                        for position in range(
                            first + 1,
                            min(first + 200, len(parts)) + 1,
                        )
                    ]
                    placeholders = ",".join(
                        "(%s,%s,%s,%s,'pending')" for _ in values
                    )
                    await tx.execute(
                        f"INSERT INTO {DELIVERIES} (report_id,recipient_id,part_no,delivery_id,status)"
                        f" VALUES {placeholders}",
                        tuple(field for value in values for field in value),
                    )

    async def pending_deliveries(self):
        return await self.db.fetch_all(
            f"""SELECT d.report_id,d.recipient_id,d.part_no,d.delivery_id,r.source_id,
                       JSON_UNQUOTE(JSON_EXTRACT(r.parts_json, CONCAT('$[',d.part_no-1,']'))) AS text
                FROM {DELIVERIES} d JOIN {REPORTS} r ON r.report_id=d.report_id AND r.status='sending'
                WHERE d.status='pending' AND NOT EXISTS (
                    SELECT 1 FROM {DELIVERIES} prev WHERE prev.report_id=d.report_id
                    AND prev.recipient_id=d.recipient_id AND prev.part_no<d.part_no
                    AND prev.status IN ('pending','sending'))
                ORDER BY r.created_at,d.report_id,d.recipient_id,d.part_no LIMIT 16""",
        )

    async def start_delivery(self, part):
        return bool(
            await self.db.execute(
                f"UPDATE {DELIVERIES} SET status='sending',started_at=UTC_TIMESTAMP(6) "
                "WHERE delivery_id=%s AND status='pending'",
                (part["delivery_id"],),
            ),
        )

    async def expired_deliveries(self):
        return await self.db.fetch_all(
            f"SELECT report_id,delivery_id FROM {DELIVERIES} WHERE status='sending' "
            "AND started_at<%s ORDER BY started_at LIMIT 50",
            (now_utc() - timedelta(seconds=DELIVERY_CONFIRM_SECONDS),),
        )

    async def finish_delivery(self, part, status, error=None, receipts=None):
        now = now_utc()
        async with transaction(self.db) as tx:
            report = await tx.fetch_one(
                f"SELECT error_code FROM {REPORTS} WHERE report_id=%s FOR UPDATE",
                (part["report_id"],),
            )
            changed = await tx.execute(
                f"UPDATE {DELIVERIES} SET status=%s,finished_at=%s,error_code=%s,receipt_ids=%s "
                "WHERE delivery_id=%s AND status='sending'",
                (
                    status,
                    now,
                    error,
                    json.dumps(receipts) if receipts else None,
                    part["delivery_id"],
                ),
            )
            if not changed:
                return
            pending = await tx.fetch_one(
                f"SELECT 1 FROM {DELIVERIES} WHERE report_id=%s AND status IN ('pending','sending') "
                "LIMIT 1",
                (part["report_id"],),
            )
            if pending:
                return
            failed = await tx.fetch_one(
                f"SELECT 1 FROM {DELIVERIES} WHERE report_id=%s AND status IN ('failed','outcome_unknown') "
                "LIMIT 1",
                (part["report_id"],),
            )
            final = (
                "completed_with_errors"
                if failed or report["error_code"]
                else "completed"
            )
            await tx.execute(
                f"UPDATE {REPORTS} SET status=%s,finished_at=%s WHERE report_id=%s AND status='sending'",
                (final, now, part["report_id"]),
            )
