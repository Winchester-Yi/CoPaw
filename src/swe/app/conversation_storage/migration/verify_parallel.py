# -*- coding: utf-8 -*-
"""Owner-collected message evidence for bounded NAS verification workers."""

from .parallel import MigrationCoordinator


class MessageEvidence:
    """Detached rows; no database, transaction, or executable SQL capability."""

    def __init__(self, sessions, archives, publications):
        self.sessions = sessions
        self.archives = archives
        self.publications = publications

    async def fetch_all(self, query, params=None):
        if query == (
            "SELECT * FROM swe_conversation_messages WHERE session_id=%s "
            "AND layer<>%s ORDER BY session_order"
        ):
            return self.sessions[params[0]]
        if query == (
            "SELECT * FROM swe_conversation_messages WHERE archive_chat_id=%s "
            "ORDER BY archive_order"
        ):
            return self.archives[params[0]]
        prefix = "SELECT * FROM swe_conversation_publications WHERE id IN ("
        if query.startswith(prefix):
            return [
                self.publications[key]
                for key in params
                if key in self.publications
            ]
        raise RuntimeError("unsupported detached message evidence lookup")


async def collect_message_evidence(
    database,
    manifest,
    workspace,
    *,
    memory_bytes,
):
    sessions = {}
    archives = {}
    remaining = memory_bytes // 16384
    assignments = manifest.data["assignments"]
    for session in workspace["sessions"]:
        sid = assignments["session"][session["position"]]["target_id"]
        sessions[sid] = await database.fetch_all(
            "SELECT * FROM swe_conversation_messages WHERE session_id=%s "
            "AND layer<>%s ORDER BY session_order LIMIT %s",
            (sid, "archive", remaining + 1),
        )
        remaining -= len(sessions[sid])
        if remaining < 0:
            raise ValueError(
                "workspace SQL evidence exceeds migration memory allowance",
            )
    for chat in workspace["chats"]:
        cid = assignments["chat"][chat["position"]]["target_id"]
        archives[cid] = await database.fetch_all(
            "SELECT * FROM swe_conversation_messages WHERE archive_chat_id=%s "
            "ORDER BY archive_order LIMIT %s",
            (cid, remaining + 1),
        )
        remaining -= len(archives[cid])
        if remaining < 0:
            raise ValueError(
                "workspace SQL evidence exceeds migration memory allowance",
            )
    identifiers = sorted(
        {
            row["publication_id"]
            for rows in (*sessions.values(), *archives.values())
            for row in rows
        },
    )
    publications = {}
    for start in range(0, len(identifiers), 128):
        chunk = identifiers[start : start + 128]
        rows = await database.fetch_all(
            "SELECT id,service_unit_key,tenant_key,source_key,agent_key,scope_kind,operation_key,relative_path,byte_length FROM swe_conversation_publications WHERE id IN ("
            + ",".join(["%s"] * len(chunk))
            + ") AND state='committed' AND gc_state='live'",
            tuple(chunk),
        )
        publications.update((row["id"], row) for row in rows)
    return MessageEvidence(sessions, archives, publications)


async def verify_workspace_messages(
    database,
    stores,
    manifest,
    inventory,
    report,
    *,
    io,
    workers,
    io_workers=4,
    memory_bytes=64 * 1024 * 1024,
    health_check=None,
):
    # Import lazily to avoid a module cycle with the existing semantic checks.
    from .verify import (
        _checked,
        _verify_session_messages,
        _verify_archive_rows,
    )

    results = {}

    async def prepare(index):
        workspace = inventory["workspaces"][index]
        return await collect_message_evidence(
            database,
            manifest,
            workspace,
            memory_bytes=memory_bytes,
        )

    async def check(index, request):
        workspace = inventory["workspaces"][index]
        evidence = await request("collect_workspace_messages", index)
        bundles = stores[workspace["scope"][0]]
        local = {"checked": [], "blockers": []}
        for session in workspace["sessions"]:
            sid = manifest.data["assignments"]["session"][session["position"]][
                "target_id"
            ]
            await _checked(
                local,
                session["position"],
                _verify_session_messages(
                    evidence,
                    bundles,
                    workspace,
                    session,
                    sid,
                    io=io,
                    io_workers=io_workers,
                ),
            )
        from ..identity import WorkspaceScope

        scope = WorkspaceScope.from_values(workspace["scope"])
        for chat in workspace["chats"]:
            await _checked(
                local,
                chat["position"],
                _verify_archive_rows(
                    evidence,
                    bundles,
                    manifest,
                    chat,
                    scope,
                    io=io,
                    io_workers=io_workers,
                ),
            )
        results[index] = local["blockers"]

    await MigrationCoordinator(
        {"collect_workspace_messages": prepare},
        workers=workers,
        health_check=health_check,
    ).run(range(len(inventory["workspaces"])), check)
    for index in range(len(inventory["workspaces"])):
        report["blockers"].extend(results[index])


