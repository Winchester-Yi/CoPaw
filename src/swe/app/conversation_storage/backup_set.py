# -*- coding: utf-8 -*-
"""Validate scope backups as one restore set before touching an empty target."""

from pathlib import Path
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
        tenant = document["scope"][0]
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
