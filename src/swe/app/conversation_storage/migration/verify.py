# -*- coding: utf-8 -*-
"""Full semantic comparisons from committed SQL locators and immutable bodies."""

from ..chat_repository import MySQLChatRepository
from ..chat_mutations import load_metadata_json
from ..identity import WorkspaceScope
from ..session_adapter import MySQLSessionAdapter
from .importer import validate_inventory
from .install_records import OfflineImportStore
from ..shares import ShareStorage
from ..errors import StorageError
from mysql.connector.errors import Error as MySQLError
from ..history import HistoryStore
from ..archive_reader import ArchiveReader
from .importer import archive_batches, message_body, task_projections
from .manifest import SourceBoundary
from .readonly import ReadOnlyDatabase, ReadOnlyRetention, read_only_adapter

VERIFY_ERRORS = (
    ValueError,
    KeyError,
    TypeError,
    OSError,
    StorageError,
    MySQLError,
)


async def _payload(database, bundles, row, offset, length):
    publication = await database.fetch_one(
        "SELECT * FROM swe_conversation_publications WHERE id=%s AND state='committed' AND gc_state='live'",
        (row["publication_id"],),
    )
    if publication is None:
        raise ValueError("referenced publication is not committed/live")
    value = bundles.read_extent(
        publication["relative_path"].decode(),
        row[offset],
        row[length],
        expected_length=publication["byte_length"],
    )
    return load_metadata_json(value)


async def verify_import(database, bundles, manifest, inventory, target):
    report = {
        "version": 1,
        "migration_id": manifest.migration_id,
        "checked": [],
        "blockers": [],
        "passed": False,
    }
    try:
        validate_inventory(inventory)
        from pathlib import Path

        binding = {
            "schema": target.schema,
            "nas_root": str(bundles.root.resolve()),
            "migration_id": manifest.migration_id,
        }
        if manifest.data.get("target") != binding or manifest.data[
            "source_boundary"
        ] != inventory.get("source_boundary"):
            raise ValueError(
                "verification target or source boundary differs from manifest",
            )
        expected = [
            (
                item["tenant_id"],
                item["source_id"],
                item["agent_id"],
                str(Path(item["root"]).resolve()),
            )
            for item in manifest.data["workspaces"]
        ]
        actual = [
            (*item["scope"], item["root"]) for item in inventory["workspaces"]
        ]
        if expected != actual:
            raise ValueError(
                "verification workspace mapping differs from manifest",
            )
        await OfflineImportStore(database, bundles, target).validate_target()
        database = ReadOnlyDatabase(
            database,
            SourceBoundary(**manifest.data["source_boundary"]),
        )
    except VERIFY_ERRORS as error:
        report["blockers"].append(
            {"position": "migration", "detail": str(error)},
        )
        return report
    for workspace in inventory["workspaces"]:
        for session in workspace["sessions"]:
            await _checked(
                report,
                session["position"],
                _verify_session(
                    database,
                    bundles,
                    manifest,
                    workspace,
                    session,
                ),
            )
        for chat in workspace["chats"]:
            await _checked(
                report,
                chat["position"],
                _verify_chat(database, bundles, manifest, workspace, chat),
            )
    for share in inventory["shares"]:
        await _checked(
            report,
            share["position"],
            _verify_share(database, bundles, share),
        )
    for item in inventory.get("files", []):
        position = (
            "source-artifact:" + str(item["workspace"]) + "/" + item["path"]
        )
        await _checked(
            report,
            position,
            _verify_artifact(database, bundles, manifest, inventory, item),
        )
    report["passed"] = not report["blockers"]
    if report["passed"]:
        for position, state in list(manifest.data["batches"].items()):
            if state == "committed":
                manifest.set_batch_state(position, "verified")
    return report


