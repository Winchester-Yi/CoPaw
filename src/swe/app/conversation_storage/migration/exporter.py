# -*- coding: utf-8 -*-
"""Export a verified consistent backup to the pinned old reader's native formats."""

from copy import deepcopy
from dataclasses import dataclass
from datetime import timezone
import json
from pathlib import Path
import re

from ..backup import _atomic_json, _new_directory, load_backup
from ..bundle import BundleStore
from ..chat_mutations import load_metadata_json
from ..errors import IntegrityError
from .legacy import read_archive, read_session

LEDGER_FIELDS = frozenset(
    {
        "session_generation",
        "turn_ledger_version",
        "ledger_nonce",
        "next_admission_order",
        "cold_turn_count",
        "ledger_latest",
    },
)


@dataclass(frozen=True)
class OldFormatTarget:
    image_digest: str
    session_version: int
    checkpoint_version: int

    def __post_init__(self):
        if not re.fullmatch(r".+@sha256:[a-f0-9]{64}", self.image_digest):
            raise ValueError("export requires a pinned old image digest")
        if self.session_version not in (
            1,
            2,
            3,
        ) or self.checkpoint_version not in (1, 2):
            raise ValueError(
                "old image capabilities must be explicit supported formats",
            )


def export_session_state(state, *, target_version):
    state = deepcopy(state)
    if target_version not in (1, 2, 3):
        raise ValueError("unsupported old Session format")
    if target_version < 3 and any(key in state for key in LEDGER_FIELDS):
        raise ValueError("old target cannot express retained ledger metadata")
    state["schema_version"] = target_version
    return state


def _text(value):
    return value.decode() if isinstance(value, bytes) else value


def _stamp(value):
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat()


