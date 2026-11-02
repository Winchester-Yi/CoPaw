# -*- coding: utf-8 -*-
"""Validate scope backups as one restore set before touching an empty target."""

from pathlib import Path
import re
from operator import itemgetter

from .bundle import BundleStore, CHUNK_BYTES
from .errors import IntegrityError


def _shared_row(name, row, publications):
    if name == "shares":
        row = publications[row["publication_id"]]
    return row.get("scope_kind") == "legacy_share"


def _comparable(name, row):
    if name == "retentions" and row["kind"] in ("reader", "backup_fence"):
        if row["state"] not in ("active", "released"):
            raise IntegrityError("invalid stopped-source retention state")
        return {**row, "state": "released", "released_at": None}
    return row


def _same_bundle(left, right, row):
    relative = row["relative_path"].decode()
    with left._open(relative) as first, right._open(relative) as second:
        while True:
            chunk = first.read(CHUNK_BYTES)
            if chunk != second.read(CHUNK_BYTES):
                raise IntegrityError("restore set shared bundle bytes differ")
            if not chunk:
                return


def load_restore_set(backup_path):
    from .backup import (
        TABLES,
        load_backup,
        snapshot_publication_ids,
        _validate_associations,
    )

    paths = (
        list(backup_path)
        if isinstance(backup_path, (list, tuple))
        else [backup_path]
    )
    if not paths:
        raise IntegrityError("restore set must contain at least one scope")
    documents = [load_backup(path) for path in paths]
    scopes = [tuple(document["scope"]) for document in documents]
    if len(scopes) != len(set(scopes)):
        raise IntegrityError("restore set contains duplicate workspace scopes")
    schemas = {document.get("source_schema") for document in documents}
    if len(schemas) != 1 or (len(paths) > 1 and not next(iter(schemas))):
        raise IntegrityError(
            "restore set must come from one identified source schema",
        )
    merged = {name: {} for name in TABLES}
    sources, shared = {}, {}
    for path, document in zip(paths, documents):
        snapshot = document["snapshot"]
        publications = {row["id"]: row for row in snapshot["publications"]}
        # Tenant-level business content must describe the same stopped source.
        tenant_shared = {
            name: [
                row
                for row in snapshot[name]
                if _shared_row(name, row, publications)
            ]
            for name in ("publications", "shares")
        }
        tenant = tuple(document["scope"][:2])
        if tenant in shared and shared[tenant] != tenant_shared:
            raise IntegrityError("restore set tenant share snapshots differ")
        shared[tenant] = tenant_shared
        store = BundleStore(Path(path) / "nas")
        live = set(snapshot_publication_ids(snapshot))
        for name in TABLES:
            key = "token" if name == "shares" else "id"
            if len({row[key] for row in snapshot[name]}) != len(
                snapshot[name],
            ):
                raise IntegrityError(
                    "restore scope has duplicate metadata identities: " + name,
                )
            for row in snapshot[name]:
                identity = row[key]
                previous = merged[name].get(identity)
                if previous is not None:
                    if not _shared_row(name, row, publications) or _comparable(
                        name,
                        previous,
                    ) != _comparable(name, row):
                        raise IntegrityError(
                            "restore set metadata identity collision: " + name,
                        )
                    if name == "publications" and identity in live:
                        _same_bundle(sources[identity], store, row)
                else:
                    merged[name][identity] = row
                    if name == "publications" and identity in live:
                        sources[identity] = store
    snapshot = {
        name: sorted(
            rows.values(),
            key=itemgetter("token" if name == "shares" else "id"),
        )
        for name, rows in merged.items()
    }
    snapshot_publication_ids(snapshot)
    _validate_associations(snapshot)
    return documents, snapshot, sources


async def validate_restore_tokens(database, snapshot):
    """Use the target token column's equality rules before any NAS mutation."""
    tokens = [row["token"] for row in snapshot["shares"]]
    if len(tokens) < 2:
        return
    column = await database.fetch_one(
        "SELECT CHARACTER_SET_NAME,COLLATION_NAME FROM information_schema.COLUMNS "
        "WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME='swe_chat_shares' AND COLUMN_NAME='token'",
    )
    if column is None or any(
        not isinstance(column.get(key), str)
        or not re.fullmatch(r"[A-Za-z0-9_]+", column[key])
        for key in ("CHARACTER_SET_NAME", "COLLATION_NAME")
    ):
        raise IntegrityError("restore target token collation is unavailable")
    # Identifiers are server metadata, validated above; token values stay bound.
    expression = (
        "SELECT CONVERT(%s USING "
        + column["CHARACTER_SET_NAME"]
        + ") COLLATE "
        + column["COLLATION_NAME"]
        + " AS token"
    )
    batches = [
        tokens[start : start + 256] for start in range(0, len(tokens), 256)
    ]
    # Include cross-batch pairs so equivalent tokens cannot straddle batches.
    for index, batch in enumerate(batches):
        for other in [[], *batches[:index]]:
            values = (*batch, *other)
            collision = await database.fetch_one(
                "SELECT token FROM ("
                + " UNION ALL ".join([expression] * len(values))
                + ") AS restore_tokens GROUP BY token HAVING COUNT(*)>1 LIMIT 1",
                values,
            )
            if collision is not None:
                raise IntegrityError(
                    "restore set token collision under target collation",
                )
