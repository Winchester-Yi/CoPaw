# -*- coding: utf-8 -*-
"""Private binding between one SWE recheck and its Runner admission."""

from contextvars import ContextVar
from dataclasses import dataclass
from typing import Awaitable, Callable


class RecheckStartDenied(RuntimeError):
    pass


@dataclass(frozen=True)
class RecheckContext:
    session_id: str
    user_id: str
    parent_output: str
    start: Callable[[], Awaitable[bool]]


active_recheck: ContextVar[RecheckContext | None] = ContextVar(
    "active_cron_recheck",
    default=None,
)
