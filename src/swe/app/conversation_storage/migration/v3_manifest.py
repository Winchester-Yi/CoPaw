# -*- coding: utf-8 -*-
"""Frozen discovery manifest; SQL, never this file, assigns import identities."""

from pathlib import Path
import re
from uuid import UUID

from ..identity import WorkspaceScope, service_unit_bytes
from .manifest import MigrationManifest, SourceBoundary, KINDS, TRANSITIONS


class V3Manifest(MigrationManifest):
    """Retain the durable manifest API without a local integer allocator."""

    def _validate(self):
        if self.data.get("version") != 3:
            raise ValueError("unsupported direct migration manifest version")
        if str(UUID(self.migration_id)) != self.migration_id:
            raise ValueError("invalid migration identity")
        SourceBoundary(**self.data["source_boundary"])
        services = self.data["services"]
        names = [item["service_unit_id"] for item in services]
        if not names or len(set(names)) != len(names):
            raise ValueError("unique explicit migration services are required")
        for name in names:
            service_unit_bytes(name)
        for item in self.data["workspaces"]:
            if item["service_unit_id"] not in names:
                raise ValueError("workspace service is not declared")
            WorkspaceScope(
                item["tenant_id"],
                item["source_id"],
                item["agent_id"],
                service_unit_id=item["service_unit_id"],
            )
        for kind, assignments in self.data["assignments"].items():
            if kind not in KINDS:
                raise ValueError("unknown assignment kind")
            identities = set()
            for source, assignment in assignments.items():
                self._validate_assignment(source, assignment, identities)
        if any(
            value not in TRANSITIONS for value in self.data["batches"].values()
        ):
            raise ValueError("invalid batch state")
        if self.data.get("target") is not None:
            self._validate_target(self.data["target"])

    def _validate_target(self, target):
        expected = {"schema", "nas_roots", "migration_id"}
        if (
            set(target) != expected
            or target["migration_id"] != self.migration_id
        ):
            raise ValueError("invalid direct migration target binding")
        schema = self.data["offline"]["application_schema"]
        if (
            not re.fullmatch(r"[a-zA-Z0-9_]{1,64}", schema)
            or target["schema"] != schema
        ):
            raise ValueError(
                "direct migration must select the application schema",
            )
        roots = {
            item["service_unit_id"]: item["nas_root"]
            for item in self.data["services"]
        }
        if target["nas_roots"] != roots:
            raise ValueError("direct migration service NAS binding changed")
        for value in roots.values():
            root = Path(value)
            if not root.is_absolute() or str(root.resolve()) != value:
                raise ValueError("invalid direct NAS root")

    def allocate(self, kind, source_position):
        try:
            return dict(self.data["assignments"][kind][source_position])
        except KeyError:
            raise ValueError(
                "direct migration IDs must be allocated by the SQL journal",
            ) from None

    def _refresh(self):
        current = self.load(self.path)
        for key in ("services", "fingerprints", "shares", "discovery_version"):
            if current.data.get(key) != self.data.get(key):
                raise ValueError("frozen source discovery binding changed")
        super()._refresh()
