# -*- coding: utf-8 -*-
"""Audited zero-data tenant evidence revision under the schema migration lock.

Callers must hold the existing schema named lock for both public operations.
A pending revision is retained across every uncertain SQL/file outcome.
"""

import asyncio
from hashlib import sha256
import json
import os
import time
from pathlib import Path
from uuid import uuid4

from .discovery import discover_services
from .disk_manifest import iter_json
from .journal import BUSINESS_TABLES, JOBS, ENTRIES, MigrationJournal
from .tenant_state import TenantState, _json
from .v3_manifest import V3Manifest

_FIELDS = (
    "services",
    "declared_services",
    "workspaces",
    "shares",
    "source_shares",
    "tenants",
    "blockers",
    "ready",
    "status",
    "overrides",
    "fingerprints",
    "discovery_version",
    "tenant_isolation",
)


def _digest(data):
    digest = sha256()
    for chunk in iter_json(data):
        digest.update(chunk.encode())
    return digest.hexdigest()


def _write(path, data):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        for chunk in iter_json(data):
            stream.write(chunk)
        stream.flush()
        os.fsync(stream.fileno())
    _sync(path.parent)


def _sync(directory):
    fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _pending(manifest):
    return manifest.path.with_name(manifest.path.name + ".review-pending.json")


def _key(item):
    return item["service_unit_id"], item["tenant_id"]


def _evidence(data, key):
    return {
        "workspaces": [w for w in data["workspaces"] if _key(w) == key],
        "shares": [s for s in data["shares"] if _key(s) == key],
        "service": next(
            s for s in data["services"] if s["service_unit_id"] == key[0]
        ),
    }


def _without_counts(value):
    if isinstance(value, dict):
        return {
            k: _without_counts(v)
            for k, v in value.items()
            if k not in ("counts", "empty")
        }
    if isinstance(value, list):
        return [_without_counts(v) for v in value]
    return value


def _source(data):
    # Reuse the exact journal canonical binding contract.
    from types import SimpleNamespace

    return MigrationJournal(
        None,
        SimpleNamespace(data=data, migration_id=data["migration_id"]),
    )._bindings()[1]


def _ticket_owner(row, workspaces=(), shares=()):
    details = row.get("details_text")
    if details:
        value = json.loads(details)
        if row.get("entry_kind") in ("retention", b"retention"):
            if isinstance(value.get("service_unit_key"), str) and isinstance(
                value.get("tenant_key"),
                str,
            ):
                return value["service_unit_key"], value["tenant_key"]
        scope = value.get("scope")
        if isinstance(scope, list) and len(scope) == 4:
            return tuple(scope[:2])
    raw = row["source_key"]
    if isinstance(raw, bytes):
        raw = raw.decode()
    if row.get("entry_kind") in ("share", b"share"):
        owners = {_key(share) for share in shares if share["token"] == raw}
        if len(owners) == 1:
            return owners.pop()
        raise ValueError("journal share ownership cannot be proved")
    # Entity source keys consist of a JSON identity followed by an optional
    # occurrence suffix. Decode only the leading JSON, never split on slashes
    # that may legally occur inside domain identities.
    try:
        value, end = json.JSONDecoder().raw_decode(raw)
    except ValueError as error:
        raise ValueError(
            "journal ticket ownership cannot be proved",
        ) from error
    if isinstance(value, list) and value and value[0] == "workspace":
        owners = {_key(w) for w in workspaces if w["position"] == raw[:end]}
        if len(owners) == 1 and (not raw[end:] or raw[end:].startswith("/")):
            return owners.pop()
    if raw[end:] and not raw[end:].startswith("/"):
        raise ValueError("journal source key has unknown suffix")
    if (
        isinstance(value, list)
        and value[0] in ("session", "chat")
        and len(value) >= 5
    ):
        return tuple(value[1:3])
    raise ValueError("journal ticket ownership cannot be proved")


