# -*- coding: utf-8 -*-
"""Offline semantic extraction and bounded immutable migration batches."""

from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path

from agentscope.message import Msg


def validate_inventory(report):
    if (
        report.get("version") != 2
        or report.get("blockers")
        or not report.get("ready")
    ):
        raise ValueError(
            "migration inventory contains blockers or is incomplete",
        )
    if not isinstance(report.get("workspaces"), list) or not isinstance(
        report.get("shares"),
        list,
    ):
        raise ValueError("migration inventory is incomplete")

    from ..identity import WorkspaceScope, service_unit_bytes

    for item in report["workspaces"]:
        WorkspaceScope.from_values(item["scope"])
        for session in item.get("sessions", []):
            identity = session["identity"]
            if len(identity) != 7 or identity[:4] != item["scope"]:
                raise ValueError(
                    "Session identity differs from explicit service scope",
                )
    tokens = set()
    for item in report["shares"]:
        token = item["record"]["token"]
        if token in tokens:
            raise ValueError("duplicate permanent share token")
        tokens.add(token)
        service_unit_bytes(item["service_unit_id"])
        if item["record"].get("service_unit_id") != item["service_unit_id"]:
            raise ValueError("share service ownership mismatch")


def artifact_is_reconstructed(item, workspace):
    """Whether canonical SQL/content data already recreates this source file."""
    from pathlib import Path

    path = Path(item["path"])
    if path == Path("chats.json"):
        return True
    if len(path.parts) == 3 and path.parts[0] == "dialog":
        chat_id, filename = path.parts[1:]
        if filename.endswith(".jsonl"):
            boundary_id = filename[: -len(".jsonl")]
            return any(
                chat["spec"]["id"] == chat_id
                and boundary_id
                in {
                    batch["boundary"]["id"]
                    for epoch in chat["archive"].get("epochs", [])
                    for batch in epoch.get("batches", [])
                }
                for chat in workspace["chats"]
            )
    for session in workspace["sessions"]:
        if path != Path(session["path"]):
            continue
        root = session["legacy"]["root"]
        return not any(
            key in root
            for key in (
                "session_generation",
                "turn_ledger_version",
                "ledger_nonce",
                "next_admission_order",
                "cold_turn_count",
                "ledger_latest",
            )
        )
    return False


def _entry(
    payload,
    layer,
    index,
    context_epoch,
    *,
    turn_key=None,
    chat_key=None,
):
    return {
        "payload": deepcopy(payload),
        "layer": layer,
        "role": payload.get("role")
        or ("user" if layer == "anchor" else "assistant"),
        "source_msg_key": payload.get("id"),
        "source_occurrence_no": index,
        "context_epoch": context_epoch,
        "in_context": layer == "memory",
        "turn_key": turn_key,
        "chat_key": chat_key,
    }


def extract_messages(state, *, context_epoch):
    memory = state.get("agent", {}).get("memory", {})
    raw = memory.get("content", [])
    if not isinstance(raw, list):
        raise ValueError("legacy memory content is invalid")
    messages = []
    for index, value in enumerate(raw):
        # AgentScope versions store either raw Msg dictionaries or [Msg, mark].
        payload = (
            value[0] if isinstance(value, (list, tuple)) and value else value
        )
        if not isinstance(payload, dict):
            raise ValueError("legacy memory message is invalid")
        try:
            Msg.from_dict(payload)
        except (KeyError, TypeError, ValueError) as error:
            raise ValueError("legacy memory message is invalid") from error
        messages.append(_entry(payload, "memory", index, context_epoch))
    for key, layer in (
        ("task_messages", "task"),
        ("model_call_failed_messages", "error"),
    ):
        values = state.get(key, [])
        if not isinstance(values, list) or not all(
            isinstance(value, dict) for value in values
        ):
            raise ValueError("legacy supplemental message list is invalid")
        for index, payload in enumerate(values):
            messages.append(_entry(payload, layer, index, context_epoch))
    messages.extend(_turn_anchors(state, context_epoch))
    return messages


def display_order_messages(messages):
    """Stably interleave primary layers; preserve fallback anchor order."""
    from types import SimpleNamespace
    from ...runner import api

    def key(item):
        return api._message_sort_key(
            SimpleNamespace(timestamp=item["payload"].get("timestamp")),
        )

    return [
        *sorted(
            (item for item in messages if item["layer"] != "anchor"),
            key=key,
        ),
        *(item for item in messages if item["layer"] == "anchor"),
    ]


