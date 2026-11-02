# -*- coding: utf-8 -*-
"""Audited operator entry point for an ended execution incarnation."""

import json
import os
from pathlib import Path
import re
import stat
from datetime import datetime, timezone

from .chat_mutations import load_metadata_json
from .identity import SessionIdentity, WorkspaceScope
from .recovery import EndedOwnerRecovery, OwnerExitProof


class RecoveryJob:
    def __init__(self, path):
        self.document = load_metadata_json(Path(path).read_bytes())
        data = self.document
        if data.get("version") != 2 or not re.fullmatch(
            r"[a-zA-Z0-9_]{1,64}",
            data.get("schema", ""),
        ):
            raise ValueError("recovery evidence version or schema is invalid")
        self.nas_root = self._root(data["nas_root"])
        self.workspace_root = self._root(
            data.get("workspace_root", data["nas_root"]),
        )
        self.identity = SessionIdentity(
            WorkspaceScope(**data["scope"]),
            **data["session"],
        )
        if data.get("service_unit_id") != self.identity.scope.service_unit_id:
            raise ValueError("recovery evidence service differs from scope")
        self.proof = OwnerExitProof(
            **{"service_unit_id": data["service_unit_id"], **data["owner"]},
        )
        if self.proof.service_unit_id != self.identity.scope.service_unit_id:
            raise ValueError("recovery proof service differs from scope")
        self.turn_id = data["turn_id"]
        if type(self.turn_id) is not int or self.turn_id <= 0:
            raise ValueError("recovery Turn identity must be positive")
        if (
            self.proof.owner_stopped is not True
            or self.proof.io_drained is not True
        ):
            raise ValueError(
                "recovery requires ended owner and drained IO evidence",
            )

    @staticmethod
    def _root(value):
        path = Path(value)
        if (
            not path.is_absolute()
            or path.is_symlink()
            or not path.is_dir()
            or path.resolve() != path
        ):
            raise ValueError(
                "recovery requires an existing original nonsymlink root",
            )
        return path

    def preview(self):
        return {
            "preview": True,
            "operation": "recover",
            "evidence": self.document,
            "next": "Verify owner incarnation exit and drained IO, then pass --apply --confirm-ended-owner and --receipt",
        }

    async def apply(self, confirmation, receipt):
        if confirmation != self.proof.owner_key:
            raise ValueError(
                "--confirm-ended-owner must exactly match the reviewed owner incarnation",
            )
        if receipt is None:
            raise ValueError("--receipt is required for an auditable recovery")
        from swe.database.config import get_database_config
        from swe.database.connection import DatabaseConnection
        from .factory import WorkspaceConversationStorage

        config = get_database_config()
        if config.database != self.document["schema"]:
            raise ValueError(
                "configured database differs from recovery evidence",
            )
        self._record(receipt, {"phase": "started", "evidence": self.document})
        database = DatabaseConnection(config)
        try:
            await database.connect()
            storage = WorkspaceConversationStorage(
                database,
                self.identity.scope,
                self.workspace_root,
                nas_root=self.nas_root,
                cursor_secret=b"offline-recovery-no-cursors",
            )
            await storage.verify_schema()
            result = await EndedOwnerRecovery(
                storage,
                verify_stopped=lambda proof: proof == self.proof
                and confirmation == proof.owner_key,
            ).recover(self.identity, self.turn_id, self.proof)
            self._record(receipt, {"phase": "completed", "result": result})
            return result
        except BaseException as error:
            self._record(
                receipt,
                {"phase": "unavailable", "error_type": type(error).__name__},
            )
            raise
        finally:
            await database.close()

    @staticmethod
    def _record(path, record):
        descriptor = os.open(
            path,
            os.O_WRONLY
            | os.O_APPEND
            | os.O_CREAT
            | os.O_NOFOLLOW
            | os.O_NONBLOCK,
            0o600,
        )
        try:
            if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                raise ValueError(
                    "recovery receipt must be a regular audit file",
                )
            content = (
                json.dumps(
                    {"at": datetime.now(timezone.utc).isoformat(), **record},
                    ensure_ascii=False,
                    sort_keys=True,
                )
                + "\n"
            ).encode("utf-8")
            with os.fdopen(descriptor, "ab", closefd=False) as output:
                output.write(content)
                output.flush()
                os.fsync(descriptor)
        finally:
            os.close(descriptor)
        directory = os.open(
            Path(path).absolute().parent,
            os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
        )
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
