# -*- coding: utf-8 -*-
"""Exercise the internal parallel importer through the real v3 lifecycle."""

import asyncio
from collections import Counter

import pytest

from swe.app.conversation_storage.migration.direct_import import DirectImporter
from swe.app.conversation_storage.migration.importer import (
    ParallelWorkspaceImporter,
)
from swe.app.conversation_storage.migration.offline_verify import (
    OfflineVerifyStages,
)
from tests.integrated.conversation_storage import test_v3_migration_mysql as v3


@pytest.mark.asyncio
async def test_parallel_multiservice_import_resume_verify_finalize(
    conversation_test_database,
    tmp_path,
    monkeypatch,
):
    entries, workspaces, retries, verified = [], [], [], []
    import_runs = 0
    run_parallel = DirectImporter.run_parallel
    run_workspace = ParallelWorkspaceImporter.run_workspace
    publish = ParallelWorkspaceImporter._publish
    prepare_verify = OfflineVerifyStages.prepare
    execute = v3._execute

    async def spooled_execute(
        operation,
        manifest,
        inventory,
        output,
        **options,
    ):
        if operation not in ("import", "resume"):
            return await execute(
                operation,
                manifest,
                inventory,
                output,
                **options,
            )
        from swe.cli.conversation_storage_v3_cmd import run_v3_command

        work_dir = tmp_path / "migration-spool"
        work_dir.mkdir(exist_ok=True)
        return await asyncio.to_thread(
            run_v3_command,
            operation,
            manifest.path,
            apply=True,
            workers=2,
            io_workers=2,
            memory_budget_mib=512,
            work_dir=work_dir,
        )

    monkeypatch.setattr(v3, "_execute", spooled_execute)

    async def verification(self, request, *args, **kwargs):
        ticket = await prepare_verify(self, request, *args, **kwargs)
        verified.append(request.scope.service_unit_id)
        return ticket

    async def parallel_run(
        self,
        report,
        *,
        workers=4,
        io_workers=4,
        health_check=None,
    ):
        nonlocal import_runs
        import_runs += 1
        owner = asyncio.current_task()
        entries.append(owner)
        result = await run_parallel(
            self,
            report,
            workers=2,
            io_workers=2,
            health_check=health_check,
        )
        if import_runs == 1:
            raise RuntimeError("simulated stopped migration process")
        # A second complete pass must verify committed source plans/bodies.
        # This exercises the workspace retry path even when the interrupted
        # legacy fixture stops during its shared-artifact phase.
        repeated = await run_parallel(
            self,
            report,
            workers=2,
            io_workers=2,
            health_check=health_check,
        )
        assert repeated == result
        return result

    async def workspace_run(self, workspace):
        workspaces.append((tuple(workspace["scope"]), asyncio.current_task()))
        return await run_workspace(self, workspace)

    async def publish_batch(self, position, scope, rows, blocks, *, spec=None):
        # The real method still builds the source plan and runs all domain
        # stages. Record publication order without replacing its behavior.
        result = await publish(self, position, scope, rows, blocks, spec=spec)
        retries.append((scope.service_unit_id, position))
        return result

    monkeypatch.setattr(DirectImporter, "run_parallel", parallel_run)
    monkeypatch.setattr(
        ParallelWorkspaceImporter,
        "run_workspace",
        workspace_run,
    )
    monkeypatch.setattr(ParallelWorkspaceImporter, "_publish", publish_batch)
    monkeypatch.setattr(OfflineVerifyStages, "prepare", verification)
    # Existing fixture creates 3 populated workspaces across 2 services,
    # interrupts import, reloads the manifest, resumes, verifies via CLI,
    # finalizes, and validates runtime authority plus permanent share fields.
    await v3.test_v3_discovery_import_resume_verify_finalize(
        conversation_test_database,
        tmp_path,
        monkeypatch,
    )
    assert len(entries) >= 2
    scopes = Counter(scope for scope, _task in workspaces)
    assert len(scopes) == 3
    assert {scope[0] for scope in scopes} == {"TEST-SERVICE", "TEST-SERVICE2"}
    assert all(count >= 2 for count in scopes.values())
    assert all(task not in entries for _scope, task in workspaces)
    assert retries
    assert set(verified) == {"TEST-SERVICE", "TEST-SERVICE2"}
    assert all(count >= 2 for count in Counter(retries).values())
    services = await conversation_test_database.fetch_all(
        "SELECT service_unit_key,COUNT(*) AS count "
        "FROM swe_conversation_sessions GROUP BY service_unit_key",
    )
    assert {row["service_unit_key"]: row["count"] for row in services} == {
        b"TEST-SERVICE": 2,
        b"TEST-SERVICE2": 1,
    }
