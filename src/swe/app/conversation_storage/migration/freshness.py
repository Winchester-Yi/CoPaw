# -*- coding: utf-8 -*-
"""Lightweight source mutation checks for tenant-scoped migration runs."""

import os
from pathlib import Path
import stat


def _walk_error(error):
    # Incomplete scans can never prove source freshness, including legacy jobs.
    raise error


def tenant_key(service_unit_id, tenant_id):
    return service_unit_id + "\x00" + tenant_id


def changed_source(paths, cutoff_ns, *, checked=None, recursive=True):
    """Return the first source path whose mtime is newer than the cutoff."""
    checked = set() if checked is None else checked
    for value in paths:
        root = Path(value)
        if root in checked or (
            recursive and any(parent in checked for parent in root.parents)
        ):
            continue
        try:
            root_details = root.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(root_details.st_mode):
            return root, root_details.st_mtime_ns
        candidates = [root]
        if recursive and root.is_dir():
            for directory, names, files in os.walk(
                root,
                followlinks=False,
                onerror=_walk_error,
            ):
                names.sort()
                files.sort()
                candidates.extend(Path(directory) / name for name in names)
                candidates.extend(Path(directory) / name for name in files)
        for path in candidates:
            details = path.lstat()
            if stat.S_ISLNK(details.st_mode):
                return path, details.st_mtime_ns
            if details.st_mtime_ns > cutoff_ns:
                return path, details.st_mtime_ns
        checked.add(root)
    return None
