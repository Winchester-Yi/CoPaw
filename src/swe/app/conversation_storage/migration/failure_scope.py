# -*- coding: utf-8 -*-
"""Keep shared infrastructure failures fatal in tenant-isolated operations."""

from contextlib import contextmanager
from contextvars import ContextVar
from functools import wraps

from mysql.connector.errors import Error as MySQLError

from ..errors import StorageUnavailable

_STRICT_IO = ContextVar("conversation_tenant_strict_io", default=False)


@contextmanager
def strict_tenant_io(enabled=True):
    token = _STRICT_IO.set(enabled or _STRICT_IO.get())
    try:
        yield
    finally:
        _STRICT_IO.reset(token)


def raise_shared_failure(error):
    if not _STRICT_IO.get():
        return
    if isinstance(error, (MySQLError, StorageUnavailable)) or (
        isinstance(error, OSError) and not isinstance(error, FileNotFoundError)
    ):
        raise error


def isolated_inventory(function):
    @wraps(function)
    def wrapped(data, *args, **kwargs):
        with strict_tenant_io(data.get("tenant_isolation", False)):
            return function(data, *args, **kwargs)

    return wrapped


def isolated_verification(function):
    @wraps(function)
    async def wrapped(database, bundles, manifest, *args, **kwargs):
        with strict_tenant_io(manifest.data.get("tenant_isolation", False)):
            return await function(database, bundles, manifest, *args, **kwargs)

    return wrapped


def isolated_discovery(function):
    """Set the scope before any service/config/coverage IO is attempted."""

    @wraps(function)
    def wrapped(*args, **kwargs):
        with strict_tenant_io(kwargs.get("tenant_isolation", False)):
            return function(*args, **kwargs)

    return wrapped


def source_walk_error(error):
    """os.walk normally suppresses scandir errors; partial discovery must not."""
    raise_shared_failure(error)


def config_candidates(root):
    """Enumerate root and one tenant layer without suppressing shared IO errors."""
    from pathlib import Path
    import os
    import stat

    root = Path(root)
    if not _STRICT_IO.get():
        return [root / "config.json", *sorted(root.glob("*/config.json"))]
    result = [root / "config.json"]
    with os.scandir(root) as entries:
        for entry in entries:
            try:
                details = entry.stat(follow_symlinks=False)
            except FileNotFoundError:
                continue
            if stat.S_ISLNK(details.st_mode):
                from .source_inputs import SourceInputInvalid

                raise SourceInputInvalid(
                    "config source contains a symlink directory entry",
                )
            if stat.S_ISDIR(details.st_mode):
                candidate = Path(entry.path) / "config.json"
                try:
                    candidate.lstat()
                except FileNotFoundError:
                    continue
                result.append(candidate)
    return [result[0], *sorted(result[1:])]
