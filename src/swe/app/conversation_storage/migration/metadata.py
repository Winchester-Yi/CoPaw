# -*- coding: utf-8 -*-
"""Import a frozen Chat registry while leaving the original NAS files in place."""

import asyncio
from concurrent.futures import ThreadPoolExecutor
from hashlib import sha256
from datetime import datetime, timezone
from functools import partial
from itertools import groupby
from pathlib import Path
import json
import logging
from stat import S_ISDIR, S_ISLNK
from uuid import UUID, uuid4

from mysql.connector.errorcode import ER_CHECK_CONSTRAINT_VIOLATED
from mysql.connector.errors import (
    DatabaseError,
    DataError,
    IntegrityError as SQLIntegrityError,
)
from pydantic import ValidationError

from ....config.context import (
    canonicalize_scope_id,
    decode_scope_id,
    is_valid_identity_value,
)
from ...runner.expert_binding import (
    EXPERT_BINDING_META_KEY,
    read_expert_binding,
)
from ...runner.models import ChatSpec, ChatsFile
from ...runner.session import session_filename
from ...runner.turn_ledger import atomic_json
from ..chat_mutations import (
    CHAT_INSERT,
    chat_insert_values,
    highest_registry_order,
    load_metadata_json,
)
from ..metadata_catalog import _utc, SCOPE_WHERE
from ..errors import ConflictError
from ..paths import existing_root
from ..runtime import require_empty_conversation
from ..identity import (
    WorkspaceScope,
    SessionIdentity,
    chat_key_bytes,
    identity_bytes,
    service_unit_from_env,
)
from .legacy import read_archive, read_session
from ..metadata_repository import (
    MySQLMetadataRepository,
    SOURCES,
    WORKSPACES,
    SOURCE_PREDICATE,
    SCOPE_PREDICATE,
    SESSION_INSERT,
    metadata_fields,
    metadata_install,
    session_filename_key,
)

logger = logging.getLogger(__name__)

MODE = "mysql-metadata"
BATCH_SIZE = 100
VERIFY_PAGE_SIZE = 100
EMPTY_REGISTRY_DIGEST = sha256(b"missing chats.json").hexdigest()
WORKSPACE_ERRORS = (
    ConflictError,
    ValueError,
    OSError,
    TypeError,
    DataError,
    SQLIntegrityError,
)


def _workspace(item):
    scope = WorkspaceScope.from_values(item["scope"])
    root = existing_root(item["root"])
    if root.resolve(strict=True) != root:
        raise ValueError("workspace root must be canonical")
    identity_bytes(str(root), 1024)
    return scope, root


def _registry_bytes(root):
    path = root / "chats.json"
    if not path.exists() and not path.is_symlink():
        require_empty_conversation(root)
        return None
    if path.is_symlink() or not path.is_file():
        raise ValueError(f"expected a regular Chat registry at {path}")
    return path.read_bytes()


def _registry_digest(raw):
    return EMPTY_REGISTRY_DIGEST if raw is None else sha256(raw).hexdigest()


def _registry_document(root, raw):
    document = load_metadata_json(raw)
    if (
        not isinstance(document, dict)
        or type(document.get("version")) is not int
        or document["version"] != 1
    ):
        raise ValueError(
            f"unsupported Chat registry version at {root / 'chats.json'}",
        )
    return document


def _validate_chat(chat):
    if str(UUID(chat.id)) != chat.id:
        raise ValueError(f"noncanonical Chat UUID {chat.id}")
    chat_key_bytes(chat.id)
    identity_bytes(session_filename(chat.session_id, chat.user_id), 255)
    if len(chat.name.encode("utf-8")) > 65_535:
        raise ValueError(f"Chat {chat.id} title exceeds the SQL TEXT size")
    metadata_fields(chat.meta)
    binding = read_expert_binding(chat)
    if binding.expert_id is not None:
        identity_bytes(binding.expert_id, 256)


def _session_failure(fields, detail, **location):
    return {
        "chat_id": fields.get("id"),
        "session_id": fields.get("session_id"),
        "user_id": fields.get("user_id"),
        "channel": fields.get("channel"),
        "detail": detail,
        **location,
    }


def _entry_identity(scope, fields):
    channel = fields.get("channel", ChatSpec.model_fields["channel"].default)
    session_id = fields.get("session_id")
    if all(isinstance(value, str) for value in (channel, session_id)):
        return SessionIdentity(scope, scope.tenant_id, channel, session_id)
    return None


