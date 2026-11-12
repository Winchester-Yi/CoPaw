# -*- coding: utf-8 -*-
"""Slow 10k-workspace local-filesystem migration acceptance benchmark."""

import asyncio
from hashlib import sha256
import json
import shutil
import os
import sys
from pathlib import Path
import threading
import time
from uuid import NAMESPACE_URL, uuid5

import psutil
import pytest

from agentscope.message import Msg
from swe.agents.memory.conversation_archive import ConversationArchiveStore
from swe.app.conversation_storage.bundle import BundleStore
from swe.app.chat_sharing.store import ChatShareStore
from swe.app.conversation_storage.migration.discovery import (
    discover_services,
    freeze_discovery,
    source_key,
)
from swe.app.conversation_storage.migration.manifest import SourceBoundary
from swe.app.conversation_storage.migration.spool import InventorySpool
from swe.cli.conversation_storage_v3_cmd import run_v3_command
from tests.integrated.conversation_storage.conftest import (
    SQL_ROOT,
    apply_sql_script,
)

WORKSPACE_COUNT = 10000
POPULATED_EVERY = 20


def _run_bounded_command(operation, manifest_path, **options):
    from swe.app.conversation_storage.migration.disk_manifest import (
        disk_index_policy,
    )

    directory = options.get("work_dir", Path(manifest_path).parent / "work")
    with disk_index_policy(directory, shutil.disk_usage(directory).free // 4):
        report = run_v3_command(operation, manifest_path, **options)
        if "batches" in report:
            report = {
                "migration_id": report["migration_id"],
                "batch_count": len(report["batches"]),
            }
        return report


def _make_dataset(root, count):
    source = root / "source"
    nas = root / "nas"
    source_service = source / "default_SRC"
    profiles = {}
    source_service.mkdir(parents=True)
    for index in range(count):
        agent = "agent-" + str(index).zfill(5)
        workspace = source_service / "workspaces" / agent
        workspace.mkdir(parents=True)
        runtime = nas / "default_SRC" / "workspaces" / agent
        runtime.mkdir(parents=True)
        (workspace / "agent.json").write_text(
            json.dumps({"id": agent}),
        )
        profile = {
            "id": agent,
            "workspace_dir": str(runtime),
            "enabled": True,
        }
        profiles[agent] = profile
        populated = index % POPULATED_EVERY == 0
        chats = []
        if populated:
            session = "session-" + str(index)
            chat_id = str(
                uuid5(NAMESPACE_URL, "migration-benchmark/" + str(index)),
            )
            chats.append(
                {
                    "id": chat_id,
                    "session_id": session,
                    "user_id": "alice",
                    "channel": "console",
                    "created_at": "2026-11-01T00:00:00Z",
                    "updated_at": "2026-11-01T00:00:00Z",
                },
            )
            session_root = workspace / "sessions"
            session_root.mkdir()
            message_count = 128 if index == 0 else 2
            content = []
            for number in range(message_count):
                role = "user" if number % 2 == 0 else "assistant"
                message = _stable_message(
                    f"benchmark/{index}/{number}",
                    role,
                    "benchmark "
                    + str(number)
                    + "x" * (4096 if index == 0 else 32),
                )
                content.append(message.to_dict())
            if index == 0:
                shared_chat = {
                    **chats[0],
                    "id": str(uuid5(NAMESPACE_URL, "benchmark/shared")),
                }
                chats.append(shared_chat)
                store = ConversationArchiveStore(workspace / "dialog")
                archived = [
                    _stable_message(
                        f"archive/{index}/{number}",
                        "user",
                        "archived question " + str(number) + "x" * 4096,
                    )
                    for number in range(32)
                ]
                _stable_archive(store, chat_id, archived)
                # A Session without a registry Chat is still a migration input.
                (session_root / "orphan_standalone.json").write_text(
                    json.dumps(
                        {
                            "schema_version": 2,
                            "revision": 1,
                            "agent": {
                                "memory": {
                                    "content": [
                                        _stable_message(
                                            "independent/session",
                                            "user",
                                            "independent session",
                                        ).to_dict(),
                                    ],
                                },
                            },
                        },
                    ),
                )
            (session_root / ("alice_" + session + ".json")).write_text(
                json.dumps(
                    {
                        "schema_version": 2,
                        "revision": 1,
                        "agent": {"memory": {"content": content}},
                    },
                ),
            )
        (workspace / "chats.json").write_text(
            json.dumps({"chats": chats}),
        )
    snapshot = source / "default" / "chat_shares" / (root.name + "-token.json")
    snapshot.parent.mkdir(parents=True)
    snapshot.write_text(
        json.dumps(
            {
                "chat_name": "benchmark shared",
                "messages": [{"role": "user", "content": "shared content"}],
            },
        ),
    )
    config_path = source_service / "config.json"
    config_path.write_text(json.dumps({"agents": {"profiles": profiles}}))
    for path in source.rglob("*"):
        path.chmod(0o444 if path.is_file() else 0o555)
    source.chmod(0o555)
    return source, nas


def _make_writable(root):
    for path in sorted(
        root.rglob("*"),
        key=lambda item: len(item.parts),
        reverse=True,
    ):
        if path.is_dir():
            path.chmod(0o755)
    root.chmod(0o755)


def _freeze_manifest(services, shares, path, schema):
    overrides = []
    for service in services:
        workspace = (
            Path(service["source_root"]) / "default_SRC/workspaces/agent-00000"
        )
        overrides.append(
            {
                "discovery_version": 1,
                "position": source_key(
                    "workspace",
                    service["service_unit_id"],
                    "default_SRC/workspaces/agent-00000",
                ),
                "tenant_id": "default",
                "source_id": "SRC",
                "agent_id": "agent-00000",
                "reason": "benchmark independent Session with explicit identity",
                "evidence_refs": [str(workspace / "agent.json")],
                "session_paths": [
                    {
                        "user_id": "orphan",
                        "channel": "console",
                        "session_id": "standalone",
                        "path": "sessions/orphan_standalone.json",
                    },
                ],
            },
        )
    report = discover_services(services, shares=shares, overrides=overrides)
    if not report["ready"]:
        raise AssertionError(report["blockers"])
    boundary = SourceBoundary(
        "snapshot",
        "2026-11-02T00:00:00Z",
        "old@sha256:" + "a" * 64,
        ("stopped", "readonly"),
    )
    return freeze_discovery(report, path, boundary, schema)


async def _interrupt_import_and_resume(
    manifest,
    workers,
    work_dir,
    monkeypatch,
):
    from swe.app.conversation_storage.migration.journal import MigrationJournal

    original_finish = MigrationJournal.refresh_batch
    interrupted = [False]

    async def interrupt_after_first_commit(importer, position):
        await original_finish(importer, position)
        if not interrupted[0]:
            interrupted[0] = True
            raise RuntimeError("benchmark interruption after commit")

    started = time.perf_counter()
    with monkeypatch.context() as interruption:
        interruption.setattr(
            MigrationJournal,
            "refresh_batch",
            interrupt_after_first_commit,
        )
        with pytest.raises(RuntimeError, match="benchmark interruption"):
            await asyncio.to_thread(
                _run_bounded_command,
                "import",
                manifest.path,
                apply=True,
                workers=workers,
                io_workers=workers,
                work_dir=work_dir,
            )
    if not interrupted[0]:
        raise AssertionError("import did not reach a committed batch")
    await asyncio.to_thread(
        _run_bounded_command,
        "resume",
        manifest.path,
        apply=True,
        workers=workers,
        io_workers=workers,
        work_dir=work_dir,
    )
    return round(time.perf_counter() - started, 3)


async def _verify_and_finalize(manifest, database, work_dir, output, workers):
    started = time.perf_counter()
    verification = await asyncio.to_thread(
        _run_bounded_command,
        "verify",
        manifest.path,
        workers=workers,
        io_workers=workers,
        work_dir=work_dir,
    )
    if not verification["passed"]:
        raise AssertionError(verification)
    verify_seconds = round(time.perf_counter() - started, 3)
    await apply_sql_script(
        database,
        SQL_ROOT / "conversation_share_service_unit_finalize.sql",
    )
    started = time.perf_counter()
    await asyncio.to_thread(
        _run_bounded_command,
        "finalize",
        manifest.path,
        apply=True,
        output_dir=output,
        workers=workers,
        io_workers=workers,
        work_dir=work_dir,
    )
    return verify_seconds, round(time.perf_counter() - started, 3)


@pytest.mark.slow
@pytest.mark.asyncio
@pytest.mark.parametrize("workers", (1, 4, 8, 16))
async def test_10k_workspace_import_resume_verify_finalize(
    conversation_test_database,
    tmp_path,
    monkeypatch,
    workers,
):
    database = conversation_test_database
    await ChatShareStore(database).ensure_schema()
    await apply_sql_script(
        database,
        SQL_ROOT / "conversation_share_publication_extension.sql",
    )
    await apply_sql_script(
        database,
        SQL_ROOT / "conversation_migration_control_tables.sql",
    )
    _progress("dataset", workers)
    datasets = [
        _make_dataset(
            tmp_path / ("service" + str(index)),
            WORKSPACE_COUNT // 2,
        )
        for index in range(2)
    ]
    services = [
        {
            "service_unit_id": "BENCHMARK-SERVICE" + str(index),
            "source_root": str(source),
            "config_root": str(source),
            "original_root": str(nas),
            "nas_root": str(nas),
            "snapshot_root": str(source),
        }
        for index, (source, nas) in enumerate(datasets)
    ]
    for index in range(2):
        token = "service" + str(index) + "-token"
        await database.execute(
            "INSERT INTO swe_chat_shares (token,chat_id,creator_id,tenant_id,snapshot_key,created_at,access_count,last_access_at) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
            (
                token,
                str(uuid5(NAMESPACE_URL, "migration-benchmark/0")),
                "alice",
                "default",
                "default/chat_shares/" + token + ".json",
                "2026-11-01 00:00:00",
                7,
                None,
            ),
        )
    shares = await database.fetch_all(
        "SELECT token,chat_id,creator_id,tenant_id,snapshot_key,created_at,access_count,last_access_at FROM swe_chat_shares",
    )
    metrics = _dataset_metrics(datasets)
    delay_ms = _configured_io_delay(monkeypatch)
    process = psutil.Process()
    rss_peak = [process.memory_info().rss]
    stop_sampler = threading.Event()

    def sample_rss():
        while not stop_sampler.wait(0.01):
            rss_peak[0] = max(rss_peak[0], process.memory_info().rss)

    sampler = threading.Thread(target=sample_rss, daemon=True)
    sampler.start()
    peak_spool_bytes = _track_spool_peak(monkeypatch)
    peak_index_bytes = _track_index_peak(monkeypatch)
    phases = {}
    started = time.perf_counter()
    try:
        _progress("discovery", workers)
        discovery_started = time.perf_counter()
        manifest = await asyncio.to_thread(
            _freeze_manifest,
            services,
            shares,
            tmp_path / "manifest.json",
            database.config.database,
        )
        phases["discovery_and_freeze_seconds"] = round(
            time.perf_counter() - discovery_started,
            3,
        )
        monkeypatch.setattr(
            "swe.database.config.get_database_config",
            lambda: database.config,
        )
        work_dir = tmp_path / "work"
        work_dir.mkdir()
        _progress("preflight", workers)
        preflight = await asyncio.to_thread(
            _run_bounded_command,
            "preflight",
            manifest.path,
        )
        assert preflight["passed"]
        _progress("import-and-resume", workers)
        phases["import_with_resume_seconds"] = (
            await _interrupt_import_and_resume(
                manifest,
                workers,
                work_dir,
                monkeypatch,
            )
        )
        _progress("verify-and-finalize", workers)
        verify_seconds, finalize_seconds = await _verify_and_finalize(
            manifest,
            database,
            work_dir,
            tmp_path / "completion",
            workers,
        )
        phases["verify_seconds"] = verify_seconds
        phases["finalize_seconds"] = finalize_seconds
        rss_peak[0] = max(rss_peak[0], process.memory_info().rss)
        print(
            json.dumps(
                {
                    "workers": workers,
                    "workspaces": WORKSPACE_COUNT,
                    "populated_workspaces": WORKSPACE_COUNT // POPULATED_EVERY,
                    "phases": phases,
                    "dataset": metrics,
                    "configured_nas_io_delay_ms": delay_ms,
                    "total_seconds": round(time.perf_counter() - started, 3),
                    "sampled_process_peak_rss_bytes": rss_peak[0],
                    "peak_spool_bytes": peak_spool_bytes[0],
                    "peak_index_bytes": peak_index_bytes[0],
                },
                sort_keys=True,
            ),
        )
    finally:
        stop_sampler.set()
        sampler.join()
        for source, _nas in datasets:
            _make_writable(source)


def _progress(stage, workers):
    print(
        json.dumps({"benchmark_stage": stage, "workers": workers}),
        file=sys.stderr,
        flush=True,
    )


def _dataset_metrics(datasets):
    sizes = [
        path.stat().st_size
        for source, _nas in datasets
        for path in source.rglob("*")
        if path.is_file()
    ]
    return {
        "source_bytes": sum(sizes),
        "source_files": len(sizes),
        "max_file_bytes": max(sizes),
        "content_sha256": [
            _content_digest(source, nas) for source, nas in datasets
        ],
        "services": len(datasets),
        "shares": len(datasets),
        "long_tail_sessions": len(datasets),
        "archived_messages": 32 * len(datasets),
        "independent_sessions": len(datasets),
        "shared_session_chats": 2 * len(datasets),
    }


def _inject_io_delay(monkeypatch, milliseconds):
    if not milliseconds:
        return
    for name in ("read_extent", "publish", "confirm_durable"):
        original = getattr(BundleStore, name)

        def delayed(*args, _original=original, **kwargs):
            time.sleep(milliseconds / 1000)
            return _original(*args, **kwargs)

        monkeypatch.setattr(BundleStore, name, delayed)


def test_representative_benchmark_inventory(tmp_path):
    source, nas = _make_dataset(tmp_path, 21)
    service = {
        "service_unit_id": "BENCHMARK-SERVICE",
        "source_root": str(source),
        "config_root": str(source),
        "original_root": str(nas),
        "nas_root": str(nas),
        "snapshot_root": str(source),
    }
    try:
        manifest = _freeze_manifest(
            [service],
            [],
            tmp_path / "manifest.json",
            "test_benchmark",
        )
        from swe.app.conversation_storage.migration.inventory_v3 import (
            inventory_v3,
        )

        inventory = inventory_v3(manifest)
        assert inventory["ready"], inventory["blockers"]
        assert len(inventory["workspaces"]) == 21
        assert (
            sum(len(item["sessions"]) for item in inventory["workspaces"]) == 3
        )
        assert sum(len(item["chats"]) for item in inventory["workspaces"]) == 3
        assert (
            len(inventory["workspaces"][0]["chats"][0]["archive"]["batches"])
            > 0
        )
    finally:
        _make_writable(source)


def _configured_io_delay(monkeypatch):
    delay = float(os.environ.get("SWE_MIGRATION_BENCH_IO_DELAY_MS", "0"))
    if not 0 <= delay <= 1000:
        raise ValueError("benchmark IO delay must be between 0 and 1000 ms")
    _inject_io_delay(monkeypatch, delay)
    return delay


def _track_spool_peak(monkeypatch):
    peak = [0]
    active = {}
    lock = threading.Lock()
    write = InventorySpool.write
    close = InventorySpool.close

    def tracked_write(spool, index, value):
        record = write(spool, index, value)
        with lock:
            active[id(spool)] = spool.used_bytes
            peak[0] = max(peak[0], sum(active.values()))
        return record

    def tracked_close(spool):
        try:
            return close(spool)
        finally:
            with lock:
                active.pop(id(spool), None)

    monkeypatch.setattr(InventorySpool, "write", tracked_write)
    monkeypatch.setattr(InventorySpool, "close", tracked_close)
    return peak


def _track_index_peak(monkeypatch):
    from swe.app.conversation_storage.migration.disk_manifest import (
        IndexPolicy,
    )

    peak = [0]
    close = IndexPolicy.close

    def tracked_close(policy):
        peak[0] = max(peak[0], policy.peak_bytes, policy.used_bytes())
        close(policy)

    monkeypatch.setattr(IndexPolicy, "close", tracked_close)
    return peak


def _stable_message(identity, role, content):
    message = Msg(role, content, role)
    message.id = str(uuid5(NAMESPACE_URL, identity))
    message.timestamp = "2026-11-01 00:00:00.000"
    return message


def _stable_archive(store, chat_id, messages):
    boundary = store._commit(chat_id, messages)
    stable_id = str(uuid5(NAMESPACE_URL, "archive-boundary/" + chat_id))
    chat_root = store.path_for(chat_id)
    for path in sorted(chat_root.rglob("*")):
        if path.is_file():
            raw = path.read_bytes().replace(
                boundary.id.encode(),
                stable_id.encode(),
            )
            raw = raw.replace(
                boundary.created_at.encode(),
                b"2026-11-01T00:00:00+00:00",
            )
            path.write_bytes(raw)
            if boundary.id in path.name:
                path.rename(
                    path.with_name(path.name.replace(boundary.id, stable_id)),
                )


def _content_digest(source, nas):
    digest = sha256()
    for path in sorted(source.rglob("*")):
        if not path.is_file():
            continue
        relative = path.relative_to(source).as_posix()
        payload = path.read_bytes()
        if path.name == "config.json":
            payload = payload.replace(str(nas).encode(), b"<runtime-root>")
        digest.update(relative.encode() + b"\x00" + payload + b"\x00")
    return digest.hexdigest()


def test_benchmark_content_is_deterministic(tmp_path):
    first = _make_dataset(tmp_path / "first/service0", 21)
    second = _make_dataset(tmp_path / "other/service0", 21)
    try:
        assert _content_digest(*first) == _content_digest(*second)
        assert _dataset_metrics([first]) == _dataset_metrics([second])
    finally:
        _make_writable(first[0])
        _make_writable(second[0])
