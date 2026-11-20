"""The live-update listener must connect with the same URL forms as the main engine."""

import asyncio
import logging

import pytest
from sqlalchemy import text

from aivo.db.session import make_engine, make_sessionmaker
from aivo.events.bus import EventBus, emit
from tests.conftest import TEST_DB, reset_db

BASE = TEST_DB  # postgresql+asyncpg://aivo@localhost:55432/aivo_test (no SSL)


@pytest.fixture
async def schema():
    await reset_db()


async def _bus_receives(url: str) -> None:
    bus = EventBus(url, initial_backoff_s=0.05)
    await bus.start()
    q = bus.subscribe()
    try:
        for _ in range(100):
            if bus.connected:
                break
            await asyncio.sleep(0.05)
        assert bus.connected, f"listener did not connect: {bus.last_error}"
        eng = make_engine(url)
        async with make_sessionmaker(eng)() as s, s.begin():
            ev = await emit(s, scope="system", type="test.ping")
        await eng.dispose()
        got = await asyncio.wait_for(q.get(), 5)
        assert got == ev.id
    finally:
        await bus.stop()


@pytest.mark.parametrize("suffix", ["?ssl=disable", "?sslmode=disable"])
async def test_listener_and_engine_accept_ssl_url_forms(schema, suffix):
    url = BASE + suffix
    eng = make_engine(url)  # main engine must accept the same URL
    async with eng.connect() as c:
        assert (await c.execute(text("select 1"))).scalar() == 1
    await eng.dispose()
    await _bus_receives(url)


async def test_plain_postgresql_scheme_works(schema):
    await _bus_receives(BASE.replace("postgresql+asyncpg://", "postgresql://") + "?sslmode=disable")


async def test_unreachable_database_logs_one_warning(caplog):
    caplog.set_level(logging.DEBUG, logger="aivo.events.bus")
    bus = EventBus("postgresql+asyncpg://aivo@127.0.0.1:1/nope", initial_backoff_s=0.01, max_backoff_s=0.02)
    await bus.start()
    await asyncio.sleep(0.6)
    await bus.stop()
    warnings = [r for r in caplog.records if r.name == "aivo.events.bus" and r.levelno >= logging.WARNING]
    assert len(warnings) == 1, [r.getMessage() for r in warnings]
    assert "live updates" in warnings[0].getMessage()
    assert bus.connected is False and bus.last_error


async def test_health_reports_live_updates(env):
    from tests.integration.harness import Harness

    async with Harness(env, role="api") as h:
        for _ in range(100):
            lu = (await h.client.get("/api/v1/health")).json()["live_updates"]
            if lu["connected"]:
                break
            await asyncio.sleep(0.05)
        assert lu == {"connected": True, "error": None}