def _select_chats(scope, entries):
    keys = {}
    sessions = {}
    candidates, failures = [], []
    invalid = set()
    for index, entry in enumerate(entries):
        fields = entry if isinstance(entry, dict) else {}
        user_id = fields.get("user_id")
        if isinstance(user_id, str) and user_id != scope.tenant_id:
            continue
        identity = None
        try:
            if user_id == scope.tenant_id:
                identity = _entry_identity(scope, fields)
            if identity is not None:
                filename = session_filename(
                    identity.session_id,
                    identity.user_id,
                )
                owner = sessions.setdefault(filename, identity)
                if owner != identity:
                    invalid.add(owner)
                    raise ValueError(
                        f"multiple Session identities map to {filename}",
                    )
            if "id" not in fields:
                raise ValueError("Chat ID is missing")
            chat = ChatSpec.model_validate(entry)
            if chat.id in keys:
                invalid.add(keys[chat.id])
                raise ValueError(f"duplicate Chat identity {chat.id}")
            keys[chat.id] = identity
            _validate_chat(chat)
            candidates.append((identity, chat))
        except (TypeError, ValueError, ConflictError) as error:
            invalid.add(identity)
            detail = (
                str(error.errors(include_input=False, include_url=False))
                if isinstance(error, ValidationError)
                else str(error)
            )
            failures.append(
                _session_failure(fields, detail, chat_index=index),
            )
    return candidates, invalid, failures


def prepare(workspaces, schema, manifest):
    bindings = []
    roots, scopes = set(), set()
    output = Path(manifest).resolve()
    for item in workspaces:
        scope = WorkspaceScope.from_values(item["scope"])
        root = Path(item["root"])
        if output.is_relative_to(root.resolve()):
            raise ValueError("manifest must be outside the Chat workspace")
        if root in roots or scope in scopes:
            raise ValueError("duplicate Chat workspace scope or path")
        roots.add(root)
        scopes.add(scope)
        binding = {"root": str(root), "scope": scope.values}
        try:
            _, root = _workspace(item)
            raw = _registry_bytes(root)
            binding["registry_sha256"] = _registry_digest(raw)
            if raw is None:
                binding["registry_missing"] = True
        except WORKSPACE_ERRORS as error:
            binding["prepare_error"] = str(error)
        bindings.append(binding)
    if not bindings:
        raise ValueError("at least one workspace is required")
    data = {
        "version": 1,
        "mode": MODE,
        "migration_id": str(uuid4()),
        "schema": schema,
        "workspaces": sorted(bindings, key=lambda item: item["scope"]),
    }
    _write_new_json(output, data)
    return data


def prepare_scan(workspace_root, schema, manifest, *, workers=4):
    root = existing_root(workspace_root).resolve(strict=True)
    service = service_unit_from_env()
    output = Path(manifest).resolve()
    workspaces, errors = [], []
    entries = sorted(root.iterdir())
    scan = partial(_scan_scope, service=service)
    with ThreadPoolExecutor(max_workers=workers) as executor:
        for outcome in executor.map(scan, entries):
            if outcome is None:
                continue
            if "scope" in outcome:
                workspaces.append(outcome)
            else:
                errors.append(outcome)
    if not workspaces and not errors:
        raise ValueError("no default workspace found under the NAS root")
    if any(output.is_relative_to(Path(item["root"])) for item in workspaces):
        raise ValueError("manifest must be outside the Chat workspace")
    result = {
        "version": 1,
        "mode": MODE,
        "migration_id": str(uuid4()),
        "schema": schema,
        "workspace_root": str(root),
        "workspaces": workspaces,
        "scan_errors": errors,
    }
    _write_new_json(output, result)
    return result


def _scan_scope(entry, *, service):
    root = entry / "workspaces" / "default"
    recognized = entry.name.startswith("default_")
    scope_error = None
    try:
        if recognized:
            tenant, source = "default", entry.name.removeprefix("default_")
            if not is_valid_identity_value(source):
                raise ValueError("invalid default workspace source ID")
        else:
            canonical = canonicalize_scope_id(entry.name)
            recognized = True
            tenant, source = decode_scope_id(canonical)
            if tenant == "default":
                return None
            if entry.name != canonical:
                raise ValueError(
                    f"workspace scope directory must be {canonical}",
                )
        scope = WorkspaceScope.from_values(
            (service, tenant, source, "default"),
        ).values
    except ValueError as error:
        scope_error = f"workspace scope cannot be resolved: {error}"
        scope = None

    item = {"root": str(root), "scope": scope}
    try:
        if not _is_workspace_directory(entry, root, recognized):
            return None
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as error:
        item["scan_error"] = str(error)

    if scope is None:
        return {
            "root": str(root),
            "detail": item.get("scan_error") or scope_error,
        }
    if "scan_error" not in item:
        try:
            raw = _registry_bytes(root)
            if raw is not None and not isinstance(
                _registry_document(root, raw).get("chats", []),
                list,
            ):
                raise ValueError("Chat registry must contain a chats list")
        except WORKSPACE_ERRORS as error:
            item["scan_error"] = str(error)
    return item