async def _no_data(db, migration_id, keys, workspaces=(), shares=()):
    for service, tenant in keys:
        for table in BUSINESS_TABLES:
            if table in (
                "swe_conversation_turns",
                "swe_conversation_messages",
            ):
                orphan = await db.fetch_one(
                    "SELECT b.id FROM "
                    + table
                    + " b LEFT JOIN swe_conversation_sessions s ON s.id=b.session_id AND s.service_unit_key=b.service_unit_key WHERE s.id IS NULL LIMIT 1",
                )
                if orphan:
                    raise ValueError(
                        "tenant review cannot attribute orphan business row",
                    )
                query = (
                    "SELECT b.id FROM "
                    + table
                    + " b JOIN swe_conversation_sessions s ON s.id=b.session_id AND s.service_unit_key=b.service_unit_key WHERE s.service_unit_key=%s AND s.tenant_key=%s LIMIT 1"
                )
            else:
                query = (
                    "SELECT id FROM "
                    + table
                    + " WHERE service_unit_key=%s AND tenant_key=%s LIMIT 1"
                )
            row = await db.fetch_one(
                query,
                (service.encode(), tenant.encode()),
            )
            if row:
                raise ValueError(
                    "tenant evidence cannot change after business data exists",
                )
    after = 0
    while True:
        rows = await db.fetch_all(
            "SELECT id,entry_kind,source_key,details_text FROM "
            + ENTRIES
            + " WHERE migration_id=%s AND id>%s ORDER BY id LIMIT 1000",
            (migration_id.encode(), after),
        )
        if not rows:
            return
        for row in rows:
            # Runtime ID tickets are allocated only by published runtime writers;
            # tenant business rows above remain the authority for zero-data review.
            if row.get("entry_kind") in ("runtime_id", b"runtime_id"):
                continue
            if _ticket_owner(row, workspaces, shares) in keys:
                raise ValueError(
                    "tenant evidence cannot change after a journal ticket exists",
                )
        after = rows[-1]["id"]


async def review_retry(db, manifest, reviewed_report):
    """Revise only failed/discovered zero-data tenants; never rewrite live scopes."""
    if not manifest.data.get("tenant_isolation"):
        raise ValueError("tenant review requires isolated migration")
    if _pending(manifest).exists():
        raise ValueError("pending tenant review must be recovered first")
    states = TenantState(db, manifest.migration_id)
    published = set()
    for tenant in manifest.data["tenants"]:
        row = await states.get(*_key(tenant))
        if row is not None and row["state"] == "published":
            published.add(_key(tenant))

    def semantic(data):
        from .inventory_v3 import inventory_discovery

        keys = {_key(w) for w in data["workspaces"]}
        keys.update(_key(s) for s in data["shares"])
        if keys and keys.issubset(published):
            # The immutable frozen mapping must agree, but live tenant bodies
            # are never re-inventoried by a retry of another account.
            for key in keys:
                if _without_counts(_evidence(data, key)) != _without_counts(
                    _evidence(manifest.data, key),
                ):
                    raise ValueError("published tenant metadata cannot change")
            return {
                "blockers": [],
                "workspaces": [
                    {
                        "sessions": range(
                            next(
                                old
                                for old in manifest.data["workspaces"]
                                if old["position"] == w["position"]
                            )["counts"]["sessions"],
                        ),
                        "chats": range(
                            next(
                                old
                                for old in manifest.data["workspaces"]
                                if old["position"] == w["position"]
                            )["counts"]["chats"],
                        ),
                    }
                    for w in data["workspaces"]
                ],
            }
        return inventory_discovery(data)

    current = await asyncio.to_thread(
        discover_services,
        reviewed_report["declared_services"],
        shares=reviewed_report["source_shares"],
        overrides=reviewed_report.get("overrides", []),
        tenant_isolation=True,
        semantic_validator=semantic,
    )
    if current != reviewed_report:
        raise ValueError(
            "review report differs from current complete discovery",
        )
    old = manifest.data
    if (
        current["services"] != old["services"]
        or current["declared_services"] != old["declared_services"]
    ):
        raise ValueError(
            "tenant review cannot change services or storage roots",
        )
    old_tenants = {_key(t): t for t in old["tenants"]}
    new_tenants = {_key(t): t for t in current["tenants"]}
    if set(old_tenants) != set(new_tenants):
        raise ValueError("tenant review cannot add or remove tenants")
    if current["source_shares"] != old["source_shares"]:
        raise ValueError("tenant review cannot change original share records")
    changed = {
        k
        for k in old_tenants
        if _evidence(old, k) != _evidence(current, k)
        or old_tenants[k] != new_tenants[k]
    }
    if not changed:
        raise ValueError("tenant review contains no changed tenant evidence")
    revision = str(uuid4())
    new = {
        **old,
        **{k: current[k] for k in _FIELDS if k in current},
        "tenant_review_revision": revision,
    }
    from .freshness import tenant_key

    cutoff = time.time_ns()
    new["tenant_sync_started_at_ns"] = {
        **old.get("tenant_sync_started_at_ns", {}),
        **{tenant_key(*key): cutoff for key in changed},
    }
    new["source_change_blockers"] = [
        blocker
        for blocker in old.get("source_change_blockers", [])
        if tuple(blocker.get("scope", [])) not in changed
    ]
    await _commit_review(
        db,
        manifest,
        old,
        new,
        changed,
        new_tenants,
        revision,
    )
    return await recover_pending_review(db, manifest)


