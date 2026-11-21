from __future__ import annotations

from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

SSL_MODES = {"disable", "allow", "prefer", "require", "verify-ca", "verify-full"}


def normalize_db_url(url: str) -> str:
    """Accept the Postgres URL forms people actually paste and return one asyncpg can use.

    * ``postgresql://`` / ``postgres://`` (e.g. DigitalOcean's connection string) -> ``postgresql+asyncpg://``
    * libpq's ``sslmode=require`` -> asyncpg's ``ssl=require`` (an explicit ``ssl`` wins)
    * everything else, including passwords with special characters, is kept as-is
    """
    u = make_url(url)
    if u.drivername in ("postgresql", "postgres", "postgresql+psycopg", "postgresql+psycopg2"):
        u = u.set(drivername="postgresql+asyncpg")
    query = dict(u.query)
    if "sslmode" in query:
        mode = query.pop("sslmode")
        if isinstance(mode, tuple):
            mode = mode[-1]
        if "ssl" not in query and mode in SSL_MODES:
            query["ssl"] = mode
        u = u.set(query=query)
    return u.render_as_string(hide_password=False)


def make_engine(url: str, *, pool_size: int = 10) -> AsyncEngine:
    return create_async_engine(normalize_db_url(url), pool_size=pool_size, max_overflow=10, pool_pre_ping=True)


def make_sessionmaker(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False)
