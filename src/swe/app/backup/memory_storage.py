# -*- coding: utf-8 -*-
"""Guard immutable memory generations during backup and restore."""

import hashlib
import json
from pathlib import Path
import zipfile


class RestoreTargetRejected(ValueError):
    """Restore preflight failed before any destination was modified."""


def capture_memory_roots(source: Path) -> dict[str, str]:
    """Refuse in-flight cold writes and fingerprint published roots."""
    roots = {}
    if not source.exists():
        return roots
    for ledger in source.rglob(".turn-ledger"):
        if any(ledger.glob("*/pending.json")):
            raise ValueError(
                "Session ledger has a pending commit; retry backup",
            )
    for root in source.rglob("state-v2.json"):
        roots[str(root.relative_to(source))] = hashlib.sha256(
            root.read_bytes(),
        ).hexdigest()
    for directory in source.rglob("sessions"):
        for root in directory.glob("*.json"):
            payload = root.read_bytes()
            state = json.loads(payload)
            if isinstance(state, dict) and state.get("schema_version") == 3:
                from ..runner.turn_ledger import TurnLedger

                ledger = TurnLedger(root)
                # Validate every committed cold reference before accepting a backup.
                for _ in ledger.iter_cold(state):
                    pass
                roots[str(root.relative_to(source))] = hashlib.sha256(
                    payload,
                ).hexdigest()
    return roots


def verify_memory_roots(source: Path, before: dict[str, str]) -> None:
    if capture_memory_roots(source) != before:
        raise ValueError("Memory storage root changed during backup; retry")


def validate_restore_target(archive: zipfile.ZipFile, target: Path) -> None:
    """New generations require a fresh tree, never merge into live data."""
    incoming = any(
        "/.turn-ledger/" in f"/{name}" or Path(name).name == "state-v2.json"
        for name in archive.namelist()
    )
    existing = target.exists() and (
        any(target.rglob(".turn-ledger")) or any(target.rglob("state-v2.json"))
    )
    if (incoming or existing) and target.exists() and any(target.iterdir()):
        raise RestoreTargetRejected(
            "Versioned memory storage requires an empty restore target; stop writers and restore into a fresh directory",
        )