def _is_workspace_directory(entry, root, recognized):
    for path in (entry, root.parent, root):
        mode = path.lstat().st_mode
        if S_ISDIR(mode):
            continue
        if path == entry and (not recognized or not S_ISLNK(mode)):
            return False
        raise ValueError(f"not a regular workspace directory: {path}")
    return True


def _write_new_json(path, data):
    payload = json.dumps(data, ensure_ascii=False, indent=2) + "\n"
    with path.open("x", encoding="utf-8") as target:
        target.write(payload)


def read_manifest(path, schema):
    raw = Path(path).read_bytes()
    data = load_metadata_json(raw)
    if (
        not isinstance(data, dict)
        or data.get("mode") != MODE
        or data.get("version") != 1
    ):
        raise ValueError("invalid MySQL metadata manifest")
    if (
        data["schema"] != schema
        or str(UUID(data["migration_id"])) != data["migration_id"]
    ):
        raise ValueError("configured database or migration identity differs")
    bindings = data["workspaces"]
    if (not bindings and not data.get("scan_errors")) or len(
        {tuple(item["scope"]) for item in bindings},
    ) != len(bindings):
        raise ValueError(
            "migration manifest contains invalid workspace scopes",
        )
    return data, sha256(raw).digest()


def _verify_nas(root, candidates, invalid, failures, *, full_audit=False):
    sessions, dialog = root / "sessions", root / "dialog"
    for directory in (sessions, dialog):
        if directory.is_symlink() or (
            directory.exists() and not directory.is_dir()
        ):
            raise ValueError(f"invalid legacy storage directory {directory}")
    seen = set()
    audit_archives = full_audit and dialog.exists()
    for identity, chat in candidates:
        if identity in invalid:
            continue
        session = sessions / session_filename(chat.session_id, chat.user_id)
        try:
            if session not in seen:
                seen.add(session)
                if session.is_symlink() or (
                    session.exists() and not session.is_file()
                ):
                    raise ValueError(f"invalid legacy Session file {session}")
                if full_audit and session.exists():
                    _audit_legacy(read_session, session, trusted_root=root)
            if audit_archives:
                _audit_legacy(read_archive, dialog, chat.id, trusted_root=root)
        except (OSError, ValueError, TypeError, ConflictError) as error:
            invalid.add(identity)
            failures.append(
                _session_failure(
                    {
                        "id": chat.id,
                        "session_id": chat.session_id,
                        "user_id": chat.user_id,
                        "channel": chat.channel,
                    },
                    str(error),
                ),
            )


def _audit_legacy(reader, path, *args, trusted_root):
    try:
        reader(path, *args, trusted_root=trusted_root)
    except (KeyError, IndexError, AttributeError) as error:
        location = path.joinpath(*args)
        raise ValueError(
            f"invalid legacy data at {location}: {error}",
        ) from error


def _frozen_bytes(item, root):
    raw = _registry_bytes(root)
    if (raw is None) != item.get("registry_missing", False) or (
        _registry_digest(raw) != item["registry_sha256"]
    ):
        stage = (
            "migration started" if item.get("live_registry") else "preparation"
        )
        raise ConflictError(f"Chat registry changed after {stage} at {root}")
    return raw


def _live_registries(workspaces):
    current = []
    for item in workspaces:
        item = dict(item, live_registry=True)
        try:
            _, root = _workspace(item)
            raw = _registry_bytes(root)
            item["registry_sha256"] = _registry_digest(raw)
            item["registry_missing"] = raw is None
        except WORKSPACE_ERRORS as error:
            item["prepare_error"] = str(error)
        current.append(item)
    return current


