# -*- coding: utf-8 -*-
"""Explicit conversation inputs, shared by inventory and source fencing."""

from pathlib import Path
import os
import stat
import uuid

from ..bundle import MAX_READ_BYTES
from ..chat_mutations import load_metadata_json
from .spool import MigrationCapacityError


class SourceInputInvalid(ValueError):
    """Unsafe or malformed tenant source input, distinct from shared failures."""


def _relative(value):
    path = Path(value)
    if not path.parts or path.is_absolute() or ".." in path.parts:
        raise SourceInputInvalid(
            "source path must be relative to trusted workspace",
        )
    return path


def safe_input_entries(
    root,
    relative_roots,
    *,
    recursive=True,
    entry_limit=None,
    max_file_bytes=None,
    max_total_file_bytes=None,
):
    """Inspect selected entrances, never enumerate their unselected parents.

    Ancestor directories are included as shallow safety/freeze evidence.
    Missing entrances are allowed: the semantic reader checks required files.
    """
    root = Path(root).absolute()
    if root.resolve(strict=True) != root:
        raise SourceInputInvalid(
            "source root contains a symlink or is not canonical",
        )
    found = {}
    total_file_bytes = 0

    def inspect(path):
        nonlocal total_file_bytes
        details = path.lstat()
        if stat.S_ISLNK(details.st_mode):
            raise SourceInputInvalid(
                "source contains symlink; explicit regular-file snapshot required",
            )
        if not (
            stat.S_ISREG(details.st_mode) or stat.S_ISDIR(details.st_mode)
        ):
            raise SourceInputInvalid(
                "source contains unsupported filesystem object",
            )
        if path not in found:
            if entry_limit is not None and len(found) >= entry_limit:
                raise MigrationCapacityError(
                    "workspace source entries exceed migration memory allowance",
                )
            if stat.S_ISREG(details.st_mode):
                if (
                    max_file_bytes is not None
                    and details.st_size > max_file_bytes
                ):
                    raise MigrationCapacityError(
                        "workspace source file exceeds the bounded reader limit: "
                        + str(path),
                    )
                total_file_bytes += details.st_size
                if (
                    max_total_file_bytes is not None
                    and total_file_bytes > max_total_file_bytes
                ):
                    raise MigrationCapacityError(
                        "workspace source exceeds its migration memory allowance",
                    )
            found[path] = details
        return details

    if not stat.S_ISDIR(inspect(root).st_mode):
        raise SourceInputInvalid("source root must be a directory")

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
                children = []
                # Path.iterdir may materialize os.listdir before yielding.
                # Inspect and admit each entry while scandir is still bounded.
                with os.scandir(current) as entries:
                    for entry in entries:
                        child = current / entry.name
                        inspect(child)
                        children.append(child)
                pending.extend(sorted(children, reverse=True))

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
            raise SourceInputInvalid(
                "Session override must name a regular file",
            )
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
                raise SourceInputInvalid("Invalid session ledger generation")
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
