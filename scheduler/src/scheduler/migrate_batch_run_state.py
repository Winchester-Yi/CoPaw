"""Additive batch-control migration; default invocation is read-only."""

import argparse
import asyncio
import json

from .app.database.connection import init_db_connection, close_db_connection
from .app.database.batch_run_state_schema import apply_schema
from .app.services.cron.batch_run_state import initialize_missing_controls
from .app.services.cron.batch_run_state import now_local
from .app.services.cron.batch_operations import refresh_batch_counts


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--apply", action="store_true")
    group.add_argument("--dry-run", action="store_true")
    return parser.parse_args(argv)


async def run_migration(db, *, apply: bool = False) -> dict:
    if apply:
        await apply_schema(db)
    report = await initialize_missing_controls(db, dry_run=not apply)
    report["refreshed_batches"] = (
        await refresh_skipped_batches(db) if apply else 0
    )
    return report


async def refresh_skipped_batches(db) -> int:
    after = ""
    count = 0
    now = now_local()
    while True:
        rows = await db.fetch_all(
            "SELECT b.batch_id FROM swe_cron_dispatch_batches b "
            "WHERE b.batch_id>%s "
            "AND EXISTS (SELECT 1 FROM swe_cron_dispatch_intents i "
            "WHERE i.batch_id=b.batch_id AND i.source_id=b.source_id "
            "AND i.status IN ('skipped','cancelled')) "
            "ORDER BY b.batch_id LIMIT 500",
            (after,),
        )
        if not rows:
            return count
        for row in rows:
            await refresh_batch_counts(row["batch_id"], now, db=db)
            count += 1
        after = rows[-1]["batch_id"]


async def main(argv=None) -> None:
    args = parse_args(argv)
    try:
        db = await init_db_connection()
        report = await run_migration(db, apply=args.apply)
        print(
            json.dumps({"applied": args.apply, **report}, ensure_ascii=False)
        )
    finally:
        await close_db_connection()


if __name__ == "__main__":
    asyncio.run(main())
