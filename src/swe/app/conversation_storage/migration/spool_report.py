# -*- coding: utf-8 -*-
"""Synchronous inventory views retaining at most one decoded spool record.

Shares remain an in-memory list. These views do not bound parser allocations or
move spool reads off the calling task; the spool must outlive every consumer.
"""

from collections.abc import Iterable, Sequence


class WorkspaceRecords(Sequence):
    def __init__(self, spool, summaries):
        self._spool = spool
        self._summaries = tuple(summaries)
        if any(
            item.index != index for index, item in enumerate(self._summaries)
        ):
            raise ValueError(
                "workspace summaries require contiguous global indexes",
            )
        self._cached_index = None
        self._cached_record = None

    def __len__(self):
        return len(self._summaries)

    def _record(self, index):
        if type(index) is not int:
            raise TypeError("workspace index must be an integer")
        if index < 0:
            index += len(self)
        if not 0 <= index < len(self):
            raise IndexError("workspace index out of range")
        if self._cached_index != index:
            self._cached_index = None
            self._cached_record = None
            summary = self._summaries[index]
            record = self._spool.read(summary.record)
            try:
                (workspace,) = record["workspaces"]
                valid = (
                    record["version"] == 2
                    and record["ready"] == (not record["blockers"])
                    and not record["shares"]
                    and tuple(workspace["scope"]) == summary.scope
                    and workspace["position"] == summary.position
                    and len(workspace["sessions"]) == summary.sessions
                    and len(workspace["chats"]) == summary.chats
                    and len(record["files"]) == summary.files
                    and tuple(record["blockers"]) == summary.blockers
                    and all(
                        item["workspace"] == index for item in record["files"]
                    )
                )
            except (KeyError, TypeError, ValueError) as error:
                raise ValueError("invalid workspace spool record") from error
            if not valid:
                raise ValueError("workspace spool record differs from summary")
            self._cached_record = record
            self._cached_index = index
        return self._cached_record

    def __getitem__(self, index):
        return self._record(index)["workspaces"][0]


class Files(Iterable):
    """Repeatable file iteration sharing the workspace record cache."""

    def __init__(self, workspaces):
        self._workspaces = workspaces

    def __iter__(self):
        for index in range(len(self._workspaces)):
            yield from self._workspaces._record(index)["files"]


def spool_report(spool, summaries, *, shares, source_boundary, blockers=()):
    """Build the importer report contract without collecting workspace bodies."""
    if not isinstance(shares, list):
        raise ValueError("shares must remain an inventory list")
    workspaces = WorkspaceRecords(spool, summaries)
    all_blockers = [
        blocker for item in workspaces._summaries for blocker in item.blockers
    ]
    all_blockers.extend(blockers)
    return {
        "version": 2,
        "workspaces": workspaces,
        "files": Files(workspaces),
        "shares": shares,
        "blockers": all_blockers,
        "ready": not all_blockers,
        "source_boundary": source_boundary,
    }
