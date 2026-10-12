"""Own pool, analyzer and bounded retention job; no Trace dependency."""

import asyncio
import logging

from .analysis import Analyzer
from .query_service import TokenQueryService
from .store import TokenStore
from ....config.constant import EnvVarLoader

logger = logging.getLogger(__name__)


async def maintain(store):
    days = EnvVarLoader.get_int(
        "SWE_TOKEN_DIAGNOSTICS_RETENTION_DAYS", 30, min_value=1, max_value=365
    )
    while True:
        try:
            await asyncio.wait_for(store.cleanup(days), 5)
        except Exception as exc:
            logger.warning(
                "Token retention unavailable: %s", type(exc).__name__
            )
        await asyncio.sleep(60)


async def start_services(app):
    app.state.token_queries = None
    if not EnvVarLoader.get_bool("SWE_TRACING_ENABLED", False):
        return
    store = TokenStore()
    analyzer = Analyzer(store)
    analyzer.start()
    app.state.token_store = store
    app.state.token_analyzer = analyzer
    app.state.token_queries = TokenQueryService(store)
    app.state.token_retention = asyncio.create_task(maintain(store))


async def close_services(app):
    app.state.token_queries = None
    retention = getattr(app.state, "token_retention", None)
    if retention:
        retention.cancel()
        try:
            await retention
        except asyncio.CancelledError:
            pass
    analyzer = getattr(app.state, "token_analyzer", None)
    if analyzer:
        await analyzer.close()
    store = getattr(app.state, "token_store", None)
    if store:
        try:
            await asyncio.wait_for(store.close(), 3)
        except Exception as exc:
            logger.warning(
                "Token pool close incomplete: %s", type(exc).__name__
            )
