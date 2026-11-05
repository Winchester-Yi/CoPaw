# -*- coding: utf-8 -*-
"""Consistent metadata and immutable-body backups with explicit job protection."""

import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
import json
import os
from pathlib import Path
from operator import itemgetter
import shutil
import re

from .bundle import BundleStore, new_operation_key
from .chat_mutations import load_metadata_json
from .errors import ConflictError, IntegrityError
from .identity import WorkspaceScope, SCOPE_COLUMNS, service_unit_bytes
from .retention import MAX_PIN_BATCH, RetentionStore

BACKUP_SETTLE_SECONDS = 30

TABLES = {
    name: "swe_conversation_" + name
    for name in (
        "sessions",
        "chats",
        "turns",
        "publications",
        "messages",
        "retentions",
    )
}
TABLES["shares"] = "swe_chat_shares"
REFERENCE_FIELDS = {
    "sessions": ("recovery_publication_id",),
    "chats": (
        "content_meta_publication_id",
        "active_checkpoint_publication_id",
    ),
    "turns": ("admission_publication_id", "terminal_publication_id"),
    "messages": ("publication_id", "archive_publication_id"),
    "shares": ("publication_id",),
}


@dataclass(frozen=True)
class RestoreEvidence:
    source_writers_stopped_ref: str
    isolated_target_ref: str

    def __post_init__(self):
        if not all(
            isinstance(value, str) and value.strip()
            for value in (
                self.source_writers_stopped_ref,
                self.isolated_target_ref,
            )
        ):
            raise ValueError(
                "restore requires stopped-source and isolated-target evidence",
            )


def _restore_target(database, source_schema):
    from ...database.config import get_database_config

    schema = database.config.database
    if schema in (
        source_schema,
        get_database_config().database,
    ) or not re.fullmatch(
        r"test_conversation_restore_[a-f0-9]{32}|conversation_staging_[a-f0-9]{32}",
        schema,
    ):
        raise ValueError(
            "restore requires a generated isolated staging schema",
        )