def _verify_workspace(item, *, full_audit):
    if "prepare_error" in item:
        raise ValueError(item["prepare_error"])
    scope, root = _workspace(item)
    raw = _frozen_bytes(item, root)
    entries = (
        [] if raw is None else _registry_document(root, raw).get("chats", [])
    )
    if not isinstance(entries, list):
        raise ValueError("Chat registry must contain a chats list")
    candidates, invalid, failures = _select_chats(scope, entries)
    _verify_nas(root, candidates, invalid, failures, full_audit=full_audit)
    chats = [chat for identity, chat in candidates if identity not in invalid]
    return ChatsFile(chats=chats), failures


class WorkspaceProgress:
    """Receipts written after a workspace passes SQL and NAS verification."""

    def __init__(self, path, digest, database, *, previous=None):
        self.path = Path(path).with_name(Path(path).name + ".progress.json")
        self.identity = {
            "manifest_sha256": digest.hex(),
            "database": [database.host, database.port, database.database],
            "tenant_filtered_registry": True,
        }
        self.completed = self._read(self.path, self.identity)
        self.previous = previous.completed if previous is not None else {}

    @staticmethod
    def _read(path, identity):
        try:
            value = load_metadata_json(path.read_bytes())
        except (FileNotFoundError, ValueError):
            return {}
        except OSError as error:
            logger.warning(
                "Cannot read migration progress at %s: %s",
                path,
                error,
            )
            return {}
        if not isinstance(value, dict) or value.get("identity") != identity:
            return {}
        completed = value.get("completed")
        return completed if isinstance(completed, dict) else {}

    @staticmethod
    def _key(item):
        return json.dumps(item["scope"], ensure_ascii=False)

    def receipt(self, item, *, full_audit):
        key = self._key(item)
        for records in (self.completed, self.previous):
            receipt = records.get(key)
            if not isinstance(receipt, dict):
                continue
            chat_count = receipt.get("chat_count")
            audited = receipt.get("full_audit")
            if (
                type(chat_count) is not int
                or chat_count < 0
                or type(audited) is not bool
            ):
                continue
            if (
                receipt.get("root") == item["root"]
                and receipt.get("registry_sha256")
                == item.get("registry_sha256")
                and (not full_audit or audited)
            ):
                return receipt
        return None

    def record(self, item, chat_count, *, full_audit):
        key = self._key(item)
        receipt = {
            "root": item["root"],
            "registry_sha256": item["registry_sha256"],
            "chat_count": chat_count,
            "full_audit": full_audit,
        }
        if self.completed.get(key) == receipt:
            return
        completed = {**self.completed, key: receipt}
        try:
            atomic_json(
                self.path,
                {"identity": self.identity, "completed": completed},
            )
        except OSError as error:
            logger.warning(
                "Cannot write migration progress at %s: %s",
                self.path,
                error,
            )
            return
        self.completed = completed


async def _completed_workspaces(
    database,
    source_key,
    workspaces,
    progress,
    *,
    full_audit,
):
    receipts = {
        WorkspaceScope.from_values(item["scope"]).sql_values[
            -1
        ]: progress.receipt(item, full_audit=full_audit)
        for item in workspaces
    }
    if not any(receipts.values()):
        return {}
    rows = await database.fetch_all(
        f"SELECT agent_key,workspace_root,registry_digest,imported_count FROM {WORKSPACES} WHERE "
        + SOURCE_PREDICATE,
        source_key,
    )
    completed = {}
    for row in rows:
        agent = bytes(row["agent_key"])
        receipt = receipts.get(agent)
        if (
            receipt is not None
            and receipt["root"].encode() == bytes(row["workspace_root"])
            and receipt["registry_sha256"]
            == bytes(row["registry_digest"]).hex()
            and receipt["chat_count"] == row["imported_count"]
        ):
            completed[agent] = receipt
    return completed


