# -*- coding: utf-8 -*-
"""Durable external migration journal; source identities never use digests."""

from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
import json
import os
import re
import tempfile
from uuid import UUID, uuid4

import fcntl

from ..bundle import BundleStore, new_operation_key
from ..chat_mutations import load_metadata_json
from ..identity import WorkspaceScope, service_unit_bytes

KINDS = frozenset(
    {
        "session",
        "chat",
        "turn",
        "message",
        "publication",
        "retention",
        "share",
    },
)
TRANSITIONS = {
    "planned": {"importing", "blocked"},
    "importing": {"committed", "blocked"},
    "blocked": {"importing"},
    "committed": {"verified"},
    "verified": set(),
}


@dataclass(frozen=True)
class SourceBoundary:
    snapshot_id: str
    captured_at: str
    old_image_digest: str
    evidence_refs: tuple[str, ...]

    def __post_init__(self):
        if not isinstance(self.snapshot_id, str) or not self.snapshot_id:
            raise ValueError("source snapshot identity is required")
        timestamp = datetime.fromisoformat(
            self.captured_at.replace("Z", "+00:00"),
        )
        if timestamp.tzinfo is None:
            raise ValueError("source boundary timestamp requires timezone")
        if not re.fullmatch(r".+@sha256:[a-f0-9]{64}", self.old_image_digest):
            raise ValueError(
                "old image must be pinned by immutable image digest",
            )
        if (
            not isinstance(self.evidence_refs, (tuple, list))
            or not self.evidence_refs
            or not all(
                isinstance(ref, str) and ref for ref in self.evidence_refs
            )
        ):
            raise ValueError(
                "source writer-stop and read-only evidence references are required",
            )