async def _verify_artifact(database, bundles, manifest, inventory, item):
    from pathlib import Path

    position = "source-artifact:" + str(item["workspace"]) + "/" + item["path"]
    assignment = manifest.data["assignments"]["publication"][position]
    publication = await database.fetch_one(
        "SELECT * FROM swe_conversation_publications WHERE id=%s AND state='committed' AND gc_state='live'",
        (assignment["target_id"],),
    )
    if (
        publication is None
        or publication["operation_key"].decode() != assignment["operation_key"]
    ):
        raise ValueError(
            "retained legacy artifact publication identity differs",
        )
    workspace = inventory["workspaces"][item["workspace"]]
    expected = {
        "version": 1,
        "workspace": item["workspace"],
        "scope": workspace["scope"],
        "relative_path": item["path"],
        "bytes": item["bytes"],
        "snapshot_id": inventory["source_boundary"]["snapshot_id"],
    }
    index = bundles.inspect(
        assignment["relative_path"],
        expected_length=publication["byte_length"],
    )
    meta = index.blocks["legacy_artifact_metadata"]
    body = index.blocks["legacy_artifact"]
    if (
        load_metadata_json(
            bundles.read_extent(index.relative_path, meta.offset, meta.length),
        )
        != expected
    ):
        raise ValueError("retained legacy artifact source identity differs")
    source = (Path(workspace["root"]) / item["path"]).read_bytes()
    if (
        len(source) != item["bytes"]
        or bundles.read_extent(index.relative_path, body.offset, body.length)
        != source
    ):
        raise ValueError("retained native legacy artifact bytes differ")


async def _checked(report, position, check):
    try:
        await check
        report["checked"].append(position)
    except VERIFY_ERRORS as error:
        report["blockers"].append({"position": position, "detail": str(error)})


async def _verify_session(database, bundles, manifest, workspace, session):
    scope = WorkspaceScope(*workspace["scope"])
    identity = session["identity"]
    adapter = MySQLSessionAdapter(
        database,
        scope,
        bundles,
        channel=identity[4],
    )
    read_only_adapter(adapter)
    actual = await adapter.get_session_state_dict(
        identity[5],
        identity[3],
        False,
    )
    expected = session["legacy"]["state"]
    if actual != {**expected, "revision": expected.get("revision", 0)}:
        raise ValueError("complete Session recovery modules differ")
    sid = manifest.data["assignments"]["session"][session["position"]][
        "target_id"
    ]
    await _verify_session_messages(database, bundles, workspace, session, sid)
    await _verify_turns(adapter, identity, expected)


async def _verify_turns(adapter, identity, expected):
    for key, value in expected.get("turn_states", {}).items():
        if value.get("chat_id") is not None:
            actual_turn = await adapter.get_turn_state(
                identity[5],
                identity[3],
                {},
                key,
            )
            if actual_turn != value:
                raise ValueError("runtime cold Turn state differs")


async def _verify_session_messages(database, bundles, workspace, session, sid):
    identity = session["identity"]
    expected = session["legacy"]["state"]
    rows = await database.fetch_all(
        "SELECT * FROM swe_conversation_messages WHERE session_id=%s AND layer<>%s ORDER BY session_order",
        (sid, "archive"),
    )
    from .importer import extract_messages

    messages = extract_messages(
        expected,
        context_epoch=expected.get("context_epoch", 0),
    )
    session_chats = [
        item
        for item in workspace["chats"]
        if item["spec"]["session_id"] == identity[5]
        and item["spec"]["user_id"] == identity[3]
        and item["spec"]["channel"] == identity[4]
    ]
    if any(
        item["spec"]["meta"].get("session_kind") == "task"
        for item in session_chats
    ):
        task_projections(messages, expected)
    if len(rows) != len(messages):
        raise ValueError("Session Message occurrence count differs")
    for row, message in zip(rows, messages):
        body = await _payload(
            database,
            bundles,
            row,
            "byte_offset",
            "byte_length",
        )
        if (
            body != message_body(message)
            or row["layer"] != message["layer"]
            or row["source_occurrence_no"] != message["source_occurrence_no"]
        ):
            raise ValueError("Message role/body/order/occurrence differs")


