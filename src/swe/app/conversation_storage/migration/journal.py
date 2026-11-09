# -*- coding: utf-8 -*-
"""DML-only authoritative mappings in DBA-precreated control tables."""

import json
from ..bundle import BundleStore, new_operation_key

JOBS = "swe_conversation_migration_jobs"
ENTRIES = "swe_conversation_migration_entries"
TICKET_KINDS = frozenset({"session", "chat", "turn", "message", "publication"})
ENTITY_TABLES = {
    kind: "swe_conversation_" + kind + "s" for kind in TICKET_KINDS
}
BUSINESS_TABLES = (*ENTITY_TABLES.values(), "swe_conversation_retentions")
JOB_TRANSITIONS = {
    "planned": {"importing", "blocked"},
    "importing": {"imported", "blocked"},
    "imported": {"importing", "verified", "blocked"},
    "verified": {"importing", "ready", "blocked"},
    "blocked": {"importing", "imported", "verified"},
    "ready": set(),
}


def _text(value):
    return value.decode() if isinstance(value, bytes) else value


def _json(value):
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )


class MigrationJournal:
    def __init__(self, database, manifest, *, server_uuid=None):
        if manifest.data.get("version") != 3:
            raise ValueError("SQL journal requires manifest version 3")
        self.db, self.manifest = database, manifest
        self.server_uuid = server_uuid
        self.migration_id = manifest.migration_id.encode()

    def _bindings(self):
        data = self.manifest.data
        if data.get("target") is None:
            raise ValueError(
                "migration target must be bound before preparing SQL journal",
            )
        source = {
            key: data[key]
            for key in (
                "source_boundary",
                "services",
                "workspaces",
                "fingerprints",
                "tenant_sync_started_at_ns",
                "shares",
                "discovery_version",
            )
            if key in data
        }
        target = dict(data["target"])
        if self.server_uuid is not None:
            target["server_uuid"] = self.server_uuid
        return _json(target), _json(source)

    async def prepare(self):
        target, source = self._bindings()
        async with self.db.transaction():
            jobs = await self.db.fetch_all(
                "SELECT * FROM " + JOBS + " FOR UPDATE",
            )
            current = next(
                (
                    row
                    for row in jobs
                    if row["migration_id"] == self.migration_id
                ),
                None,
            )
            if any(row["migration_id"] != self.migration_id for row in jobs):
                raise ValueError(
                    "another migration job already owns this application schema",
                )
            if current is None:
                for table in BUSINESS_TABLES:
                    if await self.db.fetch_one(
                        "SELECT id FROM " + table + " LIMIT 1",
                    ):
                        raise ValueError(
                            "first migration requires six empty business tables; unknown rows found",
                        )
                await self.db.execute(
                    "INSERT INTO "
                    + JOBS
                    + " (migration_id,manifest_version,state,target_binding_text,source_binding_text,created_at,updated_at) VALUES (%s,3,'planned',%s,%s,UTC_TIMESTAMP(6),UTC_TIMESTAMP(6))",
                    (self.migration_id, target, source),
                )
            elif (
                current["target_binding_text"] != target
                or current["source_binding_text"] != source
                or current["manifest_version"] != 3
            ):
                raise ValueError("migration source or target binding changed")
            elif current["state"] == "ready":
                raise ValueError(
                    "ready migration cannot resume offline import",
                )
        await self.hydrate()
        await self.validate_ownership()
        return current

    async def validate_ownership(self):
        for kind, table in ENTITY_TABLES.items():
            unknown = await self.db.fetch_one(
                "SELECT b.id FROM "
                + table
                + " b LEFT JOIN "
                + ENTRIES
                + " e ON e.migration_id=%s AND e.entry_kind=%s AND e.target_id=b.id AND e.target_id=e.id WHERE e.id IS NULL LIMIT 1",
                (self.migration_id, kind.encode()),
            )
            if unknown:
                raise ValueError(
                    "unknown existing business row is not owned by this migration",
                )
        for row in await self.db.fetch_all(
            "SELECT * FROM swe_conversation_retentions",
        ):
            await self._validate_retention(row)

    async def _validate_retention(self, row):
        scope_keys = (
            "service_unit_key",
            "tenant_key",
            "source_key",
            "agent_key",
        )
        scope = tuple(row[key] for key in scope_keys)
        known = {
            tuple(
                item[key].encode()
                for key in (
                    "service_unit_id",
                    "tenant_id",
                    "source_id",
                    "agent_id",
                )
            )
            for item in self.manifest.data["workspaces"]
        }
        legacy = (
            row["scope_kind"] == "legacy_share"
            and not row["source_key"]
            and not row["agent_key"]
            and any(
                item["service_unit_id"].encode() == scope[0]
                and item.get("record", item)["tenant_id"].encode() == scope[1]
                for item in self.manifest.data.get("shares", [])
            )
        )
        if scope not in known and not legacy:
            raise ValueError("unknown retention workspace ownership")
        if row.get("publication_id") is not None:
            owner = await self.db.fetch_one(
                "SELECT p.* FROM swe_conversation_publications p JOIN "
                + ENTRIES
                + " e ON e.target_id=p.id AND e.id=e.target_id WHERE e.migration_id=%s AND e.entry_kind='publication' AND p.id=%s",
                (self.migration_id, row["publication_id"]),
            )
            if owner is None or any(
                owner[key] != row[key] for key in (*scope_keys, "scope_kind")
            ):
                raise ValueError(
                    "retention publication is not owned by this migration scope",
                )
            if row["kind"] == "dependency":
                key = _text(row["retention_key"])
                if not key.startswith("dep:") or not key[4:].isdigit():
                    raise ValueError("invalid retention dependency key")
                source_id = int(key[4:])
                source = await self.db.fetch_one(
                    "SELECT p.* FROM swe_conversation_publications p JOIN "
                    + ENTRIES
                    + " e ON e.target_id=p.id AND e.id=e.target_id WHERE e.migration_id=%s AND e.entry_kind='publication' AND p.id=%s",
                    (self.migration_id, source_id),
                )
                if (
                    source is None
                    or source_id <= row["publication_id"]
                    or any(
                        source[key] != row[key]
                        for key in (*scope_keys, "scope_kind")
                    )
                ):
                    raise ValueError(
                        "retention dependency source is not owned by this migration",
                    )
        elif row["kind"] != "gc_guard":
            raise ValueError("retention without a publication is not a guard")

    @staticmethod
    def _key(kind, position):
        if (
            not isinstance(position, str)
            or not position
            or len(position.encode()) > 2048
        ):
            raise ValueError(
                "stable source key must contain 1 to 2048 UTF-8 bytes",
            )
        return kind.encode(), position.encode()

    async def allocate(self, kind, position):
        if kind not in TICKET_KINDS:
            raise ValueError(
                "only five entity kinds may allocate integer tickets",
            )
        kind_key, key = self._key(kind, position)
        async with self.db.transaction():
            row = await self.db.fetch_one(
                "SELECT * FROM "
                + ENTRIES
                + " WHERE migration_id=%s AND entry_kind=%s AND source_key=%s FOR UPDATE",
                (self.migration_id, kind_key, key),
            )
            if row is None:
                operation = new_operation_key()
                identity = await self.db.insert(
                    "INSERT INTO "
                    + ENTRIES
                    + " (migration_id,entry_kind,source_key,operation_key,state,created_at,updated_at) VALUES (%s,%s,%s,%s,'mapped',UTC_TIMESTAMP(6),UTC_TIMESTAMP(6))",
                    (self.migration_id, kind_key, key, operation.encode()),
                )
                await self.db.execute(
                    "UPDATE "
                    + ENTRIES
                    + " SET target_id=%s WHERE id=%s AND migration_id=%s",
                    (identity, identity, self.migration_id),
                )
                row = {
                    "id": identity,
                    "target_id": identity,
                    "operation_key": operation,
                }
            assignment = self._assignment(row)
        self.manifest.data["assignments"].setdefault(kind, {})[
            position
        ] = assignment
        self.manifest._persist()
        return dict(assignment)

    @staticmethod
    def _assignment(row):
        if row["id"] != row["target_id"] or not row["target_id"]:
            raise ValueError("SQL journal integer ticket mapping is invalid")
        operation = _text(row["operation_key"])
        return {
            "target_id": row["target_id"],
            "operation_key": operation,
            "relative_path": BundleStore.path_for(operation),
        }

    async def hydrate(self, *, persist=True):
        rows = await self.db.fetch_all(
            "SELECT * FROM " + ENTRIES + " WHERE migration_id=%s ORDER BY id",
            (self.migration_id,),
        )
        assignments, batches = {}, {}
        for row in rows:
            kind, key = _text(row["entry_kind"]), _text(row["source_key"])
            if kind in TICKET_KINDS:
                assignments.setdefault(kind, {})[key] = self._assignment(row)
            elif kind == "batch":
                batches[key] = (
                    "importing" if row["state"] == "prepared" else row["state"]
                )
        for kind, values in self.manifest.data["assignments"].items():
            if any(
                assignments.get(kind, {}).get(key) != value
                for key, value in values.items()
            ):
                raise ValueError(
                    "external mapping has no matching durable SQL ticket",
                )
        self.manifest.data["assignments"], self.manifest.data["batches"] = (
            assignments,
            batches,
        )
        if persist:
            self.manifest._persist()

    async def set_batch_state(self, position, state, *, mirror=True):
        from .manifest import TRANSITIONS

        kind, key = self._key("batch", position)
        sql_state = "prepared" if state == "importing" else state
        async with self.db.transaction():
            row = await self.db.fetch_one(
                "SELECT * FROM "
                + ENTRIES
                + " WHERE migration_id=%s AND entry_kind=%s AND source_key=%s FOR UPDATE",
                (self.migration_id, kind, key),
            )
            previous = (
                "planned"
                if row is None
                else (
                    "importing" if row["state"] == "prepared" else row["state"]
                )
            )
            if state != previous and state not in TRANSITIONS[previous]:
                raise ValueError("invalid migration batch transition")
            if row is None:
                await self.db.execute(
                    "INSERT INTO "
                    + ENTRIES
                    + " (migration_id,entry_kind,source_key,state,created_at,updated_at) VALUES (%s,%s,%s,%s,UTC_TIMESTAMP(6),UTC_TIMESTAMP(6))",
                    (self.migration_id, kind, key, sql_state),
                )
            else:
                await self.db.execute(
                    "UPDATE "
                    + ENTRIES
                    + " SET state=%s,updated_at=UTC_TIMESTAMP(6) WHERE id=%s AND migration_id=%s",
                    (sql_state, row["id"], self.migration_id),
                )
        if mirror:
            self.manifest.data["batches"][position] = state
            self.manifest._persist()

    async def record_share(self, token, before, expected):
        # Called while the original share is row-locked in publication commit.
        kind, key = self._key("share", token)
        encode = lambda values: [_text(value) for value in values]
        async with self.db.transaction():
            row = await self.db.fetch_one(
                "SELECT * FROM "
                + ENTRIES
                + " WHERE migration_id=%s AND entry_kind=%s AND source_key=%s FOR UPDATE",
                (self.migration_id, kind, key),
            )
            if row is None:
                if any(value is not None for value in before):
                    raise ValueError("unknown existing share extension values")
                details = _json(
                    {"before": encode(before), "expected": encode(expected)},
                )
                await self.db.execute(
                    "INSERT INTO "
                    + ENTRIES
                    + " (migration_id,entry_kind,source_key,state,target_id,details_text,created_at,updated_at) VALUES (%s,%s,%s,'committed',%s,%s,UTC_TIMESTAMP(6),UTC_TIMESTAMP(6))",
                    (self.migration_id, kind, key, expected[1], details),
                )
            else:
                details = json.loads(row["details_text"])
                if details["expected"] != encode(expected) or encode(
                    before,
                ) not in (
                    details["before"],
                    details["expected"],
                ):
                    raise ValueError(
                        "share locator differs from this migration journal",
                    )

    async def record_retention(self, retention):
        await self._validate_retention(retention)
        keys = (
            "scope_kind",
            "service_unit_key",
            "tenant_key",
            "source_key",
            "agent_key",
            "retention_key",
            "member_key",
            "kind",
            "publication_id",
            "guard_marker",
        )
        details = {key: _text(retention.get(key)) for key in keys}
        position = _json([details[key] for key in keys[:8]])
        kind, key = self._key("retention", position)
        async with self.db.transaction():
            row = await self.db.fetch_one(
                "SELECT * FROM "
                + ENTRIES
                + " WHERE migration_id=%s AND entry_kind=%s AND source_key=%s FOR UPDATE",
                (self.migration_id, kind, key),
            )
            if row is None:
                await self.db.execute(
                    "INSERT INTO "
                    + ENTRIES
                    + " (migration_id,entry_kind,source_key,state,target_id,details_text,created_at,updated_at) VALUES (%s,%s,%s,'committed',%s,%s,UTC_TIMESTAMP(6),UTC_TIMESTAMP(6))",
                    (
                        self.migration_id,
                        kind,
                        key,
                        retention["id"],
                        _json(details),
                    ),
                )
            elif (
                row["target_id"] != retention["id"]
                or json.loads(row["details_text"]) != details
            ):
                raise ValueError("retention identity or business key changed")

    async def capture_retentions(self):
        for row in await self.db.fetch_all(
            "SELECT * FROM swe_conversation_retentions",
        ):
            await self.record_retention(row)

    async def mark_state(self, state, verification=None):
        async with self.db.transaction():
            row = await self.db.fetch_one(
                "SELECT * FROM " + JOBS + " WHERE migration_id=%s FOR UPDATE",
                (self.migration_id,),
            )
            if row is None or (
                state != row["state"]
                and state not in JOB_TRANSITIONS[row["state"]]
            ):
                raise ValueError("invalid migration job transition")
            await self.db.execute(
                "UPDATE "
                + JOBS
                + " SET state=%s,verification_text=COALESCE(%s,verification_text),updated_at=UTC_TIMESTAMP(6) WHERE migration_id=%s",
                (
                    state,
                    None if verification is None else _json(verification),
                    self.migration_id,
                ),
            )
