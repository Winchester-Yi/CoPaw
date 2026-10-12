# -*- coding: utf-8 -*-
from contextlib import asynccontextmanager
import logging
import os

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from ..__version__ import __version__
from ..config.constant import (
    DOCS_ENABLED,
    CORS_ORIGINS,
    DB_HOST,
    ES_HOST,
    DB_INIT_TABLES,
)
from .routers import api_router

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(
    fastapi_app: FastAPI,
):  # pylint: disable=redefined-outer-name,unused-argument
    """应用生命周期管理."""
    logger.info("Monitor service starting up...")
    logger.info(f"Environment: {os.environ.get('MONITOR_ENV', 'prd')}")

    # Initialize database connection if configured
    if DB_HOST:
        try:
            from .database import init_db_connection

            await init_db_connection()
            logger.info("Database initialized successfully")
        except Exception as e:
            logger.warning("Database initialization failed: %s", e)
            # Continue without database - some features may be unavailable
    else:
        logger.info("Database not configured (MONITOR_DB_HOST not set)")

    # Initialize Elasticsearch client if configured (用于查询 model_output)
    if ES_HOST:
        try:
            from .database import init_es_client

            es_client = await init_es_client()
            if es_client and es_client.is_connected:
                logger.info("Elasticsearch client initialized successfully")
            else:
                logger.warning(
                    "Elasticsearch client initialized but not connected",
                )
        except Exception as e:
            logger.warning("Elasticsearch initialization failed: %s", e)
    else:
        logger.info("Elasticsearch not configured (ES_HOST not set)")

    from .services.token_diagnostics.lifecycle import start_services, close_services

    await start_services(fastapi_app)
    try:
        yield
    finally:
        await close_services(fastapi_app)

    # Close Elasticsearch client on shutdown
    if ES_HOST:
        try:
            from .database import close_es_client

            await close_es_client()
            logger.info("Elasticsearch client closed")
        except Exception as e:
            logger.warning("Failed to close Elasticsearch client: %s", e)

    # Close database connection on shutdown
    if DB_HOST:
        try:
            from .database import close_db_connection

            await close_db_connection()
            logger.info("Database connection closed")
        except Exception as e:
            logger.warning("Failed to close database connection: %s", e)

    logger.info("Monitor service shutting down...")


app = FastAPI(
    title="Monitor",
    description="系统运维管理服务",
    version=__version__,
    lifespan=lifespan,
    docs_url="/docs" if DOCS_ENABLED else None,
    redoc_url="/redoc" if DOCS_ENABLED else None,
    openapi_url="/openapi.json" if DOCS_ENABLED else None,
)

if CORS_ORIGINS:
    origins = [o.strip() for o in CORS_ORIGINS.split(",") if o.strip()]
    app.add_middleware(
        CORSMiddleware,
        allow_origins=origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

app.include_router(api_router, prefix="/api")