async def _verify_chat(database, bundles, manifest, workspace, chat):
    scope = WorkspaceScope(*workspace["scope"])
    actual = await read_only_adapter(
        MySQLChatRepository(database, scope, bundles),
    ).get_chat(
        chat["spec"]["id"],
    )
    if chat["archive"]["deleted"]:
        if actual is not None:
            raise ValueError("deleted Chat was resurrected")
    elif actual is None or actual.model_dump(mode="json") != {
        **chat["spec"],
        "status": "idle",
    }:
        raise ValueError("Chat metadata/binding/identity/time differs")
    await _verify_archive_rows(database, bundles, manifest, chat)
    await _verify_epochs(database, bundles, manifest, chat)
    if not chat["archive"]["deleted"]:
        await _verify_runtime_chat(database, bundles, scope, chat, workspace)


async def _verify_archive_rows(database, bundles, manifest, chat):
    cid = manifest.data["assignments"]["chat"][chat["position"]]["target_id"]
    rows = await database.fetch_all(
        "SELECT * FROM swe_conversation_messages WHERE archive_chat_id=%s ORDER BY archive_order",
        (cid,),
    )
    expected = [
        message
        for batch in archive_batches(chat["archive"])
        for message in batch["messages"]
    ]
    if len(rows) != len(expected):
        raise ValueError("archive occurrence count differs")
    for row, message in zip(rows, expected):
        if (
            await _payload(
                database,
                bundles,
                row,
                "byte_offset",
                "byte_length",
            )
            != message
        ):
            raise ValueError("archive message order/body differs")


async def _verify_epochs(database, bundles, manifest, chat):
    for epoch in chat["archive"]["epochs"]:
        position = chat["position"] + "/epoch:" + str(epoch["epoch"])
        assignment = manifest.data["assignments"]["publication"][position]
        publication = await database.fetch_one(
            "SELECT * FROM swe_conversation_publications WHERE id=%s",
            (assignment["target_id"],),
        )
        if (
            publication["checkpoint_revision"] != epoch["record"]["revision"]
            or publication["checkpoint_event_watermark"]
            != epoch["record"]["applied_event_sequence"]
        ):
            raise ValueError("checkpoint epoch/revision/watermark differs")
        index = bundles.inspect(assignment["relative_path"])
        extent = index.blocks["legacy_epoch"]
        actual_epoch = load_metadata_json(
            bundles.read_extent(
                index.relative_path,
                extent.offset,
                extent.length,
            ),
        )
        if actual_epoch != epoch:
            raise ValueError("full checkpoint/event/epoch semantics differ")


async def _verify_share(database, bundles, share):
    actual = await database.fetch_one(
        "SELECT * FROM swe_chat_shares WHERE token=%s",
        (share["record"]["token"],),
    )
    if actual is None:
        raise ValueError("permanent share token missing")
    for key, value in share["record"].items():
        if hasattr(actual[key], "isoformat"):
            from ..chat_mutations import utc_datetime

            if actual[key] != utc_datetime(value):
                raise ValueError("share timestamp differs")
        elif actual[key] != value:
            raise ValueError("share token/ownership/counter differs")
    if (
        await _payload(database, bundles, actual, "byte_offset", "byte_length")
        != share["snapshot"]
    ):
        raise ValueError("complete share snapshot differs")
    if (
        await ShareStorage(
            database,
            bundles,
            retention_factory=ReadOnlyRetention,
        ).verify_snapshot(
            share["record"]["token"],
        )
        != share["snapshot"]
    ):
        raise ValueError("runtime share snapshot differs")


