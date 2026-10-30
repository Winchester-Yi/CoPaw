# -*- coding: utf-8 -*-
"""Storage errors translated at application boundaries, never into empty data."""


class StorageError(RuntimeError):
    """Conversation storage could not complete an operation."""


class IntegrityError(StorageError):
    """Stored identity, reference or content violates its contract."""


class StorageUnavailable(StorageError):
    """The authority or referenced content cannot currently be read."""


class ConflictError(StorageError):
    """The operation no longer matches its authority or revision."""


class UnknownCommit(StorageUnavailable):
    """The original operation must be reconciled before execution advances."""

    def __init__(self, operation_key: str):
        self.operation_key = operation_key
        super().__init__(
            f"Conversation commit outcome is unknown: {operation_key}",
        )
