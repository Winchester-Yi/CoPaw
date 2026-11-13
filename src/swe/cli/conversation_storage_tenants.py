# -*- coding: utf-8 -*-
"""Tenant-isolated direct migration, retaining the original job and SQL tickets."""

from hashlib import sha256
import json
from pathlib import Path
import shutil
import tempfile
import time
from types import SimpleNamespace

from swe.app.conversation_storage.bundle import BundleStore
from swe.app.conversation_storage.migration.direct_import import (
    DirectImporter,
    DirectTarget,
)
from swe.app.conversation_storage.migration.inventory_v3 import (
    inventories_equal,
    inventory_v3_spooled,
)
from swe.app.conversation_storage.migration.journal import (
    BUSINESS_TABLES,
    MigrationJournal,
    JOBS,
)
from swe.app.conversation_storage.migration.parallel import MigrationIO
from swe.app.conversation_storage.migration.preflight import (
    DMLDatabase,
    check_schema,
    check_tenant_schema,
)
from swe.app.conversation_storage.migration.spool import (
    InventorySpool,
    MigrationCapacityError,
    MigrationDiskCapacityError,
)
from swe.app.conversation_storage.migration.tenant_state import TenantState
from swe.app.conversation_storage.migration.verify import verify_import


class TenantFailure(ValueError):
    """An explicitly identified tenant data failure, never a shared SQL error."""


class TenantManifest:
    """A filtered parser view; persist only the complete parent manifest."""

    def __init__(self, parent, tenant):
        self.parent = parent
        self.migration_id, self.path = parent.migration_id, parent.path
        self.key = tenant["service_unit_id"], tenant["tenant_id"]
        self.data = {
            **parent.data,
            "workspaces": [
                w
                for w in parent.data["workspaces"]
                if (w["service_unit_id"], w["tenant_id"]) == self.key
            ],
            "shares": [
                s
                for s in parent.data["shares"]
                if (s["service_unit_id"], s["tenant_id"]) == self.key
            ],
            "source_change_blockers": [
                b
                for b in parent.data.get("source_change_blockers", [])
                if tuple(b.get("scope", [])) == self.key
            ],
        }

    def _persist(self):
        for name in ("assignments", "batches"):
            self.parent.data[name] = self.data[name]
        self.parent._persist()

    def record_source_change(self, blocker):
        self._persist()
        result = self.parent.record_source_change(blocker)
        self.data["source_change_blockers"] = [
            b for b in result if tuple(b.get("scope", [])) == self.key
        ]
        return self.data["source_change_blockers"]

    def set_batch_state(self, *_args):
        # SQL completion is authoritative; verification runs read-only.
        return None

    def bind_target(self, target):
        self.parent._validate_target(target)
        if self.parent.data.get("target") != target:
            raise ValueError("tenant migration target differs from frozen job")


def tenant_evidence(view):
    return {
        "workspaces": view.data["workspaces"],
        "shares": view.data["shares"],
        "service": next(
            s
            for s in view.data["services"]
            if s["service_unit_id"] == view.key[0]
        ),
    }


def tenant_receipt(view):
    from .conversation_storage_v3_cmd import authority_receipt

    proof = authority_receipt(view)
    proof["services"] = {view.key[0]: proof["services"][view.key[0]]}
    return proof


def _work_base(work_dir, data):
    from .conversation_storage_v3_cmd import validate_output

    choices = (
        (Path(work_dir),)
        if work_dir
        else (Path(tempfile.gettempdir()).resolve(), Path("/tmp").resolve())
    )
    for choice in choices:
        try:
            candidate = validate_output(choice, data)
        except ValueError:
            continue
        if candidate.is_dir():
            return candidate
    raise ValueError("no tenant work directory is separate from source roots")


def _spool(base, data, memory_bytes, workers):
    return InventorySpool(
        base,
        max_bytes=shutil.disk_usage(base).free * 3 // 4,
        record_limit=memory_bytes // workers,
        protected=[
            s[k]
            for s in data["services"]
            for k in (
                "source_root",
                "config_root",
                "snapshot_root",
                "nas_root",
            )
            if s.get(k)
        ],
    )