async def migrate(
    database,
    manifest,
    manifest_digest,
    *,
    apply,
    full_audit=False,
    resume_from=None,
    progress=None,
    workers=1,
):
    if workers < 1:
        raise ValueError("migration workers must be at least 1")

    def by_source(item):
        return tuple(item["scope"][:3])

    groups = [
        (key, list(group))
        for key, group in groupby(
            sorted(manifest["workspaces"], key=by_source),
            key=by_source,
        )
    ]
    results = {}
    pending = iter(enumerate(groups))

    async def process_sources():
        for index, (key, workspaces) in pending:
            skipped_workspaces = []
            try:
                state, errors = await _migrate_source(
                    database,
                    manifest["migration_id"],
                    manifest_digest,
                    workspaces,
                    apply=apply,
                    full_audit=full_audit,
                    resume_from=resume_from,
                    progress=progress,
                    skipped_workspaces=skipped_workspaces,
                    live_registry="workspace_root" in manifest,
                )
            except (ConflictError, DataError, SQLIntegrityError) as error:
                state = "failed"
                errors = [{"source": key, "detail": str(error)}]
            results[index] = (key, state, errors, skipped_workspaces)

    if workers == 1:
        await process_sources()
    else:
        tasks = [
            asyncio.create_task(process_sources())
            for _ in range(min(workers, len(groups)))
        ]
        try:
            await asyncio.gather(*tasks)
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    published, skipped = [], []
    failed = [
        dict(error, phase="scan") for error in manifest.get("scan_errors", [])
    ]
    skipped_workspaces = []
    for index in range(len(groups)):
        key, state, errors, reused = results[index]
        failed.extend(errors)
        skipped_workspaces.extend(reused)
        if state == "skipped":
            skipped.append(key)
        elif state == "ready":
            published.append(key)
    return {
        "mode": MODE,
        "migration_id": manifest["migration_id"],
        "status": (
            "partial_success" if failed else ("ready" if apply else "preview")
        ),
        "published" if apply else "validated": published,
        "already_published": skipped,
        "skipped_workspaces": skipped_workspaces,
        "failed": failed,
    }


def _workspace_failure(item, phase, error):
    return {
        "source": tuple(item["scope"][:3]),
        "scope": tuple(item["scope"]),
        "root": item["root"],
        "phase": phase,
        "detail": str(error),
    }


def _check_resume_membership(workspaces, original_manifest):
    source = tuple(workspaces[0]["scope"][:3])
    original_scopes = {
        tuple(item["scope"])
        for item in original_manifest["workspaces"]
        if tuple(item["scope"][:3]) == source
    }
    if {tuple(item["scope"]) for item in workspaces} != original_scopes:
        raise ConflictError(
            "resume manifest changes source workspace membership",
        )


async def _check_resume_bindings(
    database,
    source_key,
    workspaces,
    row,
    resume_from,
):
    if resume_from is None:
        raise ConflictError(
            "source is bound to a different metadata migration",
        )
    original, original_digest = resume_from
    if (row["migration_id"], row["manifest_digest"]) != (
        original["migration_id"],
        original_digest,
    ):
        raise ConflictError(
            "resume manifest does not match the source binding",
        )
    if row["published"]:
        raise ConflictError(
            "published source must retain its original manifest",
        )
    expected = {
        WorkspaceScope.from_values(item["scope"]).sql_values[-1]: item
        for item in workspaces
    }
    rows = await database.fetch_all(
        f"SELECT agent_key,workspace_root,registry_digest FROM {WORKSPACES} WHERE "
        + SOURCE_PREDICATE,
        source_key,
    )
    for binding in rows:
        item = expected.get(bytes(binding["agent_key"]))
        if (
            item is None
            or bytes(binding["workspace_root"]) != item["root"].encode()
            or bytes(binding["registry_digest"]).hex()
            != item.get("registry_sha256")
        ):
            raise ConflictError("resume manifest changes a bound workspace")


async def _migrate_source(
    database,
    migration_id,
    digest,
    workspaces,
    *,
    apply,
    full_audit,
    resume_from,
    progress,
    skipped_workspaces,
    live_registry=False,
):
    source_key = WorkspaceScope.from_values(workspaces[0]["scope"]).sql_values[
        :3
    ]
    row = None
    previous_identity = None
    if apply:
        if resume_from is not None:
            _check_resume_membership(workspaces, resume_from[0])
        row = await _read_source_binding(database, source_key)
        if row is not None:
            bound_identity = (row["migration_id"], row["manifest_digest"])
            if bound_identity != (migration_id, digest):
                previous_identity = bound_identity
            elif row["published"]:
                return "skipped", []

    if live_registry:
        workspaces = await asyncio.to_thread(_live_registries, workspaces)
    if previous_identity is not None:
        await _check_resume_bindings(
            database,
            source_key,
            workspaces,
            row,
            resume_from,
        )

    completed = (
        await _completed_workspaces(
            database,
            source_key,
            workspaces,
            progress,
            full_audit=full_audit,
        )
        if row is not None and progress is not None
        else {}
    )
    eligible, failures = await _preflight_source_workspaces(
        workspaces,
        completed,
        progress,
        skipped_workspaces,
        full_audit=full_audit,
    )
    if not apply or (not eligible and not completed):
        return ("failed" if failures else "ready"), failures

    await _write_source_binding(
        database,
        source_key,
        (migration_id, digest),
        row,
        previous_identity,
    )
    failures.extend(
        await _import_source_workspaces(
            database,
            eligible,
            progress,
            full_audit=full_audit,
        ),
    )
    if failures:
        return "failed", failures
    await _publish_source_binding(database, source_key, migration_id, digest)
    return "ready", []