class ConversationExporter:
    def __init__(self, backup_path, target):
        if not isinstance(target, OldFormatTarget):
            raise TypeError("export requires an explicit old image target")
        self.root = Path(backup_path)
        self.target = target
        self.document = load_backup(backup_path)
        self.snapshot = self.document["snapshot"]
        self.bundles = BundleStore(self.root / "nas")
        self.publications = {
            row["id"]: row for row in self.snapshot["publications"]
        }
        self.sessions = {row["id"]: row for row in self.snapshot["sessions"]}
        self.artifacts = {}

    def payload(self, publication_id, offset, length):
        publication = self.publications[publication_id]
        return load_metadata_json(
            b"".join(
                self.bundles.iter_extent(
                    _text(publication["relative_path"]),
                    offset,
                    length,
                    expected_length=publication["byte_length"],
                ),
            ),
        )

    def block(self, publication_id, name):
        publication = self.publications[publication_id]
        index = self.bundles.inspect(_text(publication["relative_path"]))
        extent = index.blocks.get(name)
        if extent is None:
            raise IntegrityError(
                "export requires complete retained recovery metadata",
            )
        return self.payload(publication_id, extent.offset, extent.length)

    def export(self, destination, *, image_verifier=None):
        destination = _new_directory(destination)
        if any(
            row["active_turn_id"] is not None for row in self.sessions.values()
        ) or any(
            row["state"]
            in ("reserved", "admitted", "running", "stopping", "settling")
            for row in self.snapshot["turns"]
        ):
            raise ValueError(
                "old export requires stopped writers and settled original Turns",
            )
        self._source_artifacts(destination)
        expected = self._sessions(destination)
        self._chats(destination)
        self._shares(destination)
        self._archives(destination)
        report = self._verify(destination, expected)
        report.update(
            image_digest=self.target.image_digest,
            backup_id=self.document["backup_id"],
        )
        report.update(image_verified=False, rollback_ready=False)
        if image_verifier is not None:
            evidence = image_verifier(self.target.image_digest, destination)
            if (
                not isinstance(evidence, dict)
                or not evidence.get("passed")
                or evidence.get("image_digest") != self.target.image_digest
                or not evidence.get("read_and_continue_write")
            ):
                raise ValueError("pinned old image verification did not pass")
            report.update(
                image_verified=True,
                rollback_ready=True,
                image_evidence=evidence,
            )
        _atomic_json(destination / "export-report.json", report)
        return report

    def _source_artifacts(self, destination):
        scope = [value.decode() for value in self.document["scope"]]
        discarded = {
            _text(chat["external_chat_key"])
            for chat in self.snapshot["chats"]
            if self._deleted_archive_reclaimed(chat)
        }
        for publication in self.snapshot["publications"]:
            if (
                publication["state"] != "committed"
                or publication["gc_state"] != "live"
            ):
                continue
            index = self.bundles.inspect(_text(publication["relative_path"]))
            if "legacy_artifact_metadata" not in index.blocks:
                continue
            metadata = self.block(
                publication["id"],
                "legacy_artifact_metadata",
            )
            relative = Path(metadata["relative_path"])
            if (
                metadata.get("version") != 1
                or metadata.get("scope") != scope
                or relative.is_absolute()
                or ".." in relative.parts
            ):
                raise IntegrityError(
                    "retained legacy artifact identity or path is invalid",
                )
            extent = index.blocks.get("legacy_artifact")
            if (
                extent is None
                or extent.kind != "legacy_artifact"
                or extent.length != metadata["bytes"]
            ):
                raise IntegrityError(
                    "retained legacy artifact extent is invalid",
                )
            if (
                len(relative.parts) > 1
                and relative.parts[0] == "dialog"
                and relative.parts[1] in discarded
            ):
                continue
            target = destination / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            if target.exists():
                raise IntegrityError(
                    "retained legacy artifact path is duplicated",
                )
            with target.open("xb") as output:
                for chunk in self.bundles.iter_extent(
                    index.relative_path,
                    extent.offset,
                    extent.length,
                    expected_length=index.byte_length,
                ):
                    output.write(chunk)
            self.artifacts[str(relative)] = metadata
        session_dir = destination / "sessions"
        for path in session_dir.glob("*.json"):
            self.artifacts["state:" + path.name] = read_session(path)["state"]

    def recovery(self, publication_id, value):
        from ..session_projection import hydrate_state, is_projection

        if not is_projection(value):
            return value
        current = self.publications[publication_id]

        def resolve(ref):
            target_id = ref.get("publication_id", publication_id)
            target = self.publications.get(target_id)
            if target is None or target["session_id"] != current["session_id"]:
                raise IntegrityError(
                    "export projection dependency Session differs",
                )
            if target_id != publication_id and target_id >= publication_id:
                raise IntegrityError(
                    "export projection dependency order is invalid",
                )
            if "message_id" in ref:
                row = next(
                    (
                        item
                        for item in self.snapshot["messages"]
                        if item["id"] == ref["message_id"]
                    ),
                    None,
                )
                if (
                    row is None
                    or row["publication_id"] != target_id
                    or row["byte_offset"] != ref["offset"]
                    or row["byte_length"] != ref["length"]
                ):
                    raise IntegrityError(
                        "export projection occurrence locator differs",
                    )
                return self.payload(target_id, ref["offset"], ref["length"])
            if ref.get("operation_key") != _text(target["operation_key"]):
                raise IntegrityError(
                    "export projection operation identity differs",
                )
            return self.block(target_id, ref["block_name"])

        return hydrate_state(value, resolve)

    def _sessions(self, destination):
        from ....app.runner.session import sanitize_filename

        expected, paths = {}, set()
        for row in self.sessions.values():
            if row["recovery_publication_id"] is None:
                state = {}
            else:
                state = self.payload(
                    row["recovery_publication_id"],
                    row["recovery_offset"],
                    row["recovery_length"],
                )
                state = self.recovery(row["recovery_publication_id"], state)
            if not isinstance(state, dict):
                raise IntegrityError(
                    "export Session recovery body is not an object",
                )
            turns = state.setdefault("turn_states", {})
            for turn in self.snapshot["turns"]:
                if (
                    turn["session_id"] != row["id"]
                    or turn["state"] == "rejected"
                ):
                    continue
                publication = (
                    turn["terminal_publication_id"]
                    or turn["admission_publication_id"]
                )
                if publication is None:
                    raise ValueError(
                        "old export cannot express a Turn without full recovery state",
                    )
                saved = self.recovery(
                    publication,
                    self.block(publication, "recovery"),
                ).get(
                    "turn_states",
                    {},
                )
                key = _text(turn["question_msg_key"])
                value = saved.get(key) or saved.get(
                    _text(turn["runtime_turn_key"]),
                )
                if not isinstance(value, dict):
                    raise IntegrityError(
                        "export Turn root lacks complete state",
                    )
                turns[key] = value
            sid, user = _text(row["external_session_key"]), _text(
                row["user_key"],
            )
            name = (
                (sanitize_filename(user) + "_" if user else "")
                + sanitize_filename(sid)
                + ".json"
            )
            if name in paths:
                raise ValueError(
                    "distinct Session identities collide in the pinned old filename format",
                )
            paths.add(name)
            if self.target.session_version == 3 and any(
                key in state for key in LEDGER_FIELDS
            ):
                self._complete_v3_admission_order(state, row["id"])
            value = export_session_state(
                state,
                target_version=self.target.session_version,
            )
            if self.target.session_version == 3:
                from ....app.runner.turn_ledger import TurnLedger

                path = destination / "sessions" / name
                if any(key in value for key in LEDGER_FIELDS):
                    if str(Path("sessions") / name) not in self.artifacts:
                        raise ValueError(
                            "full original ledger generation requires retained export source artifacts",
                        )
                    original = self.artifacts["state:" + name]
                    if original.get("session_generation") != value.get(
                        "session_generation",
                    ):
                        raise IntegrityError(
                            "retained ledger generation differs from current recovery root",
                        )
                    if value.get("revision", 0) < original.get("revision", 0):
                        raise IntegrityError(
                            "current recovery predates retained ledger root",
                        )
                    value["schema_version"] = 3
                else:
                    value["schema_version"] = 2
                    _atomic_json(path, value)
                    value = TurnLedger(path).initialize(value)
            _atomic_json(destination / "sessions" / name, value)
            expected[name] = state
        return expected

    def _complete_v3_admission_order(self, state, session_id):
        turns = state["turn_states"]
        next_order = max(
            [
                state.get("next_admission_order", 0),
                *(
                    turn["admission_order"] + 1
                    for turn in turns.values()
                    if "admission_order" in turn
                ),
            ],
        )
        ordered = [
            _text(row["question_msg_key"])
            for row in sorted(
                self.snapshot["turns"],
                key=lambda row: row["id"],
            )
            if row["session_id"] == session_id
        ]
        for key in dict.fromkeys([*ordered, *turns]):
            if key in turns and "admission_order" not in turns[key]:
                turns[key]["admission_order"] = next_order
                next_order += 1
        state["next_admission_order"] = next_order

    def _chats(self, destination):
        from ....app.runner.models import ChatSpec, ChatsFile

        chats = []
        for row in sorted(
            self.snapshot["chats"],
            key=lambda item: (item["registry_order"], item["id"]),
        ):
            if row["deleted_at"] is not None:
                continue
            session = self.sessions[row["session_id"]]
            metadata = load_metadata_json(row["metadata_text"])
            if row["content_meta_publication_id"] is not None:
                metadata = {
                    **self.payload(
                        row["content_meta_publication_id"],
                        row["content_meta_offset"],
                        row["content_meta_length"],
                    ),
                    **metadata,
                }
            if row["expert_binding_revision"] or row["expert_id"] is not None:
                metadata["expert_main_binding"] = {
                    "revision": row["expert_binding_revision"],
                    "expert_id": _text(row["expert_id"]),
                }
            chats.append(
                ChatSpec(
                    id=_text(row["external_chat_key"]),
                    session_id=_text(session["external_session_key"]),
                    user_id=_text(row["user_key"]),
                    channel=_text(row["channel_key"]),
                    name=row["title"],
                    meta=metadata,
                    status="idle",
                    created_at=_stamp(row["created_at"]),
                    updated_at=_stamp(row["updated_at"]),
                ),
            )
        _atomic_json(
            destination / "chats.json",
            ChatsFile(chats=chats).model_dump(mode="json"),
        )

    def _shares(self, destination):
        rows = []
        for source in self.snapshot["shares"]:
            row = {
                key: (_stamp(value) if key.endswith("_at") else value)
                for key, value in source.items()
                if key not in ("publication_id", "byte_offset", "byte_length")
            }
            key = Path(row["snapshot_key"])
            if key.is_absolute() or ".." in key.parts:
                raise ValueError(
                    "old share snapshot key escapes trusted export root",
                )
            payload = self.payload(
                source["publication_id"],
                source["byte_offset"],
                source["byte_length"],
            )
            _atomic_json(destination / key, payload)
            rows.append(row)
        _atomic_json(destination / "shares.json", rows)

    def _archives(self, destination):
        for chat in self.snapshot["chats"]:
            self._archive(destination, chat)

    def _archive_messages(self, publication):
        raw = self.payload(
            publication["id"],
            publication["archive_offset"],
            publication["archive_length"],
        )
        occurrences = sorted(
            (
                row
                for row in self.snapshot["messages"]
                if row["archive_publication_id"] == publication["id"]
            ),
            key=lambda row: (row["archive_order"], row["id"]),
        )
        messages = [
            self.payload(
                row["publication_id"],
                row["byte_offset"],
                row["byte_length"],
            )
            for row in occurrences
        ]
        if len(messages) != publication["archive_source_count"]:
            raise IntegrityError("archive export occurrence set is incomplete")
        if isinstance(raw, list) and raw != messages:
            raise IntegrityError(
                "archive export boundary body differs from occurrences",
            )
        if not isinstance(raw, (list, dict)):
            raise IntegrityError("archive export boundary is invalid")
        return messages

    def _deleted_archive_reclaimed(self, chat):
        if chat["deleted_at"] is None:
            return False
        references = {
            chat[key]
            for key in ("active_checkpoint_publication_id",)
            if chat.get(key) is not None
        }
        references.update(
            row["id"]
            for row in self.snapshot["publications"]
            if row["chat_id"] == chat["id"]
            and any(
                row.get(prefix + "_offset") is not None
                for prefix in ("archive", "checkpoint", "event")
            )
        )
        references.update(
            row[key]
            for row in self.snapshot["messages"]
            if row.get("archive_chat_id") == chat["id"]
            for key in ("publication_id", "archive_publication_id")
            if row.get(key) is not None
        )
        return any(
            self.publications[identity]["gc_state"]
            in ("deleting", "reclaimed")
            for identity in references
        )

    def _archive(self, destination, chat):
        from ....agents.memory.chat_checkpoint import CheckpointRecord
        from ....agents.memory.conversation_archive import (
            ConversationArchiveBoundary,
            ConversationArchiveStore,
        )

        chat_id = _text(chat["external_chat_key"])
        ConversationArchiveStore._validate_chat_id(chat_id)
        if self._deleted_archive_reclaimed(chat):
            marker = destination / "dialog" / ".deleted" / chat_id
            marker.parent.mkdir(parents=True, exist_ok=True)
            marker.touch()
            return
        publications = [
            row
            for row in self.snapshot["publications"]
            if row["chat_id"] == chat["id"]
            and row["state"] == "committed"
            and row["gc_state"] == "live"
        ]
        epochs = sorted(
            {row["epoch"] for row in publications if row["epoch"] is not None},
        )
        if any(epoch < 1 for epoch in epochs):
            raise ValueError(
                "pinned old checkpoint format cannot express epoch zero",
            )
        root = destination / "dialog" / chat_id
        if (
            self.target.checkpoint_version == 1
            and (root / "state-v2.json").exists()
        ):
            raise ValueError(
                "old target cannot express retained journal history",
            )
        manifest, evidence, events = [], {}, []
        for row in sorted(
            publications,
            key=lambda item: (item["boundary_order"] or 0, item["id"]),
        ):
            if row["archive_offset"] is not None:
                messages = self._archive_messages(row)
                boundary = ConversationArchiveBoundary(
                    _text(row["boundary_key"]),
                    chat_id,
                    _stamp(row["committed_at"]),
                    row["archive_display_count"],
                    _text(row["archive_first_source_msg_key"]),
                    _text(row["archive_last_source_msg_key"]),
                    _stamp(row["archive_first_timestamp"]),
                    _stamp(row["archive_last_timestamp"]),
                )
                ConversationArchiveBoundary.from_dict(boundary.to_dict())
                manifest.append(boundary.to_dict())
                root.mkdir(parents=True, exist_ok=True)
                (root / (boundary.id + ".jsonl")).write_text(
                    "".join(
                        json.dumps(value, ensure_ascii=False) + "\n"
                        for value in messages
                    ),
                )
                for index, message in enumerate(messages):
                    evidence[
                        ConversationArchiveStore._evidence_epoch_key(
                            boundary.id,
                            index,
                            message["id"],
                        )
                    ] = row["epoch"]
            if row["event_offset"] is not None:
                events.extend(
                    self.payload(
                        row["id"],
                        row["event_offset"],
                        row["event_length"],
                    ),
                )
        if publications:
            root.mkdir(parents=True, exist_ok=True)
            epoch = chat["epoch"]
            checkpoints = [
                row
                for row in publications
                if row["checkpoint_offset"] is not None
                and row["epoch"] == epoch
            ]
            checkpoint = max(
                checkpoints,
                key=lambda row: (row["checkpoint_revision"], row["id"]),
                default=None,
            )
            record = (
                self.payload(
                    checkpoint["id"],
                    checkpoint["checkpoint_offset"],
                    checkpoint["checkpoint_length"],
                )
                if checkpoint
                else CheckpointRecord.new(
                    chat_id=chat_id,
                    epoch=epoch,
                ).to_dict()
            )
            _atomic_json(
                root / "manifest.json",
                {"version": 1, "chat_id": chat_id, "boundaries": manifest},
            )
            _atomic_json(root / "checkpoint.json", record)
            _atomic_json(
                root / "epochs.json",
                {
                    "current_epoch": epoch,
                    "history": [
                        {
                            "epoch": item,
                            "reason": "restored",
                            "started_at": _stamp(chat["created_at"]),
                        }
                        for item in epochs or [epoch]
                    ],
                },
            )
            _atomic_json(root / "evidence-epochs.json", evidence)
            (root / "events.jsonl").write_text(
                "".join(
                    json.dumps(item, ensure_ascii=False) + "\n"
                    for item in sorted(
                        events,
                        key=lambda value: (value["epoch"], value["sequence"]),
                    )
                ),
            )
            if self.target.checkpoint_version == 2:
                store = ConversationArchiveStore(
                    destination / "dialog",
                    cursor_secret=b"offline-rollback-verification",
                )
                if (root / "state-v2.json").exists():
                    self._advance_retained_journal(
                        root,
                        record,
                        manifest,
                        evidence,
                        events,
                        publications,
                    )
                else:
                    store.migrate_checkpoint_v2(chat_id, dry_run=False)
        if chat["deleted_at"] is not None:
            marker = destination / "dialog" / ".deleted" / chat_id
            marker.parent.mkdir(parents=True, exist_ok=True)
            marker.touch()

    def _advance_retained_journal(
        self,
        root,
        record,
        manifest,
        evidence,
        events,
        publications,
    ):
        from ....agents.memory.chat_checkpoint import (
            CheckpointEvent,
            CheckpointRecord,
        )
        from ....agents.memory.checkpoint_storage_v2 import CheckpointStorageV2

        storage = CheckpointStorageV2(root)
        head = storage.head()
        target_epoch = record["epoch"]
        if head["epoch"] > target_epoch:
            raise IntegrityError(
                "retained journal is newer than export snapshot",
            )
        boundary_epochs = {
            _text(row["boundary_key"]): row["epoch"]
            for row in publications
            if row["archive_offset"] is not None
        }
        for epoch in range(head["epoch"], target_epoch + 1):
            if storage.head()["epoch"] < epoch:
                storage.reset(
                    CheckpointRecord.new(
                        chat_id=record["chat_id"],
                        epoch=epoch,
                    ),
                    "restored",
                )
            current = storage.head()
            existing = [event.to_dict() for event in storage.events(current)]
            expected = sorted(
                (event for event in events if event["epoch"] == epoch),
                key=lambda event: event["sequence"],
            )
            if expected[: len(existing)] != existing:
                raise IntegrityError(
                    "retained journal differs from captured events",
                )
            for event in expected[len(existing) :]:
                storage.append(
                    CheckpointEvent.from_dict(event),
                    allow_oversized=True,
                )
            checkpoints = [
                row
                for row in publications
                if row["checkpoint_offset"] is not None
                and row["epoch"] == epoch
            ]
            latest = max(
                checkpoints,
                key=lambda row: (row["checkpoint_revision"], row["id"]),
                default=None,
            )
            checkpoint = (
                self.payload(
                    latest["id"],
                    latest["checkpoint_offset"],
                    latest["checkpoint_length"],
                )
                if latest
                else CheckpointRecord.new(
                    chat_id=record["chat_id"],
                    epoch=epoch,
                ).to_dict()
            )
            storage.publish(
                record=CheckpointRecord.from_dict(checkpoint),
                manifest={
                    "version": 1,
                    "chat_id": record["chat_id"],
                    "boundaries": [
                        item
                        for item in manifest
                        if boundary_epochs[item["id"]] <= epoch
                    ],
                },
                evidence={
                    key: value
                    for key, value in evidence.items()
                    if value <= epoch
                },
            )

    def _verify(self, destination, expected):
        for name, state in expected.items():
            actual = read_session(destination / "sessions" / name)["state"]
            comparison = deepcopy(state)
            comparison["schema_version"] = self.target.session_version
            if self.target.session_version < 3 and actual != comparison:
                raise IntegrityError(
                    "fixed old Session reader roundtrip differs",
                )
            if self.target.session_version == 3:
                for key, value in state.items():
                    if key == "turn_states":
                        expected_turns = {
                            name: {
                                field: item
                                for field, item in turn.items()
                                if field != "admission_order"
                            }
                            for name, turn in value.items()
                        }
                        actual_turns = {
                            name: {
                                field: item
                                for field, item in turn.items()
                                if field != "admission_order"
                            }
                            for name, turn in actual.get(key, {}).items()
                        }
                        if expected_turns != actual_turns:
                            raise IntegrityError(
                                "fixed old ledger reader changed complete Turn states",
                            )
                    elif key != "schema_version" and actual.get(key) != value:
                        raise IntegrityError(
                            "fixed old ledger reader lost recovery modules",
                        )
        for chat in self.snapshot["chats"]:
            self._verify_archive(destination, chat)
        return {
            "passed": True,
            "sessions": len(expected),
            "chats": len(self.snapshot["chats"]),
            "shares": len(self.snapshot["shares"]),
        }

    def _verify_archive(self, destination, chat):
        actual = read_archive(
            destination / "dialog",
            _text(chat["external_chat_key"]),
        )
        if actual["deleted"] != (chat["deleted_at"] is not None):
            raise IntegrityError("fixed old archive deleted marker differs")
        if self._deleted_archive_reclaimed(chat):
            if actual["batches"]:
                raise IntegrityError(
                    "fixed old reclaimed archive was resurrected",
                )
            return
        publications = [
            row
            for row in self.snapshot["publications"]
            if row["chat_id"] == chat["id"]
            and row["state"] == "committed"
            and row["gc_state"] == "live"
        ]
        expected = {
            _text(row["boundary_key"]): row
            for row in publications
            if row["archive_offset"] is not None
        }
        batches = {
            batch["boundary"]["id"]: batch for batch in actual["batches"]
        }
        if set(batches) != set(expected):
            raise IntegrityError("fixed old archive occurrence set differs")
        for key, publication in expected.items():
            if batches[key]["messages"] != self._archive_messages(publication):
                raise IntegrityError(
                    "fixed old archive occurrence body differs",
                )