async def _inventory(view, spool, resources):
    from swe.app.conversation_storage.migration.source_inputs import (
        SourceInputInvalid,
    )

    try:
        result = await inventory_v3_spooled(
            view,
            spool,
            record_source_changes=False,
            **resources,
        )
    except SourceInputInvalid as error:
        raise TenantFailure(
            "tenant source path is unsafe or invalid",
        ) from error
    except FileNotFoundError as error:
        roots = [Path(s["source_root"]) for s in view.data["services"]]
        path = Path(error.filename) if error.filename else None
        if (
            path is None
            or not all(root.is_dir() for root in roots)
            or not any(
                path.is_relative_to(Path(w["root"]))
                for w in view.data["workspaces"]
            )
        ):
            raise
        raise TenantFailure("tenant input is missing") from error
    if not result["ready"] or result["blockers"]:
        raise TenantFailure("tenant inventory contains blockers")
    from swe.app.conversation_storage.migration.importer import (
        extract_messages,
    )

    try:
        for workspace in result["workspaces"]:
            for session in workspace["sessions"]:
                extract_messages(session["legacy"]["root"], context_epoch=0)
    except (KeyError, TypeError, ValueError) as error:
        raise TenantFailure("tenant message data is invalid") from error
    return result


async def _compare_shares(db, view):
    from .conversation_storage_v3_cmd import SHARE_FIELDS, _stamp

    # Unpublished shares have no service key yet. Include NULL and this service,
    # but do not compare another service's already published share counters.
    rows = await db.fetch_all(
        "SELECT "
        + ",".join(SHARE_FIELDS)
        + ",service_unit_key FROM swe_chat_shares "
        "WHERE BINARY tenant_id=%s AND (service_unit_key IS NULL OR service_unit_key=%s) ORDER BY token",
        (view.key[1].encode(), view.key[0].encode()),
    )

    def normalized(row):
        return {
            k: (
                _stamp(row[k])
                if k in ("created_at", "last_access_at")
                else row[k]
            )
            for k in SHARE_FIELDS
        }

    expected = {s["token"]: normalized(s) for s in view.data["shares"]}
    known = {
        s["token"]
        for s in view.parent.data.get(
            "source_shares",
            view.parent.data["shares"],
        )
    }
    if any(s["token"] not in known for s in rows):
        raise TenantFailure("unregistered tenant share appeared")
    actual = {
        s["token"]: normalized(s)
        for s in rows
        if s["token"] in expected
        or s["service_unit_key"] == view.key[0].encode()
    }
    if len(expected) != len(view.data["shares"]) or actual != expected:
        raise TenantFailure("unpublished tenant original shares changed")


async def _verify(db, bundles, view, inventory, target, resources, guard):
    readonly = SimpleNamespace(
        data=view.data,
        migration_id=view.migration_id,
        set_batch_state=lambda *_: None,
    )
    async with MigrationIO(resources["io_workers"]) as io:
        result = await verify_import(
            db,
            bundles,
            readonly,
            inventory,
            target,
            io=io,
            health_check=guard.check,
            **resources,
        )
    if not result["passed"]:
        raise TenantFailure("tenant semantic verification failed")
    return result


async def _one_tenant(operation, view, db, states, guard, base, resources):
    from .conversation_storage_v3_cmd import validate_runtime_roots

    schema = view.data["offline"]["application_schema"]
    target = DirectTarget(schema, schema, view.migration_id)
    journal = MigrationJournal(db, view, server_uuid=guard.server_uuid)
    journal.active_tenant = view.key
    journal.frozen_bindings_data = view.parent.data
    bundles = {
        s["service_unit_id"]: BundleStore(s["nas_root"])
        for s in view.data["services"]
    }
    with _spool(
        base,
        view.data,
        resources["memory_bytes"],
        resources["workers"],
    ) as spool:
        inventory = await _inventory(view, spool, resources)
        await _compare_shares(db, view)
        row = await states.get(*view.key)
        if operation in ("import", "resume"):
            await DirectImporter(
                db,
                bundles,
                view,
                target,
                journal=journal,
            ).run_parallel(
                inventory,
                workers=resources["workers"],
                io_workers=resources["io_workers"],
                health_check=guard.check,
            )
        elif row["state"] not in ("imported", "verified"):
            raise TenantFailure("complete tenant import is required")
        else:
            await journal.hydrate(persist=False)
            await journal.validate_ownership()
        if operation in ("verify", "finalize"):
            await _verify(
                db,
                bundles,
                view,
                inventory,
                target,
                resources,
                guard,
            )
        with _spool(
            base,
            view.data,
            resources["memory_bytes"],
            resources["workers"],
        ) as check_spool:
            actual = await _inventory(view, check_spool, resources)
            if not inventories_equal(inventory, actual):
                raise TenantFailure("tenant source changed during operation")
        await _compare_shares(db, view)
        await guard.check()
        await check_schema(db, schema, finalized=False)
        await check_tenant_schema(db, schema)
        if operation in ("verify", "finalize"):
            await states.transition(*view.key, "verified")
        if operation == "finalize":
            # The receipt is service-local; only this tenant's roots are checked.
            selected = {
                **view.data,
                "services": [
                    s
                    for s in view.data["services"]
                    if s["service_unit_id"] == view.key[0]
                ],
            }
            try:
                validate_runtime_roots({**selected, "services": []})
            except ValueError as error:
                raise TenantFailure(
                    "tenant runtime root is unavailable",
                ) from error
            await states.publish(*view.key, tenant_receipt(view))