async def _read_source_binding(database, source_key):
    return await database.fetch_one(
        f"SELECT migration_id,manifest_digest,published FROM {SOURCES} WHERE "
        + SOURCE_PREDICATE,
        source_key,
    )


async def _write_source_binding(
    database,
    source_key,
    identity,
    row,
    original_identity,
):
    migration_id, digest = identity
    if row is None:
        await database.execute(
            f"INSERT INTO {SOURCES} (service_unit_key,tenant_key,source_key,migration_id,manifest_digest) "
            "VALUES (%s,%s,%s,%s,%s)",
            (*source_key, migration_id, digest),
        )
    elif original_identity is not None:
        changed = await database.execute(
            f"UPDATE {SOURCES} SET migration_id=%s,manifest_digest=%s WHERE "
            + SOURCE_PREDICATE
            + " AND migration_id=%s AND manifest_digest=%s AND published=0",
            (migration_id, digest, *source_key, *original_identity),
        )
        if changed != 1:
            raise ConflictError("source binding changed during resume")


async def _publish_source_binding(database, source_key, migration_id, digest):
    if (
        await database.execute(
            f"UPDATE {SOURCES} SET published=1 WHERE "
            + SOURCE_PREDICATE
            + " AND migration_id=%s AND manifest_digest=%s AND published=0",
            (*source_key, migration_id, digest),
        )
        != 1
    ):
        raise ConflictError("source binding changed before publication")


async def _preflight_source_workspaces(
    workspaces,
    completed,
    progress,
    skipped_workspaces,
    *,
    full_audit,
):
    eligible, failures = [], []
    for item in workspaces:
        try:
            receipt = completed.get(
                WorkspaceScope.from_values(item["scope"]).sql_values[-1],
            )
            if receipt is not None:
                progress.record(
                    item,
                    receipt["chat_count"],
                    full_audit=receipt["full_audit"],
                )
                skipped_workspaces.append(tuple(item["scope"]))
                continue
            registry, session_failures = await asyncio.to_thread(
                _verify_workspace,
                item,
                full_audit=full_audit,
            )
        except WORKSPACE_ERRORS as error:
            failures.append(_workspace_failure(item, "preflight", error))
        else:
            failures.extend(
                {
                    **_workspace_failure(item, "preflight", failure["detail"]),
                    **failure,
                }
                for failure in session_failures
            )
            if registry.chats or not session_failures:
                eligible.append((item, registry, session_failures))
    return eligible, failures


async def _import_source_workspaces(
    database,
    eligible,
    progress,
    *,
    full_audit,
):
    failures = []
    for item, registry, session_failures in eligible:
        try:
            await _import_verified_workspace(
                database,
                item,
                registry,
                session_failures,
                progress,
                full_audit=full_audit,
            )
        except WORKSPACE_ERRORS as error:
            failures.append(_workspace_failure(item, "import", error))
        except DatabaseError as error:
            if error.errno != ER_CHECK_CONSTRAINT_VIOLATED:
                raise
            failures.append(_workspace_failure(item, "import", error))
    return failures


async def _import_verified_workspace(
    database,
    item,
    registry,
    session_failures,
    progress,
    *,
    full_audit,
):
    chat_count = await _import_workspace(database, item, registry)
    verified, current_failures = await asyncio.to_thread(
        _verify_workspace,
        item,
        full_audit=full_audit,
    )
    if [chat.id for chat in verified.chats] != [
        chat.id for chat in registry.chats
    ] or current_failures != session_failures:
        raise ConflictError("Session validation changed during migration")
    if progress is not None and not session_failures:
        progress.record(item, chat_count, full_audit=full_audit)


