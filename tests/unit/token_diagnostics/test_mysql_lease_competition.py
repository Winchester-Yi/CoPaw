# -*- coding: utf-8 -*-
"""Opt-in real MySQL competition between two independent Monitor pools."""

import asyncio
import os
from urllib.parse import unquote, urlparse

import pytest

from tests.unit.token_diagnostics.test_b_analysis import (
    analysis_result,
    leased_run,
)
from tests.unit.token_diagnostics.test_relational_storage import (
    relational_db,
)  # noqa: F401


@pytest.mark.asyncio
async def test_two_mysql_instances_cannot_publish_with_replaced_lease(
    relational_db,
):
    """Both instances select the same candidate before either claims it."""
    db = relational_db
    if db.dialect != "mysql":
        pytest.skip("requires disposable TEST_TOKEN_MYSQL_DSN")

    import aiomysql

    from monitor.app.services.token_diagnostics.store import TokenStore

    await leased_run(db)
    db.execute(
        "UPDATE swe_token_runs SET dirty=1,claim_token=NULL,lease_until=NULL",
    )
    schema = db.execute("SELECT DATABASE() AS schema_name").fetchone()[
        "schema_name"
    ]
    assert schema.startswith("test_token_diagnostics_")
    dsn = urlparse(os.environ["TEST_TOKEN_MYSQL_DSN"])
    selected = asyncio.Event()
    participant_count = 0

    class CompetingStore(TokenStore):
        async def rows(self, query, params=()):
            nonlocal participant_count
            candidates = await super().rows(query, params)
            participant_count += 1
            if participant_count == 2:
                selected.set()
            await selected.wait()
            return candidates

    pools = []
    try:
        for _ in range(2):
            pools.append(
                await aiomysql.create_pool(
                    host=dsn.hostname,
                    port=dsn.port or 3306,
                    user=unquote(dsn.username or "root"),
                    password=unquote(dsn.password or ""),
                    db=schema,
                    minsize=1,
                    maxsize=1,
                    charset="utf8mb4",
                    autocommit=True,
                    connect_timeout=3,
                ),
            )
        stores = [CompetingStore(pool, context_enabled=True) for pool in pools]
        claims = await asyncio.wait_for(
            asyncio.gather(*(store.claim_next() for store in stores)),
            timeout=10,
        )
        winners = [index for index, claim in enumerate(claims) if claim]
        assert len(winners) == 1
        winner = winners[0]
        old_claim = claims[winner]
        saved = db.execute(
            "SELECT claim_token,analysis_state FROM swe_token_runs",
        ).fetchone()
        assert saved["claim_token"] == old_claim["claim_token"]
        assert saved["analysis_state"] == "running"

        # Expiry lets another real instance claim; the original token loses
        # both renewal and publication rights even with the same revision.
        db.execute(
            "UPDATE swe_token_runs SET lease_until='2000-01-01 00:00:00'",
        )
        successor = TokenStore(pools[1 - winner], context_enabled=True)
        new_claim = await successor.claim_next()
        assert new_claim is not None
        assert new_claim["claim_token"] != old_claim["claim_token"]
        assert not await stores[winner].renew(old_claim)
        assert not await stores[winner].commit_analysis(
            old_claim,
            7,
            analysis_result(),
        )
        assert (
            db.execute(
                "SELECT COUNT(evaluation_id) AS count "
                "FROM swe_token_diagnostic_evaluations",
            ).fetchone()["count"]
            == 0
        )
        assert await successor.commit_analysis(
            new_claim,
            7,
            analysis_result(),
        )
        saved = db.execute(
            "SELECT dirty,analysis_state FROM swe_token_runs",
        ).fetchone()
        assert saved["dirty"] == 0
        assert saved["analysis_state"] == "insufficient_evidence"
        assert (
            db.execute(
                "SELECT COUNT(evaluation_id) AS count "
                "FROM swe_token_diagnostic_evaluations",
            ).fetchone()["count"]
            == 8
        )
    finally:
        for pool in pools:
            pool.close()
        for pool in pools:
            await pool.wait_closed()