async def _verify_runtime_chat(database, bundles, scope, chat, workspace):
    from types import SimpleNamespace
    from swe.app.runner import api
    from swe.app.runner.models import ChatSpec
    from swe.app.runner.turn_ledger import TurnHistorySnapshot

    spec = ChatSpec.model_validate(chat["spec"])
    history = HistoryStore(
        database,
        scope,
        bundles,
        cursor_secret=b"offline-verification",
    )
    read_only_adapter(history)
    actual = await history.current(spec.id)
    session = next(
        item
        for item in workspace["sessions"]
        if item["identity"][3:]
        == [spec.user_id, spec.channel, spec.session_id]
    )
    expected = await api._build_chat_history(
        spec,
        session=None,
        workspace=SimpleNamespace(workspace_dir=None),
        state=TurnHistorySnapshot(session["legacy"]["state"], spec.id),
        status_override="idle",
    )
    if [item.model_dump(mode="json") for item in actual.messages] != [
        item.model_dump(mode="json") for item in expected.messages
    ]:
        raise ValueError("runtime History current display semantics differ")
    await _verify_answer_turns(history, spec, expected.messages)
    await _verify_archived_questions(history, spec, expected.messages, chat)
    await _verify_archive_page(history, spec, chat)
    await _verify_checkpoint(database, bundles, scope, spec.id, chat)


async def _verify_answer_turns(history, spec, messages):
    from swe.app.runner import api

    questions = {
        api._message_original_id(message)
        for message in messages
        if message.role == "user"
    }
    for question in questions:
        if not question:
            continue
        expected = api._slice_answer_turn(messages, msgid=question)
        if expected is None:
            continue
        actual = await history.answer_turn(
            question,
            chat_id=spec.id,
            user_id=spec.user_id,
        )
        if [
            message.model_dump(mode="json") for message in actual.messages
        ] != [message.model_dump(mode="json") for message in expected]:
            raise ValueError(
                "runtime answer Turn source/fragment semantics differ",
            )


async def _verify_archived_questions(history, spec, messages, chat):
    from swe.app.runner import api

    current = {
        api._message_original_id(message)
        for message in messages
        if message.role == "user"
    }
    archived = {
        message.get("id")
        for batch in archive_batches(chat["archive"])
        for message in batch["messages"]
        if message.get("role") == "user"
    }
    for question in archived - current - {None, ""}:
        try:
            await history.answer_turn(
                question,
                chat_id=spec.id,
                user_id=spec.user_id,
            )
        except KeyError:
            continue
        raise ValueError(
            "fully archived question was selectable as current answer Turn",
        )


async def _verify_archive_page(history, spec, chat):
    from swe.app.runner import api

    observed = []
    cursor = None
    while True:
        page = await history.archive_page(spec.id, before=cursor, limit=50)
        observed[0:0] = page.messages
        if not page.has_more:
            break
        cursor = page.next_cursor
    rows = [
        message
        for batch in archive_batches(chat["archive"])
        for message in batch["messages"]
    ]
    converted = []
    for index, message in enumerate(rows):
        from agentscope.message import Msg

        converted.extend(
            api._history_msg_to_messages(
                [Msg.from_dict(message)],
                session_id=spec.id,
                position_offset=index,
            ),
        )
    converted = api._redact_hidden_context_messages(converted)
    if [item.model_dump(mode="json") for item in observed] != [
        item.model_dump(mode="json") for item in converted
    ]:
        raise ValueError("runtime History archive page semantics differ")


async def _verify_checkpoint(database, bundles, scope, chat_id, chat):
    if not chat["archive"]["epochs"]:
        return
    current = chat["archive"]["epochs"][0]
    checkpoint = await read_only_adapter(
        ArchiveReader(database, scope, bundles),
    ).checkpoint(
        chat_id,
    )
    events = [
        event
        for event in current["events"]
        if event["epoch"] == current["epoch"]
        and event["sequence"] > current["record"]["applied_event_sequence"]
    ]
    if (
        checkpoint.record.to_dict() != current["record"]
        or [event.to_dict() for event in checkpoint.events] != events
    ):
        raise ValueError("runtime checkpoint/event state differs")