_TENANT_FAILURE_REASONS = {
    "tenant source path is unsafe or invalid": "会话路径不安全：检查软链接、越界或特殊文件",
    "tenant input is missing": "冻结会话输入缺失",
    "tenant inventory contains blockers": "会话清单校验失败；查看受限审计清单中的 blockers",
    "tenant message data is invalid": "会话消息格式无效",
    "unregistered tenant share appeared": "出现未登记的分享记录",
    "unpublished tenant original shares changed": "未发布租户的原始分享记录发生变化",
    "tenant semantic verification failed": "导入结果与源会话语义校验不一致",
    "complete tenant import is required": "租户导入未完成；先执行 resume --apply",
    "tenant source changed during operation": "迁移期间冻结会话输入发生变化",
    "tenant runtime root is unavailable": "运行时 workspace 目录缺失或不安全",
}


def _failure(tenant, stage, code="tenant_data_invalid", error=None):
    reason = _TENANT_FAILURE_REASONS.get(
        str(error),
        "租户会话输入或完整性校验未通过，修复后重试",
    )
    if isinstance(error, MigrationCapacityError):
        code, reason = (
            "tenant_capacity_exceeded",
            "会话输入超过迁移容量或条目预算",
        )
    if stage == "discover":
        reason = "租户发现失败；查看 discovery.json 中该租户的 blockers"
    return {
        "service_unit_id": tenant["service_unit_id"],
        "tenant_id": tenant["tenant_id"],
        "stage": stage,
        "code": code,
        "reason": reason,
    }


def _write_failures(manifest, operation, failures):
    from .conversation_storage_v3_cmd import _write_json

    destination = manifest.path.parent / (
        manifest.path.name
        + "."
        + operation
        + "."
        + str(time.time_ns())
        + ".failed-tenants.json"
    )
    _write_json(destination, failures)
    return str(destination)


def _write_runtime(output, manifest):
    from .conversation_storage_v3_cmd import _write_json

    output.mkdir(parents=True, exist_ok=True)
    for service in manifest.data["services"]:
        document = {
            "version": 3,
            "migration_id": manifest.migration_id,
            "service_unit_id": service["service_unit_id"],
            "nas_root": service["nas_root"],
        }
        name = sha256(service["service_unit_id"].encode()).hexdigest()
        _write_json(output / (name + ".runtime.json"), document)


async def _rollback_command(
    control,
    manifest,
    output,
    guard,
    apply,
    rollback_inputs,
):
    if not apply or rollback_inputs is None or output is None:
        raise ValueError(
            "rollback-tenants requires --apply, --rollback-inputs and --output-dir",
        )
    from swe.app.conversation_storage.chat_mutations import (
        load_metadata_json,
    )
    from swe.app.conversation_storage.migration.tenant_rollback import (
        TenantRollbackEvidence,
        compose_tenant_rollback,
    )
    from swe.app.conversation_storage.migration.manifest import (
        SourceBoundary,
    )
    from swe.app.conversation_storage.migration.exporter import (
        OldFormatTarget,
    )

    inputs = load_metadata_json(Path(rollback_inputs).read_bytes())
    evidence = TenantRollbackEvidence(
        SourceBoundary(**inputs["boundary"]),
        inputs["writers_stopped_ref"],
        inputs["io_drained_ref"],
        inputs["current_config_review_ref"],
    )
    await guard.check()
    report = await compose_tenant_rollback(
        control,
        manifest,
        output,
        OldFormatTarget(**inputs["target"]),
        evidence,
        current_configs=inputs["current_configs"],
    )
    await guard.check()
    return {
        **report,
        "status": "partial_success",
        "reason": "pinned old image validation is required before rollback",
    }