class MigrationManifest:
    def __init__(self, path, data):
        self.path = Path(path)
        self.data = data
        self._validate()

    @property
    def migration_id(self):
        return self.data["migration_id"]

    def _validate(self):
        if self.data.get("version") != 2:
            raise ValueError("unsupported migration manifest version")
        if str(UUID(self.data["migration_id"])) != self.data["migration_id"]:
            raise ValueError("invalid migration identity")
        SourceBoundary(**self.data["source_boundary"])
        if self.data.get("target") is not None:
            self._validate_target(self.data["target"])
        for item in self.data["workspaces"]:
            if "service_unit_id" not in item:
                raise ValueError(
                    "explicit workspace service_unit_id is required",
                )
            WorkspaceScope(
                item["tenant_id"],
                item["source_id"],
                item["agent_id"],
                service_unit_id=item["service_unit_id"],
            )
        ids = {}
        operations = set()
        for kind, assignments in self.data["assignments"].items():
            if kind not in KINDS:
                raise ValueError("unknown assignment kind")
            ids[kind] = set()
            for source, assignment in assignments.items():
                self._validate_assignment(source, assignment, ids[kind])
                if assignment["operation_key"] in operations:
                    raise ValueError("duplicate migration operation identity")
                operations.add(assignment["operation_key"])
        if any(
            state not in TRANSITIONS for state in self.data["batches"].values()
        ):
            raise ValueError("invalid batch state")

    @staticmethod
    def _validate_assignment(source, assignment, identities):
        if not isinstance(source, str) or not source:
            raise ValueError("source position is required")
        target = assignment["target_id"]
        if type(target) is not int or target <= 0 or target in identities:
            raise ValueError("invalid or duplicate target identity")
        identities.add(target)
        expected = BundleStore.path_for(assignment["operation_key"])
        if assignment["relative_path"] != expected:
            raise ValueError("manifest bundle path does not match operation")

    @classmethod
    def create(cls, path, boundary, workspaces):
        data = {
            "version": 2,
            "migration_id": str(uuid4()),
            "source_boundary": {
                **asdict(boundary),
                "evidence_refs": list(boundary.evidence_refs),
            },
            "workspaces": workspaces,
            "assignments": {},
            "batches": {},
        }
        manifest = cls(path, data)
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        with manifest._lock():
            if path.exists():
                raise FileExistsError(path)
            manifest._persist()
        return manifest

    @classmethod
    def load(cls, path):
        return cls(
            path,
            load_metadata_json(Path(path).read_text(encoding="utf-8")),
        )

    def _lock(self):
        return _ManifestLock(self.path.with_suffix(self.path.suffix + ".lock"))

    def _refresh(self):
        current = self.load(self.path)
        if (
            current.migration_id != self.migration_id
            or current.data["source_boundary"] != self.data["source_boundary"]
        ):
            raise ValueError("migration identity or source boundary changed")
        if current.data["workspaces"] != self.data["workspaces"]:
            raise ValueError("migration workspace mapping changed")
        for kind, assignments in self.data["assignments"].items():
            persisted = current.data["assignments"].get(kind, {})
            if any(
                persisted.get(source) != value
                for source, value in assignments.items()
            ):
                raise ValueError(
                    "known migration assignment changed or removed",
                )
        if (
            self.data.get("target") is not None
            and current.data.get("target") != self.data["target"]
        ):
            raise ValueError("migration target changed")
        self.data = current.data

    def _persist(self):
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(
                "w",
                dir=self.path.parent,
                delete=False,
                encoding="utf-8",
            ) as handle:
                temporary = Path(handle.name)
                json.dump(
                    self.data,
                    handle,
                    ensure_ascii=False,
                    sort_keys=True,
                    allow_nan=False,
                )
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.path)
            descriptor = os.open(
                self.path.parent,
                os.O_RDONLY | os.O_DIRECTORY,
            )
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    def allocate(self, kind, source_position):
        if (
            kind not in KINDS
            or not isinstance(source_position, str)
            or not source_position
        ):
            raise ValueError("valid kind and source position required")
        with self._lock():
            self._refresh()
            assignments = self.data["assignments"].setdefault(kind, {})
            if source_position not in assignments:
                operation = new_operation_key()
                assignments[source_position] = {
                    "target_id": max(
                        (value["target_id"] for value in assignments.values()),
                        default=0,
                    )
                    + 1,
                    "operation_key": operation,
                    "relative_path": BundleStore.path_for(operation),
                }
                self._persist()
            return dict(assignments[source_position])

    def bind_target(self, target):
        self._validate_target(target)
        with self._lock():
            self._refresh()
            if (
                self.data.get("target") is not None
                and self.data["target"] != target
            ):
                raise ValueError("migration target changed")
            self.data["target"] = dict(target)
            self._persist()

    def _validate_target(self, target):
        if (
            not isinstance(target, dict)
            or set(target) != {"schema", "nas_roots", "migration_id"}
            or target["migration_id"] != self.migration_id
        ):
            raise ValueError("invalid migration target binding")
        if not re.fullmatch(
            r"(?:test_conversation_[a-zA-Z0-9_]+|conversation_staging_[a-zA-Z0-9_]+)",
            target["schema"],
        ):
            raise ValueError("invalid migration staging schema binding")
        roots = target["nas_roots"]
        services = {
            item["service_unit_id"] for item in self.data["workspaces"]
        }
        if not isinstance(roots, dict) or set(roots) != services or not roots:
            raise ValueError("explicit NAS roots must match every service")
        resolved = []
        for service, value in roots.items():
            service_unit_bytes(service)
            root = Path(value)
            if (
                not root.is_absolute()
                or ".." in root.parts
                or str(root.resolve()) != value
            ):
                raise ValueError("invalid migration NAS root binding")
            resolved.append(root)
        if any(
            a == b or a.is_relative_to(b) or b.is_relative_to(a)
            for i, a in enumerate(resolved)
            for b in resolved[i + 1 :]
        ):
            raise ValueError("service NAS roots must be isolated")

    def set_batch_state(self, source_position, state):
        with self._lock():
            self._refresh()
            previous = self.data["batches"].get(source_position, "planned")
            if state != previous and state not in TRANSITIONS[previous]:
                raise ValueError("invalid migration batch transition")
            self.data["batches"][source_position] = state
            self._persist()


class _ManifestLock:
    def __init__(self, path):
        self.path = path
        self.handle = None

    def __enter__(self):
        self.handle = self.path.open("a+b")
        fcntl.flock(self.handle.fileno(), fcntl.LOCK_EX)
        return self

    def __exit__(self, *_):
        try:
            fcntl.flock(self.handle.fileno(), fcntl.LOCK_UN)
        finally:
            self.handle.close()
