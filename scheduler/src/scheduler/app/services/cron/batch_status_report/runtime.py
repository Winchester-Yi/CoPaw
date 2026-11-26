# -*- coding: utf-8 -*-
"""Background collection and single-attempt delivery of scheduled reports."""

import asyncio
import logging
import os
from contextlib import asynccontextmanager

import httpx

from .....config.constant import SWE_INTERNAL_API_BASE_URL_ENV
from ....database.connection import get_db_connection
from ..swe_routing import checked_base, swe_base_for_source
from .render import render_parts
from .store import ReportStore

logger = logging.getLogger(__name__)
PART_BYTES = 2048
SWE_REPORT_PATH = "/api/internal/cron/batch-status-reports"


class BatchReportWorker:
    def __init__(self, store, client, swe_url):
        self.store = store
        self.client = client
        self.swe_url = swe_url
        token = os.environ.get("SWE_INTERNAL_TOKEN")
        self.swe_headers = (
            {"X-Internal-Token": f"Bearer {token}"} if token else {}
        )

    async def collect(self, report):
        try:
            base = swe_base_for_source(
                report["source_id"],
                self.swe_url,
                internal=True,
            )
            response = await self.client.post(
                f"{base}{SWE_REPORT_PATH}/snapshot",
                json={
                    "source_id": report["source_id"],
                    "business_date": str(report["business_date"]),
                },
                headers=self.swe_headers,
                timeout=30,
            )
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            await self.store.collection_failed(
                report,
                "cron_report_snapshot_unavailable",
                exc.response.status_code >= 500,
            )
            return
        except httpx.RequestError:
            await self.store.collection_failed(
                report,
                "cron_report_snapshot_unavailable",
                True,
            )
            return
        try:
            snapshot = response.json()
            parts = None
            if snapshot.get("batch_scope") == "followed_batch":
                parts = render_parts(
                    report["source_id"],
                    report["business_date"],
                    snapshot,
                    PART_BYTES,
                )
        except (ValueError, KeyError, TypeError, AttributeError):
            await self.store.collection_failed(
                report,
                "cron_report_invalid_snapshot",
                False,
            )
            return
        if parts is None:
            await self.store.collection_failed(
                report,
                "cron_report_snapshot_scope_unavailable",
                True,
            )
            return
        await self.store.freeze(report, snapshot, parts)

    async def deliver(self, part):
        if not await self.store.start_delivery(part):
            return
        try:
            base = swe_base_for_source(
                part["source_id"],
                self.swe_url,
                internal=True,
            )
            response = await self.client.post(
                f"{base}{SWE_REPORT_PATH}/send-part",
                json=part,
                headers=self.swe_headers,
                timeout=30,
            )
            if response.status_code in {401, 403, 404, 422}:
                await self.store.finish_delivery(
                    part,
                    "failed",
                    "cron_report_delivery_rejected",
                )
                return
            response.raise_for_status()
            result = response.json()
            if not isinstance(result, dict) or result.get("status") not in {
                "sent",
                "failed",
                "outcome_unknown",
            }:
                raise ValueError("invalid_delivery_reply")
        except (httpx.ConnectError, httpx.ConnectTimeout):
            await self.store.finish_delivery(
                part,
                "failed",
                "cron_report_delivery_connect_failed",
            )
            return
        except (httpx.HTTPError, ValueError):
            await self.store.finish_delivery(
                part,
                "outcome_unknown",
                "cron_report_delivery_unknown",
            )
            return
        code = result.get("error_code")
        receipts = result.get("receipt_ids")
        await self.store.finish_delivery(
            part,
            result["status"],
            code[:128] if isinstance(code, str) else None,
            receipts if isinstance(receipts, list) else None,
        )

    async def tick(self):
        for part in await self.store.expired_deliveries():
            await self.store.finish_delivery(
                part,
                "outcome_unknown",
                "cron_report_delivery_expired",
            )
        report = await self.store.collect_next()
        if report:
            await self.collect(report)
        parts = await self.store.pending_deliveries()
        outcomes = await asyncio.gather(
            *(self.deliver(part) for part in parts),
            return_exceptions=True,
        )
        for result in outcomes:
            if isinstance(result, BaseException):
                logger.warning(
                    "批调度简报投递未确认 error_type=%s",
                    type(result).__name__,
                )

    async def run(self):
        while True:
            try:
                await self.tick()
            except Exception as exc:
                logger.warning(
                    "批调度简报后台异常 error_type=%s",
                    type(exc).__name__,
                )
            await asyncio.sleep(1)


@asynccontextmanager
async def report_lifespan(app):
    app.state.batch_report_ready = False
    try:
        store = ReportStore(get_db_connection())
        swe_url = checked_base(
            os.environ.get(SWE_INTERNAL_API_BASE_URL_ENV, ""),
        ).removesuffix("/api")
        await store.ready()
    except Exception as exc:
        logger.warning(
            "批调度简报后台未启用 error_type=%s",
            type(exc).__name__,
        )
        yield
        return
    async with httpx.AsyncClient() as client:
        task = asyncio.create_task(
            BatchReportWorker(store, client, swe_url).run(),
            name="cron-batch-status-reports",
        )
        app.state.batch_report_ready = True
        try:
            yield
        finally:
            app.state.batch_report_ready = False
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