async def run_tenants(
    operation,
    manifest,
    output,
    *,
    workers,
    io_workers,
    memory_budget_mib,
    work_dir,
    apply,
    reviewed_discovery=None,
    rollback_inputs=None,
):
    """Run one SQL-owner job; skip published tenants before reading old input."""
    from swe.database.config import get_database_config
    from swe.database.connection import DatabaseConnection, _named_lock_name
    from swe.app.conversation_storage.migration.lock_guard import (
        MigrationLockGuard,
    )

    base = _work_base(work_dir, manifest.data)
    from swe.app.conversation_storage.migration.discovery import _overlaps

    for online in manifest.data["services"]:
        for frozen in manifest.data["services"]:
            if any(
                _overlaps(Path(online["original_root"]), Path(frozen[key]))
                for key in ("source_root", "config_root", "snapshot_root")
                if frozen.get(key)
            ):
                raise ValueError(
                    "partial migration requires isolated frozen inputs",
                )
    resources = {
        "workers": workers,
        "io_workers": io_workers,
        "memory_bytes": memory_budget_mib * 1024 * 1024,
    }
    config = get_database_config()
    schema = manifest.data["offline"]["application_schema"]
    if config.database != schema:
        raise ValueError("configured application schema differs from manifest")
    control = DatabaseConnection(
        config.model_copy(update={"time_zone": "+00:00"}),
    )
    try:
        await control.connect()
        async with control.named_lock(
            "conversation-migration:" + schema,
            timeout=0,
        ):
            db = DMLDatabase(control)
            identity = await db.fetch_one(
                "SELECT @@server_uuid AS server_uuid,DATABASE() AS schema_name",
            )
            if (
                identity["schema_name"] != schema
                or not identity["server_uuid"]
            ):
                raise ValueError("database identity differs from target")
            guard = await MigrationLockGuard.capture(
                db,
                _named_lock_name("conversation-migration:" + schema),
                schema=schema,
                server_uuid=identity["server_uuid"],
            )
            await check_schema(db, schema, finalized=False)
            await check_tenant_schema(db, schema)
            from swe.app.conversation_storage.migration.tenant_retry import (
                recover_pending_review,
                review_retry,
            )

            await recover_pending_review(db, manifest)
            # Fix cutoffs under the same lock before first recording SQL binding.
            if operation in ("import", "resume") and apply:
                manifest.begin_tenant_syncs(
                    [
                        (t["service_unit_id"], t["tenant_id"])
                        for t in manifest.data["tenants"]
                    ],
                )
            await _check_job_binding(db, manifest, guard.server_uuid)
            if operation == "retry-review":
                if not apply or reviewed_discovery is None:
                    raise ValueError(
                        "retry-review requires --apply and --reviewed-discovery",
                    )
                from swe.app.conversation_storage.chat_mutations import (
                    load_metadata_json,
                )

                report = load_metadata_json(
                    Path(reviewed_discovery).read_bytes(),
                )
                await guard.check()
                outcome = await review_retry(db, manifest, report)
                await guard.check()
                return {
                    "version": 3,
                    "status": "success",
                    "review": outcome,
                    "migration_id": manifest.migration_id,
                }
            if operation == "rollback-tenants":
                return await _rollback_command(
                    control,
                    manifest,
                    output,
                    guard,
                    apply,
                    rollback_inputs,
                )
            if operation == "preflight":
                result = await _preview(
                    operation,
                    manifest,
                    base,
                    resources,
                    states=TenantState(db, manifest.migration_id),
                )
                return {**result, "passed": result["failed"] == 0}
            if operation in ("inventory", "plan", "inspect") or (
                operation in ("import", "resume", "finalize") and not apply
            ):
                return await _preview(
                    operation,
                    manifest,
                    base,
                    resources,
                    states=TenantState(db, manifest.migration_id),
                )
            return await _run_registered(
                operation,
                manifest,
                output,
                db,
                guard,
                base,
                resources,
            )
    finally:
        await control.close()


async def _register_tenants(db, manifest, states, guard):
    tenants = manifest.data["tenants"]
    registrations = []
    for tenant in tenants:
        view = TenantManifest(manifest, tenant)
        registrations.append({**tenant, "evidence": tenant_evidence(view)})
    # Retain a complete immutable job binding even when every tenant failed
    # discovery; runtime startup can then safely keep all tenants unavailable.
    jobs = await _check_job_binding(db, manifest, guard.server_uuid)
    await states.register(registrations)
    if not jobs:
        binding_journal = MigrationJournal(
            db,
            manifest,
            server_uuid=guard.server_uuid,
        )
        binding_journal.active_tenant = (
            tenants[0]["service_unit_id"],
            tenants[0]["tenant_id"],
        )
        await binding_journal.prepare()