def _turn_anchors(state, context_epoch):
    messages = []
    turns = state.get("turn_states", {})
    if not isinstance(turns, dict):
        raise ValueError("legacy Turn states are invalid")
    for index, (key, turn) in enumerate(turns.items()):
        if not isinstance(turn, dict):
            raise ValueError("legacy Turn state is invalid")
        payload = turn.get("message")
        if payload is not None:
            if not isinstance(payload, dict):
                raise ValueError("legacy Turn question is invalid")
            messages.append(
                _entry(
                    payload,
                    "anchor",
                    index,
                    context_epoch,
                    turn_key=key,
                    chat_key=turn.get("chat_id"),
                ),
            )
    return messages


def archive_batches(archive):
    from swe.agents.memory.conversation_archive import ConversationArchiveStore

    merged = {}
    for epoch in reversed(archive["epochs"]):
        for batch in epoch["batches"]:
            key = batch["boundary"]["id"]
            if key in merged and merged[key] != batch:
                raise ValueError(
                    "retained archive boundary content changed across epochs",
                )
            merged[key] = batch
    result = []
    evidence = (
        archive["epochs"][0].get("evidence", {}) if archive["epochs"] else {}
    )
    for key, batch in merged.items():
        epochs = {
            evidence.get(
                ConversationArchiveStore._evidence_epoch_key(
                    key,
                    index,
                    message["id"],
                ),
                1,
            )
            for index, message in enumerate(batch["messages"])
        }
        if len(epochs) != 1:
            raise ValueError(
                "archive boundary has mixed unsupported evidence epochs",
            )
        result.append({**batch, "epoch": epochs.pop()})
    return result


def task_projections(messages, state):
    from swe.app.runner import api

    memory = [item for item in messages if item["layer"] == "memory"]
    runs = api._normalize_task_runs(state.get("task_runs", []), len(memory))
    if runs is None:
        return
    cursor = 0
    for start, end, index, run_id in runs:
        if start < cursor:
            return
        cursor = end
    for start, end, index, run_id in runs:
        converted = [
            api._history_msg_to_messages(
                [Msg.from_dict(item["payload"])],
                position_offset=item["source_occurrence_no"],
            )
            for item in memory[start:end]
        ]
        candidates = [
            offset
            for offset, values in enumerate(converted)
            if any(
                value.role == "assistant"
                and api._message_has_text_content(value)
                for value in values
            )
        ]
        if not candidates:
            continue
        final = candidates[-1]
        if len(converted[final]) != 1:
            raise ValueError(
                "task run final source expands to multiple messages; projection unsupported",
            )
        for offset, item in enumerate(memory[start:end]):
            item["history_projection"] = {
                "task_run_id": run_id,
                "task_run_index": index,
                "task_run_section": "final" if offset == final else "step",
            }


def message_body(item):
    if "history_projection" not in item:
        return item["payload"]
    return {
        "version": 1,
        "message": item["payload"],
        "history_projection": item["history_projection"],
    }


@dataclass(frozen=True)
class ImportBatch:
    updates: tuple = ()
    locks: dict | None = None
    scope_kind: str = "workspace"
    session_id: int | None = None
    chat_id: int | None = None
    turn_id: int | None = None
    commit_plan: object | None = None
    kind: str = "offline_import"
    epoch: int | None = None


