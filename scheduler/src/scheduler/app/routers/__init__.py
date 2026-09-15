# -*- coding: utf-8 -*-
"""Scheduler routers."""

from fastapi import APIRouter

from .cron import router as cron_router
from .batch_operations import router as batch_operations_router
from .batch_run_state import router as batch_run_state_router

api_router = APIRouter()
api_router.include_router(cron_router)
api_router.include_router(batch_operations_router)
api_router.include_router(batch_run_state_router)

__all__ = ["api_router"]