def _encode(value):
    if isinstance(value, bytes):
        return {"$bytes": value.hex()}
    if isinstance(value, datetime):
        return {"$datetime": value.isoformat()}
    if isinstance(value, dict):
        return {key: _encode(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_encode(item) for item in value]
    return value


def _decode(value):
    if isinstance(value, dict):
        if set(value) == {"$bytes"}:
            return bytes.fromhex(value["$bytes"])
        if set(value) == {"$datetime"}:
            return datetime.fromisoformat(value["$datetime"])
        return {key: _decode(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_decode(item) for item in value]
    return value


def encode_metadata(value):
    return json.dumps(
        _encode(value),
        ensure_ascii=False,
        separators=(",", ":"),
        allow_nan=False,
    )


def decode_metadata(value):
    return _decode(load_metadata_json(value))


def _effective_message(row, chats):
    if row.get("deleted_at") is not None:
        return False
    if row["layer"] != "archive":
        return row["current_visible"] == 1
    chat = chats.get(row["archive_chat_id"])
    if chat is None:
        raise IntegrityError("backup archive Message owner Chat is missing")
    return chat["deleted_at"] is None


def _effective_reference_ids(snapshot):
    references = set()
    chats = {row["id"]: row for row in snapshot["chats"]}
    for table, fields in REFERENCE_FIELDS.items():
        for row in snapshot[table]:
            if table == "chats" and row["deleted_at"] is not None:
                continue
            if table == "messages" and not _effective_message(row, chats):
                continue
            references.update(
                row[key] for key in fields if row.get(key) is not None
            )
    references.update(
        row["publication_id"]
        for row in snapshot["retentions"]
        if row["state"] == "active" and row.get("publication_id") is not None
    )
    references.update(
        row["id"]
        for row in snapshot["publications"]
        if row.get("retained_history")
    )
    return references


def snapshot_publication_ids(snapshot):
    publications = {row["id"]: row for row in snapshot["publications"]}
    if len(publications) != len(snapshot["publications"]):
        raise IntegrityError("backup has duplicate publication identities")
    if any(row["state"] == "prepared" for row in publications.values()):
        raise IntegrityError(
            "backup requires original prepared operations to settle",
        )
    locators = {
        row[key]
        for table, fields in REFERENCE_FIELDS.items()
        for row in snapshot[table]
        for key in fields
        if row.get(key) is not None
    }
    if any(
        identity not in publications
        or publications[identity]["state"] != "committed"
        or publications[identity]["gc_state"]
        not in ("live", "deleting", "reclaimed")
        for identity in locators
    ):
        raise IntegrityError(
            "backup locator publication is missing or not committed",
        )
    live = {
        identity
        for identity, row in publications.items()
        if row["state"] == "committed" and row["gc_state"] == "live"
    }
    if _effective_reference_ids(snapshot) - live:
        raise IntegrityError(
            "backup locator or dependency is missing or not committed/live",
        )
    return sorted(live)


def _new_directory(path):
    path = Path(path)
    if path.exists() and (
        path.is_symlink() or not path.is_dir() or any(path.iterdir())
    ):
        raise ValueError("backup destination must be an empty directory")
    path.mkdir(parents=True, exist_ok=True)
    return path.resolve()


def _atomic_json(path, value):
    from ..runner.turn_ledger import atomic_json

    atomic_json(path, _encode(value))


def _copy_bundle(source, target_root, row):
    relative = row["relative_path"].decode()
    index = source.inspect(relative, expected_length=row["byte_length"])
    if index.metadata.get("operation_key") != row["operation_key"].decode():
        raise IntegrityError("backup bundle operation identity mismatch")
    target_store = BundleStore(target_root)
    with target_store._directory(relative, create=True) as (directory, name):
        descriptor = os.open(
            name,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            0o600,
            dir_fd=directory,
        )
        with (
            source._open(relative) as incoming,
            os.fdopen(descriptor, "wb") as outgoing,
        ):
            shutil.copyfileobj(incoming, outgoing, 65536)
            outgoing.flush()
            os.fsync(outgoing.fileno())
        os.fsync(directory)
    target_store.confirm_durable(relative)


def _scope_key(row):
    return (
        row["scope_kind"],
        row["service_unit_key"],
        row["tenant_key"],
        row["source_key"],
        row["agent_key"],
    )


def _pin_members(document):
    scope = WorkspaceScope.from_values(document["scope"])
    namespaces = (
        ("legacy_share", *scope.sql_values[:2], b"", b""),
        ("workspace", *scope.sql_values),
    )
    live = set(snapshot_publication_ids(document["snapshot"]))
    result = {}
    for namespace, suffix in zip(namespaces, (":l:", ":w:")):
        ids = sorted(
            row["id"]
            for row in document["snapshot"]["publications"]
            if row["id"] in live and _scope_key(row) == namespace
        )
        for start in range(0, len(ids), MAX_PIN_BATCH):
            result[document["backup_id"] + suffix + str(start)] = (
                namespace,
                ids[start : start + MAX_PIN_BATCH],
            )
    return result


def _validate_associations(snapshot):
    publications = {row["id"]: row for row in snapshot["publications"]}
    for table, fields in REFERENCE_FIELDS.items():
        for row in snapshot[table]:
            for key in fields:
                if row.get(key) is not None and publications[row[key]].get(
                    "service_unit_key",
                ) != row.get("service_unit_key"):
                    raise IntegrityError("backup publication service mismatch")
    sessions = {row["id"]: row for row in snapshot["sessions"]}
    chats = {row["id"]: row for row in snapshot["chats"]}
    turns = {row["id"]: row for row in snapshot["turns"]}
    for table, rows in (
        ("chats", chats.values()),
        ("turns", turns.values()),
        ("messages", snapshot["messages"]),
    ):
        for row in rows:
            parent = sessions.get(row["session_id"])
            if parent is not None and row.get(
                "service_unit_key",
            ) != parent.get("service_unit_key"):
                raise IntegrityError("backup parent service mismatch")
    for row in snapshot.get("retentions", []):
        if row.get("publication_id") is not None and row.get(
            "service_unit_key",
        ) != publications[row["publication_id"]].get("service_unit_key"):
            raise IntegrityError("backup retention service mismatch")
    for chat in chats.values():
        session = sessions.get(chat["session_id"])
        if session is None:
            raise IntegrityError("backup Chat Session identity mismatch")
        identity_fields = (
            "service_unit_key",
            "tenant_key",
            "source_key",
            "agent_key",
            "user_key",
            "channel_key",
        )
        if any(chat[key] != session[key] for key in identity_fields):
            raise IntegrityError("backup Chat Session identity mismatch")
    for turn in turns.values():
        chat = chats.get(turn["chat_id"])
        if (
            chat is None
            or turn["session_id"] not in sessions
            or any(
                chat[key] != sessions[turn["session_id"]][key]
                for key in SCOPE_COLUMNS
            )
        ):
            raise IntegrityError("backup Turn association mismatch")
    publications = {row["id"]: row for row in snapshot["publications"]}
    _validate_publication_associations(publications, sessions, chats, turns)
    _validate_message_associations(
        snapshot,
        sessions,
        chats,
        turns,
        publications,
    )
    for table, fields in REFERENCE_FIELDS.items():
        if table == "shares":
            continue
        for row in snapshot[table]:
            session_id = (
                row["id"] if table == "sessions" else row["session_id"]
            )
            for key in fields:
                if (
                    row.get(key) is not None
                    and publications[row[key]]["session_id"] != session_id
                ):
                    publication = publications[row[key]]
                    session = sessions[session_id]
                    if (
                        table == "chats"
                        and publication["chat_id"] == row["id"]
                        and publication["session_id"] in sessions
                        and all(
                            publication[field] == row[field]
                            for field in SCOPE_COLUMNS
                        )
                    ):
                        continue
                    if (
                        table == "chats"
                        and key == "content_meta_publication_id"
                        and publication["kind"] == "offline_import"
                        and publication["session_id"] is None
                        and _scope_key(publication)
                        == (
                            "workspace",
                            session["service_unit_key"],
                            session["tenant_key"],
                            session["source_key"],
                            session["agent_key"],
                        )
                    ):
                        continue
                    raise IntegrityError(
                        "backup locator publication Session differs",
                    )


def _validate_message_associations(
    snapshot,
    sessions,
    chats,
    turns,
    publications,
):
    for message in snapshot["messages"]:
        if message["session_id"] not in sessions:
            raise IntegrityError("backup Message Session is missing")
        for key in ("origin_chat_id", "archive_chat_id"):
            identity = message[key]
            if identity is not None and (
                identity not in chats
                or any(
                    chats[identity][key]
                    != sessions[message["session_id"]][key]
                    for key in SCOPE_COLUMNS
                )
            ):
                raise IntegrityError(
                    "backup Message Chat association mismatch",
                )
        if message["turn_id"] is not None and (
            message["turn_id"] not in turns
            or turns[message["turn_id"]]["session_id"] != message["session_id"]
        ):
            raise IntegrityError("backup Message Turn association mismatch")
        if (
            publications[message["publication_id"]]["session_id"]
            != message["session_id"]
        ):
            raise IntegrityError(
                "backup Message payload publication Session differs",
            )


def _validate_publication_associations(publications, sessions, chats, turns):
    for row in publications.values():
        if row["scope_kind"] == "legacy_share":
            if any(
                row[key] is not None
                for key in ("session_id", "chat_id", "turn_id")
            ):
                raise IntegrityError(
                    "backup legacy share has workspace associations",
                )
            continue
        if row["session_id"] is not None:
            session = sessions.get(row["session_id"])
            if session is None or _scope_key(row)[1:] != tuple(
                session[key] for key in SCOPE_COLUMNS
            ):
                raise IntegrityError(
                    "backup publication Session scope mismatch",
                )
        if row["chat_id"] is not None and (
            row["chat_id"] not in chats
            or any(
                chats[row["chat_id"]][key] != row[key] for key in SCOPE_COLUMNS
            )
        ):
            raise IntegrityError(
                "backup publication Chat association mismatch",
            )
        if row["turn_id"] is not None and (
            row["turn_id"] not in turns
            or turns[row["turn_id"]]["chat_id"] != row["chat_id"]
            or turns[row["turn_id"]]["session_id"] != row["session_id"]
        ):
            raise IntegrityError(
                "backup publication Turn association mismatch",
            )


def validate_backup(snapshot, bundles):
    ids = snapshot_publication_ids(snapshot)
    _validate_associations(snapshot)
    publications = {row["id"]: row for row in snapshot["publications"]}
    indexes = {}
    for identity in ids:
        row = publications[identity]
        index = bundles.inspect(
            row["relative_path"].decode(),
            expected_length=row["byte_length"],
        )
        if (
            index.metadata.get("operation_key")
            != row["operation_key"].decode()
        ):
            raise IntegrityError("backup bundle operation identity mismatch")
        for extent in index.blocks.values():
            for _ in bundles.iter_extent(
                index.relative_path,
                extent.offset,
                extent.length,
                expected_length=index.byte_length,
            ):
                pass
        indexes[identity] = index
    _validate_extents(snapshot, indexes)
    _validate_bodies(snapshot, bundles, indexes)
    return {
        "passed": True,
        "publications": len(ids),
        "rows": {table: len(snapshot[table]) for table in TABLES},
    }


def _validate_snapshot_scopes(snapshot, scope):
    for rows in snapshot.values():
        if any(
            row.get("service_unit_key") != scope.sql_values[0] for row in rows
        ):
            raise IntegrityError("backup contains a different service")
    allowed = {
        ("workspace", *scope.sql_values),
        ("legacy_share", *scope.sql_values[:2], b"", b""),
    }
    for table in ("publications", "retentions"):
        if any(_scope_key(row) not in allowed for row in snapshot[table]):
            raise IntegrityError(
                "backup contains a different storage namespace",
            )
    for table in ("sessions", "chats"):
        if any(
            tuple(row[key] for key in SCOPE_COLUMNS) != scope.sql_values
            for row in snapshot[table]
        ):
            raise IntegrityError("backup contains a different workspace scope")


def _validate_extents(snapshot, indexes):
    specs = {
        "sessions": (
            ("recovery_publication_id", "recovery_offset", "recovery_length"),
        ),
        "chats": (
            (
                "content_meta_publication_id",
                "content_meta_offset",
                "content_meta_length",
            ),
        ),
        "messages": (("publication_id", "byte_offset", "byte_length"),),
        "shares": (("publication_id", "byte_offset", "byte_length"),),
    }
    for table, fields in specs.items():
        for row in snapshot[table]:
            for publication, offset, length in fields:
                if (
                    row[publication] is not None
                    and row[publication] in indexes
                ):
                    _extent(
                        indexes[row[publication]],
                        row[offset],
                        row[length],
                        kinds={
                            "sessions": {"recovery"},
                            "chats": {"content_meta"},
                            "shares": {"share"},
                            "messages": {
                                "message",
                                "memory",
                                "task",
                                "error",
                                "anchor",
                                "archive",
                            },
                        }[table],
                    )
    for row in snapshot["publications"]:
        if row["id"] not in indexes:
            continue
        for prefix in ("archive", "checkpoint", "event"):
            if row[prefix + "_offset"] is not None:
                _extent(
                    indexes[row["id"]],
                    row[prefix + "_offset"],
                    row[prefix + "_length"],
                    kinds={"events" if prefix == "event" else prefix},
                )


def _extent(index, offset, length, *, kinds=None):
    if not any(
        offset == block.offset
        and length == block.length
        and (kinds is None or block.kind in kinds)
        for block in index.blocks.values()
    ):
        raise IntegrityError(
            "backup locator is not an exact immutable payload block",
        )


def _read_payload(snapshot, bundles, publication_id, offset, length):
    publication = next(
        row for row in snapshot["publications"] if row["id"] == publication_id
    )
    return load_metadata_json(
        b"".join(
            bundles.iter_extent(
                publication["relative_path"].decode(),
                offset,
                length,
                expected_length=publication["byte_length"],
            ),
        ),
    )


def _recovery_payload(
    snapshot,
    bundles,
    publication_id,
    *,
    offset=None,
    length=None,
):
    from .session_projection import hydrate_state

    publications = {row["id"]: row for row in snapshot["publications"]}
    current = publications[publication_id]
    index = bundles.inspect(
        current["relative_path"].decode(),
        expected_length=current["byte_length"],
    )
    extent = (
        index.blocks.get("recovery")
        if offset is None
        else next(
            (
                block
                for block in index.blocks.values()
                if (block.offset, block.length) == (offset, length)
            ),
            None,
        )
    )
    if extent is None or extent.kind != "recovery":
        raise IntegrityError(
            "backup Turn publication has no complete recovery root",
        )
    value = _read_payload(
        snapshot,
        bundles,
        publication_id,
        extent.offset,
        extent.length,
    )

    def resolve(ref):
        target_id = ref.get("publication_id", publication_id)
        target = publications.get(target_id)
        if (
            target is None
            or target["session_id"] != current["session_id"]
            or (target_id != publication_id and target_id >= publication_id)
        ):
            raise IntegrityError(
                "backup projection dependency association differs",
            )
        if "message_id" in ref:
            row = next(
                (
                    row
                    for row in snapshot["messages"]
                    if row["id"] == ref["message_id"]
                ),
                None,
            )
            if row is None or (
                row["publication_id"],
                row["byte_offset"],
                row["byte_length"],
            ) != (target_id, ref["offset"], ref["length"]):
                raise IntegrityError("backup projection occurrence differs")
            return _read_payload(
                snapshot,
                bundles,
                target_id,
                ref["offset"],
                ref["length"],
            )
        if ref["operation_key"] != target["operation_key"].decode():
            raise IntegrityError("backup projection operation differs")
        target_index = bundles.inspect(target["relative_path"].decode())
        target_extent = target_index.blocks[ref["block_name"]]
        return _read_payload(
            snapshot,
            bundles,
            target_id,
            target_extent.offset,
            target_extent.length,
        )

    return hydrate_state(value, resolve)


def _validate_bodies(snapshot, bundles, indexes):
    from .shares import ShareStorage
    from ..chat_sharing.store import ChatShareStore
    from ..chat_sharing.models import ChatShareSnapshot

    publications = {row["id"]: row for row in snapshot["publications"]}
    for row in snapshot["shares"]:
        publication = publications[row["publication_id"]]
        if publication["kind"] not in ("share", "share_import"):
            raise IntegrityError(
                "backup share references an invalid publication kind",
            )
        ShareStorage._validate_record_publication(
            ChatShareStore._record_from_row(row),
            publication,
        )
        ChatShareSnapshot.model_validate(
            _read_payload(
                snapshot,
                bundles,
                row["publication_id"],
                row["byte_offset"],
                row["byte_length"],
            ),
        )
    for row in snapshot["sessions"]:
        if row["recovery_publication_id"] is not None:
            publication = publications[row["recovery_publication_id"]]
            index = indexes[publication["id"]]
            if not any(
                block.kind == "recovery"
                and (block.offset, block.length)
                == (row["recovery_offset"], row["recovery_length"])
                for block in index.blocks.values()
            ):
                raise IntegrityError(
                    "backup Session root has an invalid block kind",
                )
            _recovery_payload(
                snapshot,
                bundles,
                publication["id"],
                offset=row["recovery_offset"],
                length=row["recovery_length"],
            )
    for row in snapshot["turns"]:
        for key in ("admission_publication_id", "terminal_publication_id"):
            if row[key] is not None:
                publication = publications[row[key]]
                if (
                    publication["chat_id"] != row["chat_id"]
                    or publication["turn_id"] != row["id"]
                ):
                    raise IntegrityError(
                        "backup Turn root publication association differs",
                    )
                _validate_turn_recovery(
                    row,
                    _recovery_payload(snapshot, bundles, publication["id"]),
                    terminal=key == "terminal_publication_id",
                )


def _validate_turn_recovery(turn, state, *, terminal):
    states = state.get("turn_states", {})
    key = turn["question_msg_key"].decode()
    runtime = (
        turn["runtime_turn_key"].decode() if turn["runtime_turn_key"] else None
    )
    value = (
        states.get(key) or states.get(runtime)
        if isinstance(states, dict)
        else None
    )
    if not isinstance(value, dict):
        raise IntegrityError("backup Turn recovery root lacks complete state")
    if (
        terminal
        and turn["history_status"] is not None
        and value.get("status") != turn["history_status"]
    ):
        raise IntegrityError(
            "backup Turn terminal status differs from complete state",
        )


class ConversationBackup:
    def __init__(self, database, bundles, scope):
        self.db, self.bundles, self.scope = database, bundles, scope
        self.stores = (
            RetentionStore(
                database,
                WorkspaceScope(
                    scope.tenant_id,
                    "",
                    "",
                    service_unit_id=scope.service_unit_id,
                ),
                scope_kind="legacy_share",
            ),
            RetentionStore(database, scope),
        )

    async def _install_fences(self, keys, snapshot_ref):
        deadline = asyncio.get_running_loop().time() + BACKUP_SETTLE_SECONDS
        while True:
            async with self.db.transaction():
                for store in self.stores:
                    await store.lock_guard()
                pending = await self.db.fetch_all(
                    "SELECT id FROM swe_conversation_publications WHERE state='prepared' AND "
                    "((scope_kind='workspace' AND service_unit_key=%s AND tenant_key=%s AND source_key=%s AND agent_key=%s) "
                    "OR (scope_kind='legacy_share' AND service_unit_key=%s AND tenant_key=%s AND source_key=%s AND agent_key=%s)) "
                    "ORDER BY id FOR UPDATE",
                    (
                        *self.scope.sql_values,
                        *self.scope.sql_values[:2],
                        b"",
                        b"",
                    ),
                )
                if not pending:
                    for store, key in zip(self.stores, keys):
                        await store.install_backup_fence(key, snapshot_ref)
                    return
            if asyncio.get_running_loop().time() >= deadline:
                raise ConflictError(
                    "backup requires prepared original operations to settle",
                )
            await asyncio.sleep(0.05)

    async def _snapshot(self):
        async with self.db.transaction():
            isolation = await self.db.fetch_one(
                "SELECT @@transaction_isolation AS level",
            )
            if (
                isolation["level"].upper().replace("_", "-")
                != "REPEATABLE-READ"
            ):
                raise IntegrityError(
                    "backup snapshot requires REPEATABLE READ isolation",
                )
            values = self.scope.sql_values
            snapshot = {}
            for name in ("sessions", "chats", "publications", "retentions"):
                where = "service_unit_key=%s AND tenant_key=%s AND source_key=%s AND agent_key=%s"
                params = values
                if name in ("publications", "retentions"):
                    where = (
                        "("
                        + where
                        + " AND scope_kind='workspace') OR (service_unit_key=%s AND tenant_key=%s AND scope_kind='legacy_share' AND source_key=%s AND agent_key=%s)"
                    )
                    params = (*values, *values[:2], b"", b"")
                snapshot[name] = await self.db.fetch_all(
                    "SELECT * FROM "
                    + TABLES[name]
                    + " WHERE "
                    + where
                    + " ORDER BY id",
                    params,
                )
            for name in ("turns", "messages"):
                snapshot[name] = await self.db.fetch_all(
                    "SELECT r.* FROM "
                    + TABLES[name]
                    + " r JOIN swe_conversation_sessions s ON s.id=r.session_id WHERE r.service_unit_key=s.service_unit_key AND s.service_unit_key=%s AND s.tenant_key=%s AND s.source_key=%s AND s.agent_key=%s ORDER BY r.id",
                    values,
                )
            snapshot["shares"] = await self.db.fetch_all(
                "SELECT r.* FROM swe_chat_shares r JOIN swe_conversation_publications p ON p.id=r.publication_id WHERE r.service_unit_key=p.service_unit_key AND ((p.service_unit_key=%s AND p.tenant_key=%s AND p.source_key=%s AND p.agent_key=%s AND p.scope_kind=%s) OR (p.service_unit_key=%s AND p.tenant_key=%s AND p.source_key=%s AND p.agent_key=%s AND p.scope_kind=%s)) ORDER BY r.token",
                (*values, "workspace", *values[:2], b"", b"", "legacy_share"),
            )
            if await self.db.fetch_one(
                "SELECT token FROM swe_chat_shares r LEFT JOIN swe_conversation_publications p ON p.id=r.publication_id "
                "WHERE r.service_unit_key=%s AND r.tenant_id=%s AND (p.id IS NULL OR p.service_unit_key<>r.service_unit_key) LIMIT 1",
                (
                    self.scope.sql_values[0],
                    self.scope.tenant_id,
                ),
            ):
                raise IntegrityError(
                    "tenant share has a missing publication locator",
                )
            return snapshot

    async def capture(
        self,
        destination,
        *,
        backup_id=None,
        restore_verifier=None,
    ):
        destination = _new_directory(destination)
        backup_id = backup_id or new_operation_key()
        snapshot_ref = str(destination)
        keys = [backup_id + ":l", backup_id + ":w"]
        await self._install_fences(keys, snapshot_ref)
        snapshot = await self._snapshot()
        _validate_snapshot_scopes(snapshot, self.scope)
        _validate_associations(snapshot)
        ids = snapshot_publication_ids(snapshot)
        publications = {row["id"]: row for row in snapshot["publications"]}
        pin_keys = []
        for store, key in zip(self.stores, keys):
            members = [
                identity
                for identity in ids
                if _scope_key(publications[identity]) == store.scope_values
            ]
            for start in range(0, len(members), MAX_PIN_BATCH):
                pin_key = key + ":" + str(start)
                await store.pin(
                    members[start : start + MAX_PIN_BATCH],
                    kind="backup",
                    retention_key=pin_key,
                )
                changed = await self.db.execute(
                    "UPDATE swe_conversation_retentions SET snapshot_ref=%s WHERE service_unit_key=%s AND retention_key=%s AND kind=%s AND state=%s",
                    (
                        snapshot_ref.encode(),
                        self.scope.sql_values[0],
                        pin_key.encode(),
                        "backup",
                        "active",
                    ),
                )
                if changed != len(members[start : start + MAX_PIN_BATCH]):
                    raise ConflictError(
                        "backup pin snapshot reference was not confirmed",
                    )
                pin_keys.append(pin_key)
        document = {
            "version": 2,
            "service_unit_id": self.scope.service_unit_id,
            "backup_id": backup_id,
            "scope": list(self.scope.sql_values),
            "snapshot": snapshot,
            "pin_keys": pin_keys,
            "snapshot_ref": snapshot_ref,
            "source_schema": getattr(
                getattr(self.db, "config", None),
                "database",
                None,
            ),
        }
        from .migration.authority import backup_authority

        authority = await backup_authority(self.db, self.scope)
        if authority is not None:
            document.update(version=3, migration_authority=authority)
        await asyncio.to_thread(
            _atomic_json,
            destination / "metadata.json",
            document,
        )
        for store, key in zip(self.stores, keys):
            await store.release_backup_fence(key, snapshot_ref)
        target = destination / "nas"
        target.mkdir()
        for identity in ids:
            await asyncio.to_thread(
                _copy_bundle,
                self.bundles,
                target,
                publications[identity],
            )
        report = await asyncio.to_thread(
            validate_backup,
            snapshot,
            BundleStore(target),
        )
        report.update(
            version=2,
            service_unit_id=self.scope.service_unit_id,
            backup_id=backup_id,
            state="captured",
            pin_keys=pin_keys,
            content_verified=True,
            restore_verified=False,
            backup_ready=False,
            snapshot_ref=snapshot_ref,
        )
        if restore_verifier is not None:
            evidence = await restore_verifier(destination, backup_id)
            expected_evidence = {
                "version": 2,
                "service_unit_id": self.scope.service_unit_id,
                "passed": True,
                "complete_metadata_and_readers": True,
                "restore_ready": True,
                "needs_settlement": [],
                "backup_id": backup_id,
                "snapshot_ref": snapshot_ref,
                "scope": [value.decode() for value in self.scope.sql_values],
                "reader_counts": {
                    name: len(snapshot[name])
                    for name in ("sessions", "chats", "turns", "shares")
                },
            }
            if not isinstance(evidence, dict) or not evidence.get(
                "isolated_target_ref",
            ):
                raise IntegrityError(
                    "complete backup restore verification did not pass",
                )
            if (
                not evidence.get("source_writers_stopped_ref")
                or not evidence.get("target_nas_root")
                or not re.fullmatch(
                    r"test_conversation_restore_[a-f0-9]{32}|conversation_staging_[a-f0-9]{32}",
                    evidence.get("target_schema", ""),
                )
            ):
                raise IntegrityError(
                    "complete backup restore verification did not pass",
                )
            if any(
                evidence.get(key) != value
                for key, value in expected_evidence.items()
            ):
                raise IntegrityError(
                    "complete backup restore verification did not pass",
                )
            report.update(
                state="verified",
                restore_verified=True,
                backup_ready=True,
                restore_evidence=evidence,
            )
        await asyncio.to_thread(
            _atomic_json,
            destination / "report.json",
            report,
        )
        return report

    async def release(self, backup_path):
        document = await asyncio.to_thread(
            load_backup,
            backup_path,
            expected_scope=self.scope,
        )
        if tuple(document["scope"]) != self.scope.sql_values:
            raise IntegrityError("backup release scope differs")
        expected = _pin_members(document)
        async with self.db.transaction():
            for store in self.stores:
                await store.lock_guard()
            for key, (namespace, ids) in expected.items():
                rows = await self.db.fetch_all(
                    "SELECT * FROM swe_conversation_retentions WHERE service_unit_key=%s AND retention_key=%s ORDER BY member_key FOR UPDATE",
                    (
                        self.scope.sql_values[0],
                        key.encode(),
                    ),
                )
                if [row["member_key"] for row in rows] != ids or any(
                    row["kind"] != "backup"
                    or row["state"] != "active"
                    or _scope_key(row) != namespace
                    or row["snapshot_ref"] != document["snapshot_ref"].encode()
                    or row["publication_id"] != row["member_key"]
                    or row["guard_marker"] is not None
                    for row in rows
                ):
                    raise ConflictError(
                        "backup release member identity differs",
                    )
            for key, (namespace, _) in expected.items():
                store = next(
                    store
                    for store in self.stores
                    if store.scope_values == namespace
                )
                await store.release(key.encode())


def load_backup(path, *, expected_scope=None, expected_service_unit_id=None):
    root = Path(path).resolve(strict=True)
    document = decode_metadata((root / "metadata.json").read_text())
    if document.get("version") not in (2, 3) or not document.get("backup_id"):
        raise IntegrityError("unsupported backup metadata")
    if document.get("version") == 3:
        from .migration.authority import validate_receipt

        authority = validate_receipt(document.get("migration_authority"))
        scope = WorkspaceScope.from_values(document["scope"])
        service = authority["services"].get(scope.service_unit_id)
        if set(authority["services"]) != {scope.service_unit_id} or not any(
            item["scope"] == list(scope.values)
            for item in service["workspaces"]
        ):
            raise IntegrityError("backup migration authority scope differs")
    scope = WorkspaceScope.from_values(document["scope"])
    if expected_service_unit_id is not None:
        service_unit_bytes(expected_service_unit_id)
        if scope.service_unit_id != expected_service_unit_id:
            raise IntegrityError(
                "backup service differs from explicit binding",
            )
    if expected_scope is not None and scope != expected_scope:
        raise IntegrityError("backup release scope differs")
    if document.get("service_unit_id") != scope.service_unit_id:
        raise IntegrityError("backup service differs from scope")
    _validate_snapshot_scopes(document["snapshot"], scope)
    if set(document["pin_keys"]) != set(_pin_members(document)) or len(
        document["pin_keys"],
    ) != len(set(document["pin_keys"])):
        raise IntegrityError(
            "backup pin job membership differs from captured publications",
        )
    validate_backup(document["snapshot"], BundleStore(root / "nas"))
    return document


def _restored_rows(snapshot, evidence):
    snapshot = decode_metadata(encode_metadata(snapshot))
    changes = []
    for row in snapshot["retentions"]:
        if row["state"] == "active" and row["kind"] in (
            "reader",
            "backup_fence",
        ):
            row.update(
                state="released",
                released_at=datetime.now(timezone.utc).replace(tzinfo=None),
            )
            changes.append(
                {
                    "table": "retentions",
                    "id": row["id"],
                    "action": "release_stopped_source_owner",
                },
            )
    active = set()
    for row in snapshot["sessions"]:
        if (
            row["owner_key"] is not None
            or row["lease_until"] is not None
            or row["active_turn_id"] is not None
        ):
            if row["active_turn_id"] is not None:
                active.add(row["active_turn_id"])
            row.update(
                generation=row["generation"] + 1,
                owner_key=None,
                lease_until=None,
            )
            changes.append(
                {
                    "table": "sessions",
                    "id": row["id"],
                    "action": "fence_stopped_source_owner",
                },
            )
    for row in snapshot["turns"]:
        if row["id"] in active and row["state"] in (
            "reserved",
            "admitted",
            "running",
            "stopping",
            "settling",
        ):
            if row["state"] not in ("reserved", "stopping"):
                row["state"] = "settling"
            changes.append(
                {
                    "table": "turns",
                    "id": row["id"],
                    "action": "require_original_operation_reconciliation",
                },
            )
    return snapshot, {
        "source_writers_stopped_ref": evidence.source_writers_stopped_ref,
        "isolated_target_ref": evidence.isolated_target_ref,
        "changes": changes,
    }


def _settlement_report(document, snapshot, database, bundles):
    original_sessions = {
        row["id"]: row for row in document["snapshot"]["sessions"]
    }
    sessions = {row["id"]: row for row in snapshot["sessions"]}
    publications = {row["id"]: row for row in snapshot["publications"]}
    pending = []
    for turn in snapshot["turns"]:
        if turn["session_id"] not in original_sessions:
            continue
        if (
            turn["state"] not in ("reserved", "stopping", "settling")
            or sessions[turn["session_id"]]["active_turn_id"] != turn["id"]
        ):
            continue
        pending.append(
            {
                "session_id": turn["session_id"],
                "turn_id": turn["id"],
                "question_msg_key": turn["question_msg_key"].decode(),
                "original_generation": original_sessions[turn["session_id"]][
                    "generation"
                ],
                "restored_generation": sessions[turn["session_id"]][
                    "generation"
                ],
                "admission_operation_key": (
                    publications[turn["admission_publication_id"]][
                        "operation_key"
                    ].decode()
                    if turn["admission_publication_id"]
                    else None
                ),
                "terminal_operation_key": (
                    publications[turn["terminal_publication_id"]][
                        "operation_key"
                    ].decode()
                    if turn["terminal_publication_id"]
                    else None
                ),
            },
        )
    return {
        "scope": [value.decode() for value in document["scope"]],
        "target_schema": database.config.database,
        "target_nas_root": str(Path(bundles.root).resolve()),
        "needs_settlement": pending,
        "restore_ready": not pending,
    }


def _restore_stores(bundles, documents, backup_path):
    services = {
        WorkspaceScope.from_values(doc["scope"]).service_unit_id
        for doc in documents
    }
    if isinstance(bundles, BundleStore):
        if len(services) != 1:
            raise IntegrityError(
                "mixed-service restore requires explicit NAS mapping",
            )
        stores = {next(iter(services)): bundles}
    elif isinstance(bundles, dict):
        stores = bundles
        if set(stores) != services or any(
            not isinstance(store, BundleStore) for store in stores.values()
        ):
            raise IntegrityError(
                "restore NAS mapping must exactly match services",
            )
    else:
        raise TypeError("restore requires BundleStore or service NAS mapping")
    if any(
        Path(store.root).is_symlink()
        or Path(store.root).absolute() != Path(store.root).resolve()
        for store in stores.values()
    ):
        raise IntegrityError("restore NAS roots must be nonsymlink roots")
    roots = [Path(store.root).resolve() for store in stores.values()]
    paths = (
        list(backup_path)
        if isinstance(backup_path, (list, tuple))
        else [backup_path]
    )
    sources = [Path(path).resolve() for path in paths]
    for index, root in enumerate(roots):
        if any(
            root == other or root in other.parents or other in root.parents
            for other in roots[index + 1 :] + sources
        ):
            raise IntegrityError(
                "restore NAS roots must be isolated and disjoint",
            )
        if root.exists() and (
            root.is_symlink() or not root.is_dir() or any(root.iterdir())
        ):
            raise ValueError("restore destination must be an empty directory")
    return stores


def _service_snapshot(snapshot, service):
    key = service_unit_bytes(service)
    return {
        name: [row for row in rows if row["service_unit_key"] == key]
        for name, rows in snapshot.items()
    }


async def _check_empty_migration_controls(database, tables, *, lock=False):
    for table in tables:
        suffix = " FOR UPDATE" if lock else ""
        if await database.fetch_all(
            "SELECT * FROM " + table + " LIMIT 1" + suffix,
        ):
            raise IntegrityError(
                "old backup lacks migration authority receipt",
            )


# pylint: disable-next=too-many-statements
async def restore_backup(
    database,
    bundles,
    backup_path,
    *,
    evidence,
    workspace_roots=None,
):
    """Atomically restore one path or a complete list of scope backup paths."""
    from .backup_set import load_restore_set, validate_restore_tokens

    if not isinstance(evidence, RestoreEvidence):
        raise ValueError("restore requires explicit evidence")
    documents, captured, sources = await asyncio.to_thread(
        load_restore_set,
        backup_path,
    )
    stores = _restore_stores(bundles, documents, backup_path)
    receipts = [member.get("migration_authority") for member in documents]
    controls = await database.fetch_one(
        "SELECT COUNT(*) AS n FROM information_schema.TABLES "
        "WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME IN "
        "('swe_conversation_migration_jobs','swe_conversation_migration_entries')",
    )
    control_tables = (
        "swe_conversation_migration_jobs",
        "swe_conversation_migration_entries",
    )
    if int(controls["n"]) == 1:
        raise IntegrityError("restore requires both migration control tables")
    if not int(controls["n"]):
        control_tables = ()
    if any(receipt is not None for receipt in receipts):
        if not all(receipt is not None for receipt in receipts):
            raise IntegrityError(
                "restore set has incomplete migration authority receipts",
            )
        if workspace_roots is None:
            raise IntegrityError(
                "restore authority requires explicit workspace roots",
            )
        if int(controls["n"]) != 2:
            raise IntegrityError(
                "restore authority requires precreated migration controls",
            )
    else:
        await _check_empty_migration_controls(database, control_tables)
    document = {**documents[0], "snapshot": captured}
    _restore_target(database, document.get("source_schema"))
    current_schema = await database.fetch_one("SELECT DATABASE() AS name")
    if current_schema["name"] != database.config.database:
        raise IntegrityError(
            "restore connection schema differs from isolated target",
        )
    for table in TABLES.values():
        if (await database.fetch_one("SELECT COUNT(*) AS n FROM " + table))[
            "n"
        ]:
            raise ConflictError("restore requires an empty metadata target")
    await validate_restore_tokens(database, captured)
    targets = {
        service: _new_directory(store.root)
        for service, store in stores.items()
    }
    snapshot, report = _restored_rows(captured, evidence)
    ids = snapshot_publication_ids(snapshot)
    by_id = {row["id"]: row for row in snapshot["publications"]}
    for identity in ids:
        await asyncio.to_thread(
            _copy_bundle,
            sources[identity],
            targets[by_id[identity]["service_unit_key"].decode()],
            by_id[identity],
        )
    async with database.transaction():
        if not any(receipt is not None for receipt in receipts):
            await _check_empty_migration_controls(
                database,
                control_tables,
                lock=True,
            )
        for table in TABLES.values():
            if await database.fetch_all(
                "SELECT * FROM "
                + table
                + " ORDER BY "
                + ("token" if table == "swe_chat_shares" else "id")
                + " FOR UPDATE",
            ):
                raise ConflictError(
                    "restore target changed before installation",
                )
        for name, table in TABLES.items():
            columns = await database.fetch_all(
                "SELECT COLUMN_NAME FROM information_schema.COLUMNS WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME=%s",
                (table,),
            )
            allowed = {row["COLUMN_NAME"] for row in columns}
            if any(set(row) != allowed for row in snapshot[name]):
                raise IntegrityError(
                    "restore metadata schema differs from installed target",
                )
        for name, table in TABLES.items():
            for row in snapshot[name]:
                names = ",".join(row)
                await database.insert(
                    "INSERT INTO "
                    + table
                    + " ("
                    + names
                    + ") VALUES ("
                    + ",".join(["%s"] * len(row))
                    + ")",
                    tuple(row.values()),
                )
        await _verify_restored_metadata(database, snapshot, stores)
        for member in documents:
            service = WorkspaceScope.from_values(
                member["scope"],
            ).service_unit_id
            await _verify_restored_readers(database, stores[service], member)
        restored_authority = None
        if all(receipt is not None for receipt in receipts):
            from .migration.authority import restore_authorities

            restored_authority = await restore_authorities(
                database,
                receipts,
                [
                    WorkspaceScope.from_values(member["scope"])
                    for member in documents
                ],
                {
                    service: str(Path(store.root).resolve())
                    for service, store in stores.items()
                },
                workspace_roots=workspace_roots,
            )
    report.update(
        version=2,
        service_unit_id=WorkspaceScope.from_values(
            document["scope"],
        ).service_unit_id,
        passed=True,
        backup_id=document["backup_id"],
        state="restored",
        complete_metadata_and_readers=True,
        snapshot_ref=document["snapshot_ref"],
        reader_counts={
            name: len(snapshot[name])
            for name in ("sessions", "chats", "turns", "shares")
        },
    )
    member_reports = [
        _settlement_report(
            member,
            _service_snapshot(
                snapshot,
                WorkspaceScope.from_values(member["scope"]).service_unit_id,
            ),
            database,
            stores[
                WorkspaceScope.from_values(member["scope"]).service_unit_id
            ],
        )
        for member in documents
    ]
    report.update(member_reports[0])
    report["needs_settlement"] = [
        row for member in member_reports for row in member["needs_settlement"]
    ]
    report["restore_ready"] = not report["needs_settlement"]
    if len(stores) > 1:
        report.pop("service_unit_id", None)
        report["service_unit_ids"] = sorted(stores)
        report.pop("target_nas_root", None)
        report["target_nas_roots"] = {
            service: str(Path(store.root).resolve())
            for service, store in stores.items()
        }
    if len(documents) > 1:
        for key in ("backup_id", "snapshot_ref", "scope"):
            report.pop(key)
        report.update(
            backup_ids=[member["backup_id"] for member in documents],
            snapshot_refs=[member["snapshot_ref"] for member in documents],
            scopes=[
                [value.decode() for value in member["scope"]]
                for member in documents
            ],
        )
    if restored_authority is not None:
        report["migration_authority"] = restored_authority
        report["runtime_manifests"] = {
            service: {
                "version": 2,
                "service_unit_id": service,
                "nas_root": entry["nas_root"],
                "workspaces": entry["workspaces"],
                "migration_authority": restored_authority,
            }
            for service, entry in restored_authority["services"].items()
        }
    return report


async def _verify_restored_metadata(database, snapshot, stores):
    actual = {}
    for name, table in TABLES.items():
        key = "token" if name == "shares" else "id"
        actual[name] = await database.fetch_all(
            "SELECT * FROM " + table + " ORDER BY " + key,
        )
        actual[name].sort(key=itemgetter(key))
        if actual[name] != sorted(snapshot[name], key=itemgetter(key)):
            wanted = sorted(snapshot[name], key=itemgetter(key))
            fields = sorted(
                {
                    field
                    for left, right in zip(actual[name], wanted)
                    for field in left
                    if left[field] != right[field]
                },
            )
            raise IntegrityError(
                "restored metadata differs from complete captured "
                + name
                + " fields: "
                + ",".join(fields),
            )
    for service, store in stores.items():
        validate_backup(_service_snapshot(actual, service), store)


async def _verify_restored_readers(database, bundles, document):
    from .session_adapter import MySQLSessionAdapter
    from .chat_repository import MySQLChatRepository
    from .shares import ShareStorage
    from .history import HistoryStore
    from .archive_reader import ArchiveReader

    scope = WorkspaceScope.from_values(document["scope"])
    snapshot = document["snapshot"]
    sessions = {row["id"]: row for row in snapshot["sessions"]}
    for row in document["snapshot"]["sessions"]:
        if row["recovery_publication_id"] is not None:
            actual = await MySQLSessionAdapter(
                database,
                scope,
                bundles,
                channel=row["channel_key"].decode(),
            ).get_session_state_dict(
                row["external_session_key"].decode(),
                row["user_key"].decode(),
                False,
            )
            expected = _recovery_payload(
                snapshot,
                bundles,
                row["recovery_publication_id"],
                offset=row["recovery_offset"],
                length=row["recovery_length"],
            )
            expected["revision"] = row["state_revision"]
            if actual != expected:
                raise IntegrityError(
                    "restored Session reader differs from complete captured recovery",
                )
    for row in document["snapshot"]["chats"]:
        key = row["external_chat_key"].decode()
        chat = await MySQLChatRepository(database, scope, bundles).get_chat(
            key,
        )
        if row["deleted_at"] is not None:
            if chat is not None:
                raise IntegrityError("restore resurrected a deleted Chat")
            continue
        history = HistoryStore(
            database,
            scope,
            bundles,
            cursor_secret=b"isolated-backup-verification",
        )
        await history.current(key)
        cursor = None
        while True:
            page = await history.archive_page(key, before=cursor)
            if not page.next_cursor:
                break
            cursor = page.next_cursor
        async for _page in ArchiveReader(
            database,
            scope,
            bundles,
        ).iter_checkpoint_pages(key):
            pass
    live_chats = {
        row["id"] for row in snapshot["chats"] if row["deleted_at"] is None
    }
    for row in snapshot["turns"]:
        if (
            row["chat_id"] not in live_chats
            or row["state"] == "rejected"
            or (
                row["terminal_publication_id"]
                or row["admission_publication_id"]
            )
            is None
        ):
            continue
        session = sessions[row["session_id"]]
        adapter = MySQLSessionAdapter(
            database,
            scope,
            bundles,
            channel=session["channel_key"].decode(),
        )
        actual = await adapter.get_turn_state(
            session["external_session_key"].decode(),
            session["user_key"].decode(),
            {},
            row["question_msg_key"].decode(),
        )
        root = _recovery_payload(
            snapshot,
            bundles,
            row["terminal_publication_id"] or row["admission_publication_id"],
        )
        expected = root.get("turn_states", {}).get(
            row["question_msg_key"].decode(),
        )
        if actual != expected:
            raise IntegrityError("restored complete Turn reader differs")
    storage = ShareStorage(
        database,
        bundles,
        service_unit_id=scope.service_unit_id,
    )
    for row in document["snapshot"]["shares"]:
        await storage.verify_snapshot(row["token"])