async def _import_workspace(database, item, registry):
    scope, root = _workspace(item)
    await asyncio.to_thread(_frozen_bytes, item, root)
    root_key = identity_bytes(str(root), 1024)
    digest = bytes.fromhex(item["registry_sha256"])
    repo = MySQLMetadataRepository(database, scope, importing=True)
    await _verify_resume_prefix(repo, registry.chats)
    if item.get("live_registry"):
        more = await _import_batch(
            repo,
            registry,
            root_key,
            digest,
            bind_workspace=True,
        )
    else:
        await _bind_workspace(database, scope, root_key, digest)
        more = True
    while more:
        more = await _import_batch(repo, registry, root_key, digest)
    if not await _verify_import(repo, registry.chats):
        raise ConflictError(
            f"SQL Chat registry verification failed for {root}",
        )
    return len(registry.chats)


async def _verify_resume_prefix(repo, chats):
    binding = await repo.db.fetch_one(
        f"SELECT imported_count FROM {WORKSPACES} WHERE " + SCOPE_PREDICATE,
        repo.scope.sql_values,
    )
    if not binding or not binding["imported_count"]:
        return
    count = binding["imported_count"]
    if count > len(chats) or not await _verify_import(repo, chats[:count]):
        raise ConflictError(
            "committed Chats differ from the selected registry prefix",
        )


def _normalized_chat(chat):
    meta = dict(chat.meta)
    binding = read_expert_binding(chat)
    meta.pop(EXPERT_BINDING_META_KEY, None)
    if binding.revision or binding.expert_id is not None:
        meta[EXPERT_BINDING_META_KEY] = binding.model_dump()
    return chat.model_copy(
        update={
            "meta": meta,
            "status": "idle",
            "created_at": _utc(chat.created_at),
            "updated_at": _utc(chat.updated_at),
        },
    )


async def _verify_import(repo, chats):
    index = 0
    order, row_id = 0, 0
    while True:
        rows = await repo.db.fetch_all(
            repo.select_chat + SCOPE_WHERE + " AND c.deleted_at IS NULL "
            "AND (c.registry_order>%s OR (c.registry_order=%s AND c.id>%s)) "
            "ORDER BY c.registry_order,c.id LIMIT %s",
            (*repo.scope.sql_values, order, order, row_id, VERIFY_PAGE_SIZE),
        )
        for row in rows:
            if index >= len(chats) or repo._spec(row) != _normalized_chat(
                chats[index],
            ):
                return False
            index += 1
        if len(rows) < VERIFY_PAGE_SIZE:
            return index == len(chats)
        order, row_id = rows[-1]["registry_order"], rows[-1]["id"]


async def _bind_workspace(database, scope, root_key, digest):
    await database.execute(
        f"INSERT INTO {WORKSPACES} "
        "(service_unit_key,tenant_key,source_key,agent_key,workspace_root,registry_digest) "
        "VALUES (%s,%s,%s,%s,%s,%s) ON DUPLICATE KEY UPDATE imported_count=imported_count",
        (*scope.sql_values, root_key, digest),
    )


async def _import_batch(
    repo,
    registry,
    root_key,
    digest,
    *,
    bind_workspace=False,
):
    database, scope = repo.db, repo.scope
    async with database.transaction():
        if bind_workspace:
            await _bind_workspace(database, scope, root_key, digest)
        await repo._assert_writes_allowed()
        row = await database.fetch_one(
            f"SELECT workspace_root,registry_digest,imported_count FROM {WORKSPACES} WHERE "
            + SCOPE_PREDICATE
            + " FOR UPDATE",
            scope.sql_values,
        )
        if (
            row["workspace_root"] != root_key
            or row["registry_digest"] != digest
        ):
            raise ConflictError(
                "workspace is bound to a different Chat registry",
            )
        start = row["imported_count"]
        if start > len(registry.chats):
            raise ConflictError(
                "workspace progress exceeds the frozen registry",
            )
        chats = registry.chats[start : start + BATCH_SIZE]
        if not chats:
            return False
        await _insert_chats(repo, chats)
        await database.execute(
            f"UPDATE {WORKSPACES} SET imported_count=%s WHERE "
            + SCOPE_PREDICATE,
            (start + len(chats), *scope.sql_values),
        )
        return True


