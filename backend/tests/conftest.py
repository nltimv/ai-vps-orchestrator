import asyncio
import os
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

TEST_DB = os.environ.get("AIVO_TEST_DATABASE_URL", "postgresql+asyncpg://aivo@localhost:55432/aivo_test")


async def reset_db(url: str = TEST_DB) -> None:
    from aivo.db.migrate import upgrade_async

    eng = create_async_engine(url)
    async with eng.begin() as c:
        await c.execute(text("DROP SCHEMA public CASCADE"))
        await c.execute(text("CREATE SCHEMA public"))
    await eng.dispose()
    await upgrade_async(url)


@pytest.fixture
def env(tmp_path: Path):
    from aivo.config import EnvSettings
    from aivo.storage.keyring import Keyring

    return EnvSettings(
        database_url=TEST_DB,
        media_dir=tmp_path / "media",
        scratch_dir=tmp_path / "scratch",
        media_keks=Keyring.generate_spec("k1"),
        provider="fake",
        fake_boot_seconds=0.4,
        fake_job_seconds=0.3,
        _env_file=None,
    )