class ConversationImporter:
    """Bounded, resumable import into the manifest's exclusive offline target."""

    def __init__(
        self,
        database,
        bundles,
        manifest,
        target,
        *,
        batch_size=128,
        store_factory=None,
    ):
        from .install_records import OfflineImportStore

        if not 1 <= batch_size <= 512:
            raise ValueError("migration batch size must be between 1 and 512")
        if target.migration_id != manifest.migration_id:
            raise ValueError("migration target identity differs from manifest")
        self.db = database
        self.bundle_stores = bundle_stores(bundles, manifest)
        self.bundles = None
        self.manifest = manifest
        self.target = target
        self.batch_size = batch_size
        manifest.bind_target(
            {
                "schema": target.schema,
                "nas_roots": {
                    key: str(store.root.resolve())
                    for key, store in self.bundle_stores.items()
                },
                "migration_id": target.migration_id,
            },
        )
        self.stores = {
            key: (store_factory or OfflineImportStore)(
                database,
                store,
                target,
                service_unit_id=key,
            )
            for key, store in self.bundle_stores.items()
        }
        self.store = None
        self.session_ids = {}
        self.chat_ids = {}
        self.turn_ids = {}

    def assigned(self, kind, position):
        return self.manifest.allocate(kind, position)["target_id"]

    async def _allocate(self, kind, position):
        return self.manifest.allocate(kind, position)

    async def _assigned(self, kind, position):
        return (await self._allocate(kind, position))["target_id"]

    async def _set_batch_state(self, position, state):
        self.manifest.set_batch_state(position, state)

    async def _batch_finished(self, position):
        self._batch_committed(position)

    def _assert_source_fresh(self, report):
        from .freshness import changed_source, tenant_key

        timestamps = self.manifest.data.get("tenant_sync_started_at_ns", {})
        for workspace in report["workspaces"]:
            service_unit_id, tenant_id = workspace["scope"][:2]
            cutoff = timestamps.get(tenant_key(service_unit_id, tenant_id))
            if cutoff is None:
                continue
            changed = changed_source(workspace.get("source_paths", []), cutoff)
            if changed:
                path, modified_at_ns = changed
                blocker = {
                    "code": "source_modified_after_sync_started",
                    "position": workspace["position"],
                    "scope": [service_unit_id, tenant_id],
                    "path": str(path),
                    "mtime_ns": modified_at_ns,
                    "cutoff_ns": cutoff,
                }
                self.manifest.record_source_change(blocker)
                raise ValueError(
                    "tenant source changed after synchronization started: "
                    + str(path),
                )
        for share in report["shares"]:
            service_unit_id = share["service_unit_id"]
            tenant_id = share["record"]["tenant_id"]
            cutoff = timestamps.get(tenant_key(service_unit_id, tenant_id))
            if cutoff is None or not share.get("snapshot_path"):
                continue
            changed = changed_source(
                [
                    share["snapshot_path"],
                    str(Path(share["snapshot_path"]).parent),
                ],
                cutoff,
            )
            if changed:
                path, modified_at_ns = changed
                blocker = {
                    "code": "source_modified_after_sync_started",
                    "position": share["position"],
                    "scope": [service_unit_id, tenant_id],
                    "path": str(path),
                    "mtime_ns": modified_at_ns,
                    "cutoff_ns": cutoff,
                }
                self.manifest.record_source_change(blocker)
                raise ValueError(
                    "tenant source changed after synchronization started: "
                    + str(path),
                )

    async def _publish(self, position, scope, rows, blocks, *, spec=None):
        self.bundles = self.bundle_stores[scope.service_unit_id]
        self.store = self.stores[scope.service_unit_id]
        spec = spec or ImportBatch()
        updates, locks, scope_kind = spec.updates, spec.locks, spec.scope_kind
        session_id, chat_id, turn_id = (
            spec.session_id,
            spec.chat_id,
            spec.turn_id,
        )
        commit_plan, kind, epoch = spec.commit_plan, spec.kind, spec.epoch
        from ..publications import PublicationRequest, CommitPlan
        from .install_records import plan_block

        assignment = await self._allocate("publication", position)
        operation = assignment["operation_key"]
        request = PublicationRequest(
            operation,
            scope,
            kind,
            self.manifest.migration_id,
            0,
            session_id=session_id,
            chat_id=chat_id,
            turn_id=turn_id,
            scope_kind=scope_kind,
            epoch=epoch,
        )
        plan = commit_plan or CommitPlan(result_ids=(assignment["target_id"],))
        body = plan_block(self.target, rows, updates, locks)
        current = await self.store.reserve_batch(
            request,
            assignment["target_id"],
            plan,
        )
        if current["state"] == "committed":
            from ..chat_mutations import load_metadata_json

            await self.store.verify_batch(
                request,
                assignment["target_id"],
                plan,
                load_metadata_json(body.content),
            )
            await self.store.verify_blocks(current, [body, *blocks])
            await self._batch_finished(position)
            return current
        await self._set_batch_state(position, "importing")
        if not current.get("durable_at"):
            path = self.bundles.root / assignment["relative_path"]
            if path.exists():
                await self.store.confirm_bundle(operation)
            else:
                await self.store.write_bundle(operation, [body, *blocks])
        await self.store.verify_blocks(
            await self.store.get(operation),
            [body, *blocks],
        )
        result = await self.store.commit(operation)
        await self._batch_finished(position)
        return result

    def _batch_committed(self, position):
        state = self.manifest.data["batches"].get(position, "planned")
        if state in ("planned", "blocked"):
            self.manifest.set_batch_state(position, "importing")
        if self.manifest.data["batches"].get(position) != "verified":
            self.manifest.set_batch_state(position, "committed")

    async def run(self, report):
        from ..identity import WorkspaceScope

        validate_inventory(report)
        self._validate_source_binding(report)
        self._assert_source_fresh(report)
        self._validate_runtime_sources(report)
        await next(iter(self.stores.values())).validate_target()
        await self._retain_source_artifacts(report)
        for workspace in report["workspaces"]:
            scope = WorkspaceScope.from_values(workspace["scope"])
            await self._identities(workspace, scope)
            for session in workspace["sessions"]:
                await self._session(session, workspace, scope)
            for chat in workspace["chats"]:
                await self._archive(chat, scope)
        await self._shares(report)
        self._assert_source_fresh(report)
        return {
            "migration_id": self.manifest.migration_id,
            "batches": dict(self.manifest.data["batches"]),
        }

    def _validate_runtime_sources(self, report):
        for workspace in report["workspaces"]:
            for session in workspace["sessions"]:
                state = session["legacy"]["state"]
                _block("recovery", "recovery", state)
                extract_messages(
                    state,
                    context_epoch=state.get("context_epoch", 0),
                )
                for turn in state.get("turn_states", {}).values():
                    if turn.get("status") in (
                        "admitted",
                        "running",
                        "stopping",
                        "settling",
                        "unknown",
                    ):
                        raise ValueError(
                            "unsettled legacy Turn blocks offline import",
                        )
            for chat in workspace["chats"]:
                archive_batches(chat["archive"])

    async def _retain_source_artifacts(self, report):
        from pathlib import Path
        from ..bundle import BundleBlock, MAX_READ_BYTES
        from ..identity import WorkspaceScope
        import stat

        for item in report["files"]:
            workspace = report["workspaces"][item["workspace"]]
            if artifact_is_reconstructed(item, workspace):
                continue
            root = Path(workspace["root"]).resolve(strict=True)
            relative = Path(item["path"])
            if relative.is_absolute() or ".." in relative.parts:
                raise ValueError(
                    "legacy artifact path escapes bound source root",
                )
            path = root / relative
            if any(
                part.is_symlink()
                for part in (path, *path.parents)
                if part.is_relative_to(root)
            ):
                raise ValueError("legacy artifact path contains symlink")
            if (
                not path.resolve(strict=True).is_relative_to(root)
                or stat.S_IMODE(path.stat().st_mode) & 0o222
            ):
                raise ValueError(
                    "legacy artifact is not inside frozen read-only source",
                )
            if path.stat().st_size > MAX_READ_BYTES:
                raise ValueError(
                    "legacy artifact exceeds supported migration reader limit; split-file migration required",
                )
            with path.open("rb") as stream:
                raw = stream.read(MAX_READ_BYTES + 1)
            if len(raw) != item["bytes"]:
                raise ValueError(
                    "legacy artifact size changed after inventory",
                )
            metadata = {
                "version": 2,
                "workspace": item["workspace"],
                "scope": workspace["scope"],
                "relative_path": item["path"],
                "bytes": len(raw),
                "snapshot_id": report["source_boundary"]["snapshot_id"],
            }
            position = (
                "service:"
                + workspace["scope"][0]
                + "/source-artifact:"
                + str(item["workspace"])
                + "/"
                + item["path"]
            )
            await self._publish(
                position,
                WorkspaceScope.from_values(workspace["scope"]),
                [],
                [
                    BundleBlock("legacy_artifact", "legacy_artifact", raw),
                    _block(
                        "legacy_artifact_metadata",
                        "legacy_artifact_metadata",
                        metadata,
                    ),
                ],
            )

    def _validate_source_binding(self, report):
        from pathlib import Path

        if (
            report.get("source_boundary")
            != self.manifest.data["source_boundary"]
        ):
            raise ValueError(
                "inventory source boundary differs from fixed manifest",
            )
        expected = self.manifest.data["workspaces"]
        if len(expected) != len(report["workspaces"]):
            raise ValueError(
                "inventory workspace mapping differs from manifest",
            )
        for mapped, observed in zip(expected, report["workspaces"]):
            scope = [
                mapped[key]
                for key in (
                    "service_unit_id",
                    "tenant_id",
                    "source_id",
                    "agent_id",
                )
            ]
            if (
                scope != observed["scope"]
                or str(Path(mapped["root"]).resolve()) != observed["root"]
            ):
                raise ValueError(
                    "inventory workspace source mapping differs from manifest",
                )

    async def _identities(self, workspace, scope):
        from ..bundle import BundleBlock
        from ..chat_mutations import split_metadata, validate_light_metadata
        from swe.app.runner.models import ChatSpec
        from swe.app.runner.expert_binding import read_expert_binding
        import json

        for session in workspace["sessions"]:
            position = session["position"]
            identity = session["identity"]
            sid = await self._assigned("session", position)
            self.session_ids[position] = sid
            source = session["legacy"]["state"]
            now = self.manifest.data["source_boundary"]["captured_at"]
            values = {
                "id": sid,
                "service_unit_key": identity[0],
                "tenant_key": identity[1],
                "source_key": identity[2],
                "agent_key": identity[3],
                "user_key": identity[4],
                "channel_key": identity[5],
                "external_session_key": identity[6],
                "generation": 0,
                "state_revision": 0,
                "context_epoch": source.get("context_epoch", 0),
                "created_at": now,
                "updated_at": now,
            }
            rows = [{"table": "sessions", "values": values}]
            blocks = []
            for index in session["chat_positions"]:
                item = workspace["chats"][index]
                spec = ChatSpec.model_validate(item["spec"])
                cid = await self._assigned("chat", item["position"])
                self.chat_ids[item["position"]] = cid
                light, content = split_metadata(spec.meta)
                light_text = json.dumps(
                    light,
                    ensure_ascii=False,
                    separators=(",", ":"),
                    allow_nan=False,
                )
                validate_light_metadata(light_text)
                binding = read_expert_binding(spec)
                block = "content:" + str(cid)
                archive = item["archive"]
                epoch = (
                    archive["epochs"][0]["epoch"] if archive["epochs"] else 1
                )
                row = {
                    "id": cid,
                    "session_id": sid,
                    "external_chat_key": spec.id,
                    "external_session_key": spec.session_id,
                    "registry_order": item["registry_order"],
                    "service_unit_key": scope.service_unit_id,
                    "tenant_key": scope.tenant_id,
                    "source_key": scope.source_id,
                    "agent_key": scope.agent_id,
                    "user_key": spec.user_id,
                    "channel_key": spec.channel,
                    "title": spec.name,
                    "metadata_text": light_text,
                    "expert_id": binding.expert_id,
                    "expert_binding_revision": binding.revision,
                    "content_meta_publication_id": {"publication": True},
                    "content_meta_offset": {"block": block, "part": "offset"},
                    "content_meta_length": {"block": block, "part": "length"},
                    "epoch": epoch,
                    "status": "idle",
                    "created_at": spec.created_at.isoformat(),
                    "updated_at": spec.updated_at.isoformat(),
                    "deleted_at": now if archive["deleted"] else None,
                }
                rows.append({"table": "chats", "values": row})
                blocks.append(_block(block, "content_meta", content))
            await self._publish(position + "/identities", scope, rows, blocks)

    async def _session(self, session, workspace, scope):
        sid = self.session_ids[session["position"]]
        state = session["legacy"]["state"]
        epoch = state.get("context_epoch", 0)
        turns = state.get("turn_states", {})
        chats = {
            item["spec"]["id"]: self.chat_ids[item["position"]]
            for item in workspace["chats"]
            if item["position"] in self.chat_ids
            and item["spec"]["session_id"] == session["identity"][6]
            and item["spec"]["user_id"] == session["identity"][4]
            and item["spec"]["channel"] == session["identity"][5]
        }
        for key, value in turns.items():
            chat = chats.get(value.get("chat_id"))
            if chat is not None:
                self.turn_ids[(sid, key)] = await self._assigned(
                    "turn",
                    session["position"] + "/turn:" + key,
                )
        await self._turn_identities(session, scope, chats)
        messages = extract_messages(state, context_epoch=epoch)
        if any(
            item["spec"]["meta"].get("session_kind") == "task"
            for item in workspace["chats"]
            if item["spec"]["id"] in chats
        ):
            task_projections(messages, state)
        messages = display_order_messages(messages)
        for start in range(0, len(messages), self.batch_size):
            batch = messages[start : start + self.batch_size]
            rows = []
            blocks = []
            for index, item in enumerate(batch, start=start):
                position = session["position"] + "/message:" + str(index)
                mid = await self._assigned("message", position)
                block = "message:" + str(mid)
                turn = self.turn_ids.get((sid, item["turn_key"]))
                origin = chats.get(item["chat_key"])
                values = _message_row(
                    mid,
                    sid,
                    item,
                    index + 1,
                    block,
                    turn,
                    origin,
                    service_unit_id=scope.service_unit_id,
                )
                rows.append({"table": "messages", "values": values})
                blocks.append(_block(block, item["layer"], message_body(item)))
            await self._publish(
                session["position"] + "/messages:" + str(start),
                scope,
                rows,
                blocks,
                spec=ImportBatch(
                    locks={
                        "sessions": [sid],
                        "chats": list(chats.values()),
                        "turns": list(
                            self.turn_ids.get((sid, key))
                            for key in turns
                            if (sid, key) in self.turn_ids
                        ),
                    },
                    session_id=sid,
                ),
            )
        await self._root_and_turns(session, scope, chats, len(messages))

    async def _turn_identities(self, session, scope, chats):
        sid = self.session_ids[session["position"]]
        state = session["legacy"]["state"]
        rows = []
        for order, (key, value) in enumerate(
            state.get("turn_states", {}).items(),
            start=1,
        ):
            chat = chats.get(value.get("chat_id"))
            if chat is None:
                continue
            status = value.get("status")
            if status in (
                "admitted",
                "running",
                "stopping",
                "settling",
                "unknown",
            ):
                raise ValueError("unsettled legacy Turn blocks offline import")
            internal = {
                "completed": "succeeded",
                "stopped": "stopped",
                "cancelled": "stopped",
                "failed": "failed",
                "interrupted": "interrupted",
            }.get(status, "legacy")
            rows.append(
                {
                    "table": "turns",
                    "values": {
                        "id": self.turn_ids[(sid, key)],
                        "service_unit_key": scope.service_unit_id,
                        "session_id": sid,
                        "chat_id": chat,
                        "question_msg_key": key,
                        "turn_order": order,
                        "context_epoch": state.get("context_epoch", 0),
                        "generation": 0,
                        "state": internal,
                        "history_status": status,
                    },
                },
            )
        for row in rows:
            await self._publish(
                session["position"]
                + "/turn-identity:"
                + str(row["values"]["id"]),
                scope,
                [row],
                [],
                spec=ImportBatch(
                    locks={
                        "sessions": [sid],
                        "chats": [row["values"]["chat_id"]],
                    },
                    session_id=sid,
                    chat_id=row["values"]["chat_id"],
                ),
            )

    async def _root_and_turns(self, session, scope, chats, history_high):
        from ..commit_plan import CommitPlan

        sid = self.session_ids[session["position"]]
        state = deepcopy(session["legacy"]["state"])
        revision = state.get("revision", 0)
        if type(revision) is not int or revision < 0:
            raise ValueError("legacy Session revision is invalid")
        root_position = session["position"] + "/recovery"
        turns = list(state.get("turn_states", {}).items())
        for start in range(0, len(turns), self.batch_size):
            rows = []
            blocks = []
            for order, (key, value) in enumerate(
                turns[start : start + self.batch_size],
                start=start + 1,
            ):
                chat = chats.get(value.get("chat_id"))
                if chat is None:
                    continue
                tid = self.turn_ids[(sid, key)]
                block = "turn:" + str(tid)
                status = value.get("status")
                internal = {
                    "completed": "succeeded",
                    "stopped": "stopped",
                    "cancelled": "stopped",
                    "failed": "failed",
                    "interrupted": "interrupted",
                }.get(status)
                if status in (
                    "admitted",
                    "running",
                    "stopping",
                    "settling",
                    "unknown",
                ):
                    raise ValueError(
                        "unsettled legacy Turn blocks offline import",
                    )
                rows.append(
                    {
                        "table": "turns",
                        "values": {
                            "id": tid,
                            "admission_publication_id": {"publication": True},
                            "terminal_publication_id": (
                                {"publication": True} if internal else None
                            ),
                        },
                    },
                )
                # Point readers use complete Turn state, not a display-only projection.
                blocks.append(
                    _block(block, "recovery", {"turn_states": {key: value}}),
                )
            if rows:
                # Current Session adapter looks for the recovery block; one Turn
                # per publication preserves bounded point lookup and original ID.
                for (key, value), row, block in zip(
                    [
                        (key, value)
                        for key, value in turns[
                            start : start + self.batch_size
                        ]
                        if chats.get(value.get("chat_id")) is not None
                    ],
                    rows,
                    blocks,
                ):
                    chat = chats[value["chat_id"]]
                    block = type(block)("recovery", "recovery", block.content)
                    await self._publish(
                        session["position"]
                        + "/turn-record:"
                        + str(row["values"]["id"]),
                        scope,
                        [],
                        [block],
                        spec=ImportBatch(
                            updates=[row],
                            locks={
                                "sessions": [sid],
                                "chats": [chat],
                                "turns": [row["values"]["id"]],
                            },
                            session_id=sid,
                            chat_id=chat,
                            turn_id=row["values"]["id"],
                            commit_plan=CommitPlan(recovery_block="recovery"),
                        ),
                    )
        usage = state.get("context_usage")
        invalid = state.get("context_usage_invalid", False)
        updates = [
            {
                "table": "sessions",
                "values": {
                    "id": sid,
                    "state_revision": revision,
                    "history_high": history_high,
                    "context_epoch": state.get("context_epoch", 0),
                    "context_usage_text": (
                        _json(usage) if usage is not None else None
                    ),
                    "context_usage_invalid": int(bool(invalid)),
                    "recovery_publication_id": {"publication": True},
                    "recovery_offset": {"block": "recovery", "part": "offset"},
                    "recovery_length": {"block": "recovery", "part": "length"},
                },
            },
        ]
        for cid in chats.values():
            high = max(
                (
                    row.get("admission_order", index) + 1
                    for index, row in enumerate(
                        state.get("turn_states", {}).values(),
                    )
                    if chats.get(row.get("chat_id")) == cid
                ),
                default=0,
            )
            updates.append(
                {
                    "table": "chats",
                    "values": {"id": cid, "next_turn_order": high + 1},
                },
            )
        await self._publish(
            root_position,
            scope,
            [],
            [
                _block("recovery", "recovery", state),
                _block(
                    "legacy_root",
                    "legacy_root",
                    session["legacy"]["root"],
                ),
            ],
            spec=ImportBatch(
                updates=updates,
                locks={"sessions": [sid], "chats": list(chats.values())},
                session_id=sid,
            ),
        )

    async def _archive(self, chat, scope):
        if not chat["archive"]["epochs"]:
            return
        cid = self.chat_ids[chat["position"]]
        row = await self.db.fetch_one(
            "SELECT session_id FROM swe_conversation_chats WHERE id=%s",
            (cid,),
        )
        sid = row["session_id"]
        archive_order = 0
        batches = archive_batches(chat["archive"])
        for order, batch in enumerate(batches, start=1):
            boundary = batch["boundary"]
            rows = []
            blocks = [_block("archive", "archive", batch["messages"])]
            first = archive_order + 1
            for index, payload in enumerate(batch["messages"]):
                archive_order += 1
                mid = await self._assigned(
                    "message",
                    chat["position"]
                    + "/archive:"
                    + boundary["id"]
                    + "/"
                    + str(index),
                )
                name = "message:" + str(mid)
                item = _entry(payload, "archive", index, 0)
                values = _message_row(
                    mid,
                    sid,
                    item,
                    archive_order,
                    name,
                    None,
                    None,
                    service_unit_id=scope.service_unit_id,
                )
                values.update(
                    session_order=None,
                    archive_order=archive_order,
                    archive_chat_id=cid,
                    archive_publication_id={"publication": True},
                    deleted_at=(
                        self.manifest.data["source_boundary"]["captured_at"]
                        if chat["archive"]["deleted"]
                        else None
                    ),
                )
                rows.append({"table": "messages", "values": values})
                blocks.append(_block(name, "archive", payload))
            pub = await self._allocate(
                "publication",
                chat["position"] + "/boundary:" + boundary["id"],
            )
            values = {
                "id": pub["target_id"],
                "session_id": sid,
                "chat_id": cid,
                "boundary_key": boundary["id"],
                "boundary_order": order,
                "archive_first_order": first,
                "archive_last_order": archive_order,
                "archive_source_count": len(batch["messages"]),
                "archive_display_count": boundary["archived_message_count"],
                "archive_first_source_msg_key": boundary["first_message_id"],
                "archive_last_source_msg_key": boundary["last_message_id"],
                "archive_first_timestamp": _timestamp(
                    boundary["first_timestamp"],
                ),
                "archive_last_timestamp": _timestamp(
                    boundary["last_timestamp"],
                ),
                "archive_offset": {"block": "archive", "part": "offset"},
                "archive_length": {"block": "archive", "part": "length"},
                "retained_history": 1,
                "committed_at": boundary["created_at"],
            }
            await self._publish(
                chat["position"] + "/boundary:" + boundary["id"],
                scope,
                rows,
                blocks,
                spec=ImportBatch(
                    updates=[{"table": "publications", "values": values}],
                    locks={"sessions": [sid], "chats": [cid]},
                    session_id=sid,
                    chat_id=cid,
                    epoch=batch["epoch"],
                ),
            )
        epochs = chat["archive"]["epochs"]
        for item in reversed(epochs):
            position = chat["position"] + "/epoch:" + str(item["epoch"])
            pub = await self._allocate("publication", position)
            record = item["record"]
            events = [
                event
                for event in item["events"]
                if event["epoch"] == item["epoch"]
            ]
            values = {
                "id": pub["target_id"],
                "session_id": sid,
                "chat_id": cid,
                "epoch": item["epoch"],
                "checkpoint_revision": record["revision"],
                "checkpoint_base_revision": max(0, record["revision"] - 1),
                "checkpoint_event_watermark": record["applied_event_sequence"],
                "checkpoint_source_high": 0,
                "checkpoint_offset": {"block": "checkpoint", "part": "offset"},
                "checkpoint_length": {"block": "checkpoint", "part": "length"},
                "retained_history": 1,
            }
            blocks = [
                _block("checkpoint", "checkpoint", record),
                _block("legacy_epoch", "legacy_epoch", item),
            ]
            if events:
                values.update(
                    event_first=events[0]["sequence"],
                    event_last=events[-1]["sequence"],
                    event_offset={"block": "events", "part": "offset"},
                    event_length={"block": "events", "part": "length"},
                )
                blocks.append(_block("events", "events", events))
            updates = [{"table": "publications", "values": values}]
            if item is epochs[0]:
                updates.append(
                    {
                        "table": "chats",
                        "values": {
                            "id": cid,
                            "epoch": item["epoch"],
                            "event_high": max(
                                (
                                    event["sequence"]
                                    for event in events
                                    if event["epoch"] == item["epoch"]
                                ),
                                default=0,
                            ),
                            "checkpoint_revision": record["revision"],
                            "active_checkpoint_publication_id": {
                                "publication": True,
                            },
                            "history_high": archive_order,
                            "next_boundary_order": len(batches) + 1,
                        },
                    },
                )
            await self._publish(
                position,
                scope,
                [],
                blocks,
                spec=ImportBatch(
                    updates=updates,
                    locks={"sessions": [sid], "chats": [cid]},
                    session_id=sid,
                    chat_id=cid,
                    epoch=item["epoch"],
                ),
            )

    async def _shares(self, report):
        from ..identity import WorkspaceScope
        from ..shares import ShareInstall
        from ..commit_plan import CommitPlan

        for share in report["shares"]:
            record = dict(share["record"])
            cid = self.chat_ids.get(share["chat_position"])
            sid = None
            if cid is not None:
                chat = await self.db.fetch_one(
                    "SELECT session_id,service_unit_key,tenant_key,source_key,agent_key FROM swe_conversation_chats WHERE id=%s",
                    (cid,),
                )
                sid = chat["session_id"]
                scope = WorkspaceScope.from_row(chat)
                if scope.service_unit_id != share["service_unit_id"]:
                    raise ValueError("share service differs from Chat")
            else:
                scope = WorkspaceScope(
                    record["tenant_id"],
                    "",
                    "",
                    service_unit_id=share["service_unit_id"],
                )
            record.pop("service_unit_id")
            plan = CommitPlan(share=ShareInstall(**record))
            await self._publish(
                share["position"],
                scope,
                [],
                [_block("share", "share", share["snapshot"])],
                spec=ImportBatch(
                    scope_kind=share["scope_kind"],
                    session_id=sid,
                    chat_id=cid,
                    commit_plan=plan,
                    kind="share_import",
                    locks={
                        "sessions": [sid] if sid else [],
                        "chats": [cid] if cid else [],
                    },
                ),
            )