async def _commit_review(
    db,
    manifest,
    old,
    new,
    changed,
    new_tenants,
    revision,
):
    old_tenants = {_key(t): t for t in old["tenants"]}
    old_source, new_source = _source(old), _source(new)
    states = TenantState(db, manifest.migration_id)
    async with db.transaction():
        job = await db.fetch_one(
            "SELECT * FROM " + JOBS + " WHERE migration_id=%s FOR UPDATE",
            (manifest.migration_id.encode(),),
        )
        if job is None or job["source_binding_text"] != old_source:
            raise ValueError("tenant review job binding changed")
        records = []
        for key in sorted(old_tenants):
            row = await states.get(*key, locked=True)
            if row is None:
                raise ValueError("tenant review requires registered tenants")
            if key in changed:
                if row["state"] not in ("failed", "discovered"):
                    raise ValueError(
                        "only failed or discovered tenants can be reviewed",
                    )
                if row["evidence_text"] != _json(_evidence(old, key)):
                    raise ValueError("tenant review old evidence differs")
                if new_tenants[key]["state"] != "discovered":
                    raise ValueError(
                        "reviewed tenant still has discovery blockers",
                    )
                records.append(
                    {
                        "service": key[0],
                        "tenant": key[1],
                        "old": row["evidence_text"],
                        "new": _json(_evidence(new, key)),
                        "old_state": row["state"],
                    },
                )
        await _no_data(
            db,
            manifest.migration_id,
            changed,
            old["workspaces"],
            old["shares"],
        )
        files = {}
        for label, data in (("old", old), ("new", new)):
            path = manifest.path.with_name(
                manifest.path.name
                + ".review-"
                + revision
                + "."
                + label
                + ".json",
            )
            _write(path, data)
            files[label] = {"name": path.name, "sha256": _digest(data)}
        pending = {
            "version": 1,
            "migration_id": manifest.migration_id,
            "revision": revision,
            "files": files,
            "old_source": old_source,
            "new_source": new_source,
            "tenants": records,
        }
        _write(_pending(manifest), pending)
        verification = json.loads(job["verification_text"] or "{}")
        history = verification.setdefault("tenant_reviews", [])
        history.append(
            {
                "revision": revision,
                "old_source_sha256": sha256(old_source.encode()).hexdigest(),
                "new_source_sha256": sha256(new_source.encode()).hexdigest(),
                "tenants": [list(k) for k in sorted(changed)],
            },
        )
        verification["tenant_review_revision"] = revision
        changed_rows = await db.execute(
            "UPDATE "
            + JOBS
            + " SET source_binding_text=%s,verification_text=%s,updated_at=UTC_TIMESTAMP(6) WHERE migration_id=%s AND source_binding_text=%s",
            (
                new_source,
                _json(verification),
                manifest.migration_id.encode(),
                old_source,
            ),
        )
        if changed_rows != 1:
            raise ValueError("tenant review job compare-and-set failed")
        for record in records:
            changed_rows = await db.execute(
                "UPDATE swe_conversation_migration_tenants SET evidence_text=%s,state='discovered',failure_text=NULL,verification_text='{}',updated_at=UTC_TIMESTAMP(6) WHERE migration_id=%s AND service_unit_key=%s AND tenant_key=%s AND state=%s AND evidence_text=%s",
                (
                    record["new"],
                    manifest.migration_id.encode(),
                    record["service"].encode(),
                    record["tenant"].encode(),
                    record["old_state"],
                    record["old"],
                ),
            )
            if changed_rows != 1:
                raise ValueError(
                    "tenant review evidence compare-and-set failed",
                )


