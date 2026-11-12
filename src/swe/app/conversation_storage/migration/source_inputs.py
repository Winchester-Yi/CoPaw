# -*- coding: utf-8 -*-
"""Explicit conversation inputs, shared by inventory and source fencing."""

from pathlib import Path
import stat
import uuid

from ..bundle import MAX_READ_BYTES
from ..chat_mutations import load_metadata_json


def _relative(value):
    path = Path(value)
    if not path.parts or path.is_absolute() or ".." in path.parts:
        raise ValueError("source path must be relative to trusted workspace")
    return path


def safe_input_entries(root, relative_roots, *, recursive=True):
    """Inspect selected entrances, never enumerate their unselected parents.

    Ancestor directories are included as shallow safety/freeze evidence.
    Missing entrances are allowed: the semantic reader checks required files.
    """
    root = Path(root).absolute()
    if root.resolve(strict=True) != root:
        raise ValueError("source root contains a symlink or is not canonical")
    found = {}

    def inspect(path):
        details = path.lstat()
        if stat.S_ISLNK(details.st_mode):
            raise ValueError(
                "source contains symlink; explicit regular-file snapshot required",
            )
        if not (
            stat.S_ISREG(details.st_mode) or stat.S_ISDIR(details.st_mode)
        ):
            raise ValueError("source contains unsupported filesystem object")
        found[path] = details
        return details

    if not stat.S_ISDIR(inspect(root).st_mode):
        raise ValueError("source root must be a directory")

    walked = set()

    def walk(path):
        pending = [path]
        while pending:
            current = pending.pop()
            if current in walked:
                continue
            details = inspect(current)
            walked.add(current)
            if stat.S_ISDIR(details.st_mode):
                pending.extend(sorted(current.iterdir(), reverse=True))

    selected = sorted({_relative(value) for value in relative_roots})
    for relative in selected:
        path = root
        for part in relative.parts:
            path = path / part
            try:
                inspect(path)
            except FileNotFoundError:
                break
        else:
            if recursive:
                walk(path)
    return sorted(found)


def session_input_roots(
    root,
    session_paths=(),
    *,
    registry_path="chats.json",
    sessions_dir="sessions",
    dialog_dir="dialog",
    max_session_bytes=MAX_READ_BYTES,
):
    """Fixed conversation entrances plus reviewed Session/ledger overrides."""
    roots = [registry_path, sessions_dir, dialog_dir]
    values = (
        session_paths.values()
        if isinstance(session_paths, dict)
        else session_paths
    )
    root = Path(root).absolute()
    for value in values:
        relative = _relative(value)
        roots.append(relative)
        # Validate each component before reading an override outside sessions/.
        entries = safe_input_entries(root, [relative], recursive=False)
        path = root / relative
        if path in entries and not path.is_file():
            raise ValueError("Session override must name a regular file")
        if (
            path not in entries
            or not path.is_file()
            or path.stat().st_size > max_session_bytes
        ):
            continue
        state = load_metadata_json(path.read_bytes())
        if isinstance(state, dict) and state.get("schema_version") == 3:
            generation = state.get("session_generation")
            if (
                not isinstance(generation, str)
                or str(uuid.UUID(generation)) != generation
            ):
                raise ValueError("Invalid session ledger generation")
            roots.append(relative.parent / ".turn-ledger" / generation)
    return tuple(roots)


def workspace_entries(mapping):
    return safe_input_entries(
        mapping.root,
        session_input_roots(
            mapping.root,
            mapping.session_paths,
            registry_path=mapping.registry_path,
            sessions_dir=mapping.sessions_dir,
            dialog_dir=mapping.dialog_dir,
        ),
    )


def workspace_files(mapping):
    return [path for path in workspace_entries(mapping) if path.is_file()]