def _json(value):
    import json

    return json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        allow_nan=False,
    )


def _block(name, kind, value):
    from ..bundle import BundleBlock, MAX_READ_BYTES

    raw = _json(value).encode()
    if len(raw) > MAX_READ_BYTES:
        raise ValueError(
            "legacy object exceeds supported migration reader limit; projection required",
        )
    return BundleBlock(name, kind, raw)


def _timestamp(value):
    from datetime import datetime, timezone

    if value is None:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise ValueError("legacy message timestamp is invalid") from None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.isoformat()


def _message_row(
    identity,
    session,
    item,
    order,
    block,
    turn,
    origin,
    *,
    service_unit_id,
):
    projection = item.get(
        "history_projection",
        item["payload"].get("metadata", {}),
    )
    task_run_id = (
        projection.get("task_run_id") if isinstance(projection, dict) else None
    )
    return {
        "id": identity,
        "service_unit_key": service_unit_id,
        "session_id": session,
        "origin_chat_id": origin,
        "archive_chat_id": None,
        "layer": item["layer"],
        "session_order": order,
        "archive_order": None,
        "turn_id": turn,
        "source_msg_key": item["source_msg_key"],
        "source_occurrence_no": item["source_occurrence_no"],
        "role_key": item["role"],
        "kind_key": item["payload"].get("type") or "message",
        "task_run_key": task_run_id,
        "publication_id": {"publication": True},
        "byte_offset": {"block": block, "part": "offset"},
        "byte_length": {"block": block, "part": "length"},
        "context_epoch": item["context_epoch"],
        "in_context": int(item["in_context"]),
        "current_visible": 1,
        "message_timestamp": _timestamp(item["payload"].get("timestamp")),
    }


def bundle_stores(bundles, manifest):
    """Bind every source service explicitly to one isolated NAS store."""
    services = {
        item["service_unit_id"]
        for item in manifest.data.get("services", manifest.data["workspaces"])
    }
    if isinstance(bundles, dict):
        stores = dict(bundles)
    elif len(services) == 1:
        stores = {next(iter(services)): bundles}
    else:
        raise ValueError(
            "multi-service migration requires a NAS store mapping",
        )
    if set(stores) != services or not stores:
        raise ValueError(
            "NAS store mapping must cover exactly the migration services",
        )
    roots = [store.root.resolve() for store in stores.values()]
    if any(
        a == b or a.is_relative_to(b) or b.is_relative_to(a)
        for i, a in enumerate(roots)
        for b in roots[i + 1 :]
    ):
        raise ValueError("service NAS roots must be isolated")
    return stores
