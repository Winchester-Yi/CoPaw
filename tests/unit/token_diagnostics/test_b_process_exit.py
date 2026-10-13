# -*- coding: utf-8 -*-
"""A real abrupt producer exit cannot turn unflushed evidence into completeness."""

from pathlib import Path
import subprocess
import sys

import pytest

from tests.unit.token_diagnostics.test_b_queries import (
    SCOPE,
    filters,
    seed_task,
    service_for,
)
from tests.unit.token_diagnostics.test_relational_storage import (
    relational_db,
)  # noqa: F401


@pytest.mark.asyncio
async def test_abrupt_exit_keeps_business_result_and_exposes_missing_attempt(
    tmp_path,
    relational_db,
):
    destination = tmp_path / "unflushed-evidence.txt"
    code = """
import asyncio, os, sys
from pathlib import Path
from swe.token_diagnostics.config import DiagnosticConfig
from swe.token_diagnostics.writer import DiagnosticWriter
class BlockedStore:
    async def write_batch(self, records):
        await asyncio.Event().wait()
        Path(sys.argv[1]).write_text('persisted')
async def main():
    writer = DiagnosticWriter(BlockedStore(), DiagnosticConfig(
        enabled=True, context_enabled=True, max_records=1))
    assert writer.add({'kind':'call', 'data':{
        'call_id':'lost-attempt', 'branch_run_id':'branch', 'revision':1,
        'input_tokens':None, 'output_tokens':None}})
    writer.start()
    await asyncio.sleep(0)
    assert writer.inflight_count + writer.pending_count == 1
    print('business-result-returned', flush=True)
    os._exit(23)
asyncio.run(main())
"""
    result = subprocess.run(
        [sys.executable, "-c", code, str(destination)],
        cwd=Path(__file__).resolve().parents[3],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert result.returncode == 23
    assert "business-result-returned" in result.stdout
    assert not destination.exists()
    await seed_task(relational_db, calls=[], expected_attempt_count=1)
    task = await service_for(relational_db).task("task", SCOPE, filters())
    assert task["completeness"]["observed_call_count"] == 0
    assert task["completeness"]["expected_attempt_count"] == 1
    assert not task["completeness"]["usage_complete"]
    assert task["lifecycle_usage"]["actual_total_tokens"] is None
