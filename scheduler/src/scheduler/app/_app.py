# -*- coding: utf-8 -*-
"""Cron Scheduler FastAPI app."""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from .database import (
    close_db_connection,
    init_db_connection,
)

from ..__version__ import __version__
from ..config.constant import (
    DB_HOST,
    DOCS_ENABLED,
    ENV_NAME,
    get_scheduler_database_config,
)
from .routers import api_router
from .services.cron.scheduling_service import (
    cron_scheduling_runtime_enabled,
    get_cron_scheduling_service,
)
from .services.cron.batch_run_state import (
    assert_run_state_schema_ready,
    initialize_missing_controls,
)

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(fastapi_app: FastAPI):
    """Scheduler application lifecycle."""
    logger.info("Cron Scheduler service starting up")
    logger.info("Environment: %s", ENV_NAME)

    db_initialized = False
    if DB_HOST:
        try:
            db = await init_db_connection(get_scheduler_database_config())
            if cron_scheduling_runtime_enabled():
                await assert_run_state_schema_ready(db)
                await initialize_missing_controls(db)
            db_initialized = True
            logger.info("Scheduler database initialized successfully")
        except Exception as exc:  # pylint: disable=broad-except
            logger.warning("Scheduler database initialization failed: %s", exc)
    else:
        logger.info(
            "Database not configured "
            "(SCHEDULER_DB_HOST not set)",
        )

    scheduler_stop_event: asyncio.Event | None = None
    scheduler_task: asyncio.Task | None = None
    if db_initialized and cron_scheduling_runtime_enabled():
        scheduler_stop_event = asyncio.Event()
        scheduler_task = asyncio.create_task(
            get_cron_scheduling_service().run_loop(
                stop_event=scheduler_stop_event,
            ),
            name="cron-scheduler-service",
        )
        fastapi_app.state.scheduler_task = scheduler_task
        fastapi_app.state.scheduler_stop_event = scheduler_stop_event
        logger.info("Cron Scheduler loop started")
    elif DB_HOST and not db_initialized and cron_scheduling_runtime_enabled():
        logger.warning(
            "Cron Scheduler loop disabled because database initialization failed",
        )
    else:
        logger.info("Cron Scheduler loop disabled")

    yield

    if scheduler_task is not None:
        try:
            if scheduler_stop_event is not None:
                scheduler_stop_event.set()
            await asyncio.wait_for(scheduler_task, timeout=10)
            logger.info("Cron Scheduler loop stopped")
        except asyncio.TimeoutError:
            scheduler_task.cancel()
            logger.warning("Cron Scheduler loop cancelled")
        except Exception as exc:  # pylint: disable=broad-except
            logger.warning("Failed to stop Cron Scheduler loop: %s", exc)

    if DB_HOST:
        try:
            await close_db_connection()
            logger.info("Scheduler database connection closed")
        except Exception as exc:  # pylint: disable=broad-except
            logger.warning("Failed to close Scheduler database: %s", exc)


app = FastAPI(
    title="Cron Scheduler",
    description="Dispatch-managed scheduled run scheduler",
    version=__version__,
    lifespan=lifespan,
    docs_url="/docs" if DOCS_ENABLED else None,
    redoc_url="/redoc" if DOCS_ENABLED else None,
    openapi_url="/openapi.json" if DOCS_ENABLED else None,
)

app.include_router(api_router, prefix="/api")