async def recover_pending_review(db, manifest):
    """Resolve only provable old/new SQL outcomes; leave unknown outcomes intact."""
    path = _pending(manifest)
    if not path.exists():
        return None
    if path.is_symlink():
        raise ValueError("tenant review pending file cannot be a symlink")
    pending = json.loads(path.read_text())
    if pending["migration_id"] != manifest.migration_id:
        raise ValueError("tenant review migration differs")
    async with db.transaction():
        job = await db.fetch_one(
            "SELECT * FROM " + JOBS + " WHERE migration_id=%s FOR UPDATE",
            (manifest.migration_id.encode(),),
        )
        if job is None:
            raise ValueError("tenant review job is missing")
        verification = json.loads(job["verification_text"] or "{}")
        if (
            job["source_binding_text"] == pending["new_source"]
            and verification.get("tenant_review_revision")
            == pending["revision"]
        ):
            outcome = "new"
        elif (
            job["source_binding_text"] == pending["old_source"]
            and verification.get("tenant_review_revision")
            != pending["revision"]
        ):
            outcome = "old"
        else:
            raise ValueError("tenant review SQL outcome is unknown")
        states = TenantState(db, manifest.migration_id)
        for record in pending["tenants"]:
            row = await states.get(
                record["service"],
                record["tenant"],
                locked=True,
            )
            expected_state = (
                "discovered" if outcome == "new" else record["old_state"]
            )
            if (
                row is None
                or row["evidence_text"] != record[outcome]
                or row["state"] != expected_state
            ):
                raise ValueError("tenant review SQL evidence outcome differs")
        snapshot = pending["files"][outcome]
        if Path(snapshot["name"]).name != snapshot["name"]:
            raise ValueError("tenant review snapshot path is unsafe")
        source = path.parent / snapshot["name"]
        if source.is_symlink():
            raise ValueError("tenant review snapshot cannot be a symlink")
        restored = V3Manifest.load(source)
        if (
            restored.migration_id != manifest.migration_id
            or _digest(restored.data) != snapshot["sha256"]
            or _source(restored.data) != pending[outcome + "_source"]
        ):
            raise ValueError("tenant review snapshot evidence differs")
        # A revision may repair either pre/post-commit manifest, never overwrite
        # unrelated later edits or a manifest belonging to another revision.
        if _source(manifest.data) not in (
            pending["old_source"],
            pending["new_source"],
        ):
            raise ValueError("tenant review local manifest diverged")
        manifest.data = restored.data
        manifest._persist()
        archive = path.with_name(
            manifest.path.name
            + ".review-"
            + pending["revision"]
            + "."
            + outcome
            + ".audit.json",
        )
        if archive.exists():
            if archive.read_bytes() != path.read_bytes():
                raise ValueError("tenant review audit archive differs")
        else:
            os.link(path, archive)
        path.unlink()
        _sync(path.parent)
    return {"revision": pending["revision"], "outcome": outcome}