class PublicationEvidence:
    """A detached publication lookup, not a SQL executor."""

    def __init__(self, rows):
        self.rows = rows

    async def fetch_one(self, _query, params):
        return self.rows.get(params[0])


def _auxiliary_jobs(inventory):
    from .importer import artifact_is_reconstructed

    for workspace in inventory["workspaces"]:
        for chat in workspace["chats"]:
            if chat["archive"]["epochs"]:
                yield ("epochs", workspace, chat)
    for item in inventory.get("files", []):
        workspace = inventory["workspaces"][item["workspace"]]
        if not artifact_is_reconstructed(item, workspace):
            yield ("artifact", workspace, item)
    for share in inventory["shares"]:
        yield ("share", None, share)


async def verify_auxiliary_bodies(
    database,
    stores,
    manifest,
    inventory,
    report,
    *,
    io,
    workers,
    memory_bytes=64 * 1024 * 1024,
    health_check=None,
):
    from .verify import _checked, _verify_epochs, _verify_artifact, _payload
    from ..identity import WorkspaceScope

    publication_query = (
        "SELECT id,service_unit_key,tenant_key,source_key,agent_key,scope_kind,"
        "operation_key,relative_path,byte_length,checkpoint_revision,"
        "checkpoint_event_watermark FROM swe_conversation_publications WHERE id=%s"
    )

    async def prepare(job):
        kind, workspace, value = job
        if kind == "share":
            row = await database.fetch_one(
                "SELECT * FROM swe_chat_shares WHERE token=%s",
                (value["record"]["token"],),
            )
            rows = {}
            if row is not None:
                publication = await database.fetch_one(
                    publication_query
                    + " AND state='committed' AND gc_state='live'",
                    (row["publication_id"],),
                )
                if publication is not None:
                    rows[publication["id"]] = publication
            return row, PublicationEvidence(rows)
        if kind == "artifact":
            positions = [
                "service:"
                + workspace["scope"][0]
                + "/source-artifact:"
                + str(value["workspace"])
                + "/"
                + value["path"],
            ]
        else:
            positions = [
                value["position"] + "/epoch:" + str(epoch["epoch"])
                for epoch in value["archive"]["epochs"]
            ]
        rows = {}
        if len(positions) > memory_bytes // 16384:
            raise ValueError(
                "epoch SQL evidence exceeds migration memory allowance",
            )
        for position in positions:
            assignment = manifest.data["assignments"]["publication"][position]
            query = publication_query
            if kind == "artifact":
                query += " AND state='committed' AND gc_state='live'"
            row = await database.fetch_one(query, (assignment["target_id"],))
            if row is not None:
                rows[row["id"]] = row
        return (
            positions[0] if kind == "artifact" else value["position"]
        ), PublicationEvidence(rows)

    async def check(job, request):
        kind, workspace, value = job
        prepared, evidence = await request("collect_auxiliary_body", job)
        if kind == "share":
            position = value["position"]

            async def compare_share():
                if prepared is None:
                    raise ValueError("permanent share token missing")
                body = await _payload(
                    evidence,
                    stores[value["service_unit_id"]],
                    prepared,
                    "byte_offset",
                    "byte_length",
                    service_unit_id=value["service_unit_id"],
                    io=io,
                )
                if body != value["snapshot"]:
                    raise ValueError("complete share snapshot differs")

            check_body = compare_share()
        elif kind == "artifact":
            position = prepared
            check_body = _verify_artifact(
                evidence,
                stores[workspace["scope"][0]],
                manifest,
                inventory,
                value,
                io=io,
            )
        else:
            position = prepared
            check_body = _verify_epochs(
                evidence,
                stores[workspace["scope"][0]],
                manifest,
                value,
                WorkspaceScope.from_values(workspace["scope"]),
                io=io,
            )
        local = {"checked": [], "blockers": []}
        await _checked(local, position, check_body)
        # A single event-loop append is atomic; sort only after the barrier.
        results.append((kind, local))

    results = []
    await MigrationCoordinator(
        {"collect_auxiliary_body": prepare},
        workers=workers,
        health_check=health_check,
    ).run(_auxiliary_jobs(inventory), check)
    for kind, local in sorted(
        results,
        key=lambda item: (
            item[0],
            item[1]["checked"] or [b["position"] for b in item[1]["blockers"]],
        ),
    ):
        report["blockers"].extend(local["blockers"])
        if kind == "artifact":
            report["checked"].extend(local["checked"])
