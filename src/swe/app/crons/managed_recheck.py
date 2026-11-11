# -*- coding: utf-8 -*-
"""Execute one recheck in its parent's existing task session."""

import asyncio
import json
import logging
from uuid import uuid4

from ..runner.session_lock import SessionLockTimeout
from .managed_models import load_managed_spec
from .managed_resources import ResourcePreparer
from .managed_results import ExecutionOutcome
from .recheck_context import RecheckContext, RecheckStartDenied, active_recheck

logger = logging.getLogger(__name__)


def _session_run(row, spec):
    return {
        **row,
        "execution_id": row["recheck_id"],
        "spec_json": spec.model_dump_json(),
    }


class RecheckService:
    def __init__(self, store, engine, receipts, chats, workspace_dir):
        self.store = store
        self.engine = engine
        self.receipts = receipts
        self.chats = chats
        self.workspace_dir = workspace_dir

    async def execute(self, row, key):
        recheck_id = row["recheck_id"]
        dispatch_token = row["dispatch_owner"]
        owner = str(uuid4())
        try:
            payload = json.loads(row["payload_json"])
            spec = load_managed_spec(payload["spec"])
            session_id, user_id = await self._verify_binding(spec, key)
        except (ValueError, KeyError, TypeError):
            await self.store.reject(
                recheck_id,
                key,
                dispatch_token,
                "cron_recheck_binding_unavailable",
            )
            return
        timeout = spec.runtime.timeout_seconds
        started = False

        async def start():
            nonlocal started
            started = await self.store.start(
                recheck_id,
                key,
                dispatch_token,
                owner,
                timeout,
            )
            return started

        context_token = active_recheck.set(
            RecheckContext(
                session_id,
                user_id,
                payload["parent_output"],
                start,
            ),
        )
        try:
            result = await asyncio.wait_for(
                self.engine.execute(
                    spec,
                    dispatch_meta={
                        "cron_execution_key": recheck_id,
                        "workspace_dir": str(self.workspace_dir),
                    },
                ),
                timeout=timeout,
            )
            outcome = ExecutionOutcome(result.status, trace_id=result.trace_id)
        except RecheckStartDenied:
            return
        except SessionLockTimeout:
            await self.store.release(recheck_id, key, dispatch_token)
            return
        except (Exception, asyncio.CancelledError) as exc:
            logger.info(
                "回检执行结束 recheck_id=%s error_type=%s",
                recheck_id,
                type(exc).__name__,
            )
            if isinstance(exc, asyncio.CancelledError) and not started:
                raise
            outcome = ExecutionOutcome(
                (
                    "cancelled"
                    if isinstance(exc, asyncio.CancelledError)
                    else "error"
                ),
                error_code="cron_recheck_execution_failed",
            )
        finally:
            active_recheck.reset(context_token)
        if not started:
            await self.store.reject(
                recheck_id,
                key,
                dispatch_token,
                outcome.error_code or "cron_recheck_admission_unavailable",
            )
            return
        run = _session_run({**row, "run_owner": owner}, spec)
        try:
            await self.receipts.save(run, outcome)
        except Exception:
            logger.warning("回检会话凭证暂存失败 recheck_id=%s", recheck_id)
        await self.store.finish(
            recheck_id,
            key,
            owner,
            outcome.status,
            outcome.error_code,
        )
        await self.receipts.clear(run)

    async def _verify_binding(self, spec, key):
        if (spec.tenant_id, spec.source_id, spec.id) != (
            key[0],
            key[1],
            key[3],
        ):
            raise ValueError("cron_recheck_binding_conflict")
        session_id = spec.meta["task_session_id"]
        user_id = spec.meta["creator_user_id"]
        chat = await self.chats.get_chat(spec.meta["task_chat_id"])
        if chat is None:
            raise ValueError("cron_recheck_chat_missing")
        ResourcePreparer(self.chats, agent_id=key[2])._verify(
            chat,
            spec,
            session_id,
            user_id,
            migrated=(chat.meta or {}).get("management_owner") != "scheduler",
        )
        return session_id, user_id

    async def recover(self, row, key):
        if row["status"] != "running":
            return row
        spec = load_managed_spec(json.loads(row["payload_json"])["spec"])
        run = _session_run(row, spec)
        outcome = await self.receipts.load(run)
        if outcome is None:
            return await self.store.finish(
                row["recheck_id"],
                key,
                row["run_owner"],
                "error",
                "outcome_unknown",
                expired=True,
            )
        result = await self.store.finish(
            row["recheck_id"],
            key,
            row["run_owner"],
            outcome.status,
            outcome.error_code,
        )
        await self.receipts.clear(run)
        return result