async def _insert_chats(repo, chats):
    db, scope = repo.db, repo.scope
    keys = tuple(chat_key_bytes(chat.id) for chat in chats)
    placeholders = ",".join(["%s"] * len(keys))
    existing = await db.fetch_one(
        "SELECT external_chat_key FROM swe_conversation_chats WHERE "
        + SCOPE_PREDICATE
        + f" AND external_chat_key IN ({placeholders}) LIMIT 1",
        (*scope.sql_values, *keys),
    )
    if existing is not None:
        key = bytes(existing["external_chat_key"]).decode()
        raise ConflictError(
            f"target Chat {key} already exists outside the committed batch",
        )
    high = await highest_registry_order(db, scope)
    identities = [
        SessionIdentity(scope, chat.user_id, chat.channel, chat.session_id)
        for chat in chats
    ]
    sessions = await _insert_sessions(db, scope, identities)

    def rows():
        for order, (chat, identity) in enumerate(
            zip(chats, identities),
            high + 1,
        ):
            plan, extended = metadata_install(chat, chat.meta)
            yield (
                *chat_insert_values(scope, plan, sessions[identity], order),
                extended,
            )

    await db.insert_prepared(CHAT_INSERT, rows())


async def _insert_sessions(db, scope, identities):
    keys = {
        identity: session_filename_key(identity)
        for identity in dict.fromkeys(identities)
    }
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    await db.insert_prepared(
        SESSION_INSERT,
        (
            (*identity.sql_values, name, now, now)
            for identity, name in keys.items()
        ),
    )
    names = tuple(dict.fromkeys(keys.values()))
    placeholders = ",".join(["%s"] * len(names))
    rows = await db.fetch_all(
        "SELECT id,user_key,channel_key,external_session_key,nas_filename "
        "FROM swe_conversation_sessions WHERE "
        + SCOPE_PREDICATE
        + f" AND nas_filename IN ({placeholders})",
        (*scope.sql_values, *names),
    )
    by_identity = {
        (row["user_key"], row["channel_key"], row["external_session_key"]): row
        for row in rows
    }
    sessions = {}
    for identity, name in keys.items():
        row = by_identity.get(identity.sql_values[4:])
        if row is None or row["nas_filename"] != name:
            raise ConflictError(
                "NAS Session filename belongs to another identity",
            )
        sessions[identity] = row
    return sessions


async def _export_bindings(database, manifest, manifest_digest):
    if manifest is None:
        service_key = service_unit_from_env().encode()
        if await database.fetch_one(
            f"SELECT 1 FROM {SOURCES} WHERE service_unit_key=%s "
            "AND published=0 LIMIT 1",
            (service_key,),
        ):
            raise ConflictError(
                "full export requires all sources for this service unit to finish migration",
            )
        return await database.fetch_all(
            f"SELECT * FROM {WORKSPACES} WHERE service_unit_key=%s "
            "ORDER BY tenant_key,source_key,agent_key",
            (service_key,),
        )
    bindings = []
    source_keys = sorted(
        {
            WorkspaceScope.from_values(item["scope"]).sql_values[:3]
            for item in manifest["workspaces"]
        },
    )
    for source_key in source_keys:
        source = await database.fetch_one(
            f"SELECT published,migration_id,manifest_digest FROM {SOURCES} WHERE "
            + SOURCE_PREDICATE,
            source_key,
        )
        if source is None or not source["published"]:
            continue
        if (
            source["migration_id"] != manifest["migration_id"]
            or source["manifest_digest"] != manifest_digest
        ):
            raise ConflictError(
                "published source belongs to a different manifest",
            )
        bindings.extend(
            await database.fetch_all(
                f"SELECT * FROM {WORKSPACES} WHERE "
                + SOURCE_PREDICATE
                + " ORDER BY agent_key",
                source_key,
            ),
        )
    return bindings


async def export(database, manifest, manifest_digest, destination):
    root = Path(destination).resolve()
    ready = []
    async with database.transaction():
        bindings = await _export_bindings(database, manifest, manifest_digest)
        for binding in bindings:
            scope = WorkspaceScope.from_row(binding)
            workspace_root = Path(bytes(binding["workspace_root"]).decode())
            if root.is_relative_to(workspace_root):
                raise ValueError("export must be outside the live workspace")
            data = await MySQLMetadataRepository(database, scope).load()
            ready.append((scope, workspace_root, data))
    if not ready:
        raise ConflictError("no Chat directory is available for export")
    root.mkdir()
    exported = []
    for index, (scope, workspace_root, data) in enumerate(ready):
        relative = Path(str(index)) / "chats.json"
        path = root / relative
        path.parent.mkdir()
        _write_new_json(path, data.model_dump(mode="json"))
        exported.append(
            {
                "scope": scope.values,
                "root": str(workspace_root),
                "registry": str(relative),
            },
        )
    _write_new_json(root / "report.json", {"workspaces": exported})
    return exported
