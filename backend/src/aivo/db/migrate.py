"""Programmatic Alembic entry points (used by `aivo migrate` and tests)."""

from __future__ import annotations

import asyncio
from pathlib import Path

from alembic import command
from alembic.config import Config

MIGRATIONS_DIR = Path(__file__).parent / "migrations"


def alembic_config(database_url: str) -> Config:
    cfg = Config()
    cfg.set_main_option("script_location", str(MIGRATIONS_DIR))
    cfg.set_main_option("sqlalchemy.url", database_url)
    return cfg


def upgrade(database_url: str, revision: str = "head") -> None:
    command.upgrade(alembic_config(database_url), revision)


async def upgrade_async(database_url: str, revision: str = "head") -> None:
    # env.py uses asyncio.run(); run it in a worker thread to avoid nesting event loops
    await asyncio.to_thread(upgrade, database_url, revision)


def autogenerate(database_url: str, message: str) -> None:
    command.revision(alembic_config(database_url), message=message, autogenerate=True)
