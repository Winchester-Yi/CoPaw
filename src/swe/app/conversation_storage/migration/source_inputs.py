# -*- coding: utf-8 -*-
"""Validate selected legacy input paths before auditing them."""

from pathlib import Path
import os
import stat


class SourceInputInvalid(ValueError):
    pass


class MigrationCapacityError(ValueError):
    pass


def _relative(value):
    path = Path(value)
    if not path.parts or path.is_absolute() or ".." in path.parts:
        raise SourceInputInvalid(
            "source path must be relative to trusted workspace",
        )
    return path


def _inspect_input_entry(
    path,
    *,
    explicit,
    trusted,
    allow_in_root_symlinks,
    skip_unselected_unsafe,
    found,
    entry_limit,
    max_file_bytes,
    max_total_file_bytes,
    total_file_bytes,
):
    details = _input_entry_stat(
        path,
        trusted,
        allow_in_root_symlinks=allow_in_root_symlinks,
        skip_unsafe=skip_unselected_unsafe and path not in explicit,
    )
    if details is None:
        return None, total_file_bytes
    if path not in found:
        if entry_limit is not None and len(found) >= entry_limit:
            raise MigrationCapacityError(
                "workspace source entries exceed migration memory allowance",
            )
        if stat.S_ISREG(details.st_mode) and (
            max_file_bytes is not None or max_total_file_bytes is not None
        ):
            size = details.st_size
            if max_file_bytes is not None and size > max_file_bytes:
                raise MigrationCapacityError(
                    "workspace source file exceeds the bounded reader limit: "
                    + str(path),
                )
            if max_total_file_bytes is not None:
                total_file_bytes += size
                if total_file_bytes > max_total_file_bytes:
                    raise MigrationCapacityError(
                        "workspace source exceeds its migration memory allowance",
                    )
        found[path] = details
    return details, total_file_bytes


def _input_entry_stat(path, trusted, *, allow_in_root_symlinks, skip_unsafe):
    details = path.lstat()
    if stat.S_ISLNK(details.st_mode):
        if not allow_in_root_symlinks:
            raise SourceInputInvalid(
                "source contains symlink; explicit regular-file snapshot required",
            )
        try:
            resolved = path.resolve(strict=True)
        except FileNotFoundError:
            if skip_unsafe:
                return None
            raise SourceInputInvalid(
                "selected source symlink is broken",
            ) from None
        if not resolved.is_relative_to(trusted):
            if skip_unsafe:
                return None
            raise SourceInputInvalid("selected source path escapes workspace")
        details = resolved.stat()
    if not (stat.S_ISREG(details.st_mode) or stat.S_ISDIR(details.st_mode)):
        if skip_unsafe:
            return None
        raise SourceInputInvalid(
            "source contains unsupported filesystem object",
        )
    return details


def safe_input_entries(
    root,
    relative_roots,
    *,
    recursive=True,
    entry_limit=None,
    max_file_bytes=None,
    max_total_file_bytes=None,
    trusted_root=None,
    allow_in_root_symlinks=False,
    skip_unselected_unsafe=False,
):
    """Inspect selected entrances, never enumerate their unselected parents.

    Ancestor directories are included as shallow safety/freeze evidence.
    Missing entrances are allowed: the semantic reader checks required files.
    """
    root = Path(root).absolute()
    found = {}
    total_file_bytes = 0
    trusted = Path(trusted_root or root).resolve(strict=True)
    resolved_root = root.resolve(strict=True)
    if resolved_root != root and (
        not allow_in_root_symlinks or not resolved_root.is_relative_to(trusted)
    ):
        raise SourceInputInvalid(
            "source root contains a symlink or is not canonical",
        )
    selected = {_relative(value) for value in relative_roots}
    explicit = {root / value for value in selected}

    def inspect(path):
        nonlocal total_file_bytes
        details, total_file_bytes = _inspect_input_entry(
            path,
            explicit=explicit,
            trusted=trusted,
            allow_in_root_symlinks=allow_in_root_symlinks,
            skip_unselected_unsafe=skip_unselected_unsafe,
            found=found,
            entry_limit=entry_limit,
            max_file_bytes=max_file_bytes,
            max_total_file_bytes=max_total_file_bytes,
            total_file_bytes=total_file_bytes,
        )
        return details

    if not stat.S_ISDIR(inspect(root).st_mode):
        raise SourceInputInvalid("source root must be a directory")

    walked = set()

    def walk(path):
        pending = [path]
        while pending:
            current = pending.pop()
            details = inspect(current)
            if details is None:
                continue
            canonical = current.resolve(strict=True)
            if canonical in walked:
                continue
            walked.add(canonical)
            if stat.S_ISDIR(details.st_mode):
                children = []
                # Path.iterdir may materialize os.listdir before yielding.
                # Inspect and admit each entry while scandir is still bounded.
                with os.scandir(current) as entries:
                    for entry in entries:
                        child = current / entry.name
                        if inspect(child) is not None:
                            children.append(child)
                pending.extend(sorted(children, reverse=True))

    for relative in sorted(selected):
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