async def _run_registered(
    operation,
    manifest,
    output,
    db,
    guard,
    base,
    resources,
):
    states = TenantState(db, manifest.migration_id)
    tenants = manifest.data["tenants"]
    if not tenants:
        raise ValueError("tenant migration has no identifiable tenants")
    await _register_tenants(db, manifest, states, guard)
    failures, published, completed = [], 0, 0
    for tenant in tenants:
        view = TenantManifest(manifest, tenant)
        row = await states.get(*view.key)
        if row["state"] == "published":
            published += 1
            continue
        if tenant["state"] == "failed":
            failure = _failure(tenant, "discover")
            await states.fail(
                *view.key,
                "discover",
                failure["code"],
                failure["reason"],
            )
            failures.append(failure)
            continue
        await guard.check()
        try:
            await _one_tenant(
                operation,
                view,
                db,
                states,
                guard,
                base,
                resources,
            )
        except MigrationDiskCapacityError:
            raise
        except (
            TenantFailure,
            MigrationCapacityError,
        ) as error:
            failure = _failure(tenant, operation, error=error)
            await states.fail(
                *view.key,
                operation,
                failure["code"],
                failure["reason"],
            )
            failures.append(failure)
        else:
            completed += 1
            if operation == "finalize":
                published += 1
    # A job may remain partial while the runtime admits individually published tenants.
    pending_schema = False
    if published == len(tenants) and not failures:
        share_column = await db.fetch_one(
            "SELECT IS_NULLABLE FROM information_schema.COLUMNS WHERE TABLE_SCHEMA=DATABASE() "
            "AND TABLE_NAME='swe_chat_shares' AND COLUMN_NAME='service_unit_key'",
        )
        pending_schema = (
            not share_column or share_column["IS_NULLABLE"] != "NO"
        )
        if not pending_schema:
            await check_schema(
                db,
                manifest.data["offline"]["application_schema"],
                finalized=True,
            )
    state = (
        "ready"
        if published == len(tenants) and not failures and not pending_schema
        else "partial_success"
    )
    changed = await db.execute(
        "UPDATE "
        + JOBS
        + " SET state=%s,updated_at=UTC_TIMESTAMP(6) WHERE migration_id=%s",
        (state, manifest.migration_id.encode()),
    )
    if changed != 1:
        raise ValueError("migration job completion was not confirmed")
    if operation == "finalize":
        _write_runtime(output, manifest)
    return {
        "version": 3,
        "migration_id": manifest.migration_id,
        "tenant_isolation": True,
        "status": (
            "partial_success" if failures or pending_schema else "success"
        ),
        "state": state,
        "tenants": len(tenants),
        "published": published,
        "completed": completed,
        "failed": len(failures),
        "pending_schema_finalize": pending_schema,
        "failure_report": _write_failures(manifest, operation, failures),
    }


async def _check_job_binding(db, manifest, server_uuid):
    journal = MigrationJournal(db, manifest, server_uuid=server_uuid)
    target, source = journal._bindings()
    jobs = await db.fetch_all("SELECT * FROM " + JOBS)
    if any(
        row["migration_id"] != manifest.migration_id.encode() for row in jobs
    ):
        raise ValueError("another migration job owns this application schema")
    if jobs:
        if (
            jobs[0]["target_binding_text"] != target
            or jobs[0]["source_binding_text"] != source
            or jobs[0]["manifest_version"] != 3
        ):
            raise ValueError("migration source or target binding changed")
    else:
        for table in BUSINESS_TABLES:
            if await db.fetch_one("SELECT id FROM " + table + " LIMIT 1"):
                raise ValueError(
                    "first migration requires six empty business tables",
                )
    return jobs


async def _preview(operation, manifest, base, resources, *, states):
    failures, completed = [], 0
    for tenant in manifest.data["tenants"]:
        row = await states.get(tenant["service_unit_id"], tenant["tenant_id"])
        if row and row["state"] == "published":
            completed += 1
            continue
        if tenant["state"] == "failed":
            failures.append(_failure(tenant, "discover"))
            continue
        view = TenantManifest(manifest, tenant)
        try:
            with _spool(
                base,
                view.data,
                resources["memory_bytes"],
                resources["workers"],
            ) as spool:
                await _inventory(view, spool, resources)
        except MigrationDiskCapacityError:
            raise
        except (TenantFailure, MigrationCapacityError) as error:
            failures.append(_failure(tenant, operation, error=error))
        else:
            completed += 1
    return {
        "version": 3,
        "migration_id": manifest.migration_id,
        "tenant_isolation": True,
        "preview": True,
        "status": "partial_success" if failures else "success",
        "completed": completed,
        "failed": len(failures),
        "failure_report": _write_failures(manifest, operation, failures),
    }
