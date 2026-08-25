"""
Async SQLAlchemy engine/session setup.

Reads DATABASE_URL from the environment (.env supported via python-dotenv).
Accepts either a standard `postgres://` / `postgresql://` URL (e.g. a Neon
connection string) and normalizes it to the asyncpg driver form
(`postgresql+asyncpg://`) required for async SQLAlchemy.
"""

import os
from contextlib import asynccontextmanager
from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode

from dotenv import load_dotenv
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

load_dotenv()


# Query params understood by libpq/psycopg URLs but not accepted by
# asyncpg's connect() (it takes SSL config via connect_args instead).
_LIBPQ_ONLY_PARAMS = {"sslmode", "channel_binding"}


def _to_asyncpg_url(raw_url: str) -> str:
    """Normalize a postgres URL to use the asyncpg driver, and strip any
    libpq-only query params (e.g. sslmode, channel_binding) that asyncpg's
    connect() doesn't understand. SSL is instead configured via
    connect_args in _get_engine()."""
    if raw_url.startswith("postgres://"):
        raw_url = "postgresql://" + raw_url[len("postgres://"):]
    if raw_url.startswith("postgresql://"):
        raw_url = "postgresql+asyncpg://" + raw_url[len("postgresql://"):]

    parts = urlsplit(raw_url)
    query_pairs = [
        (k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
        if k.lower() not in _LIBPQ_ONLY_PARAMS
    ]
    new_query = urlencode(query_pairs)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, new_query, parts.fragment))


def get_database_url() -> str:
    raw_url = os.environ.get("DATABASE_URL")
    if not raw_url:
        raise RuntimeError(
            "DATABASE_URL is not set. Provide a Postgres connection string "
            "via the DATABASE_URL environment variable (or a .env file)."
        )
    return _to_asyncpg_url(raw_url)


_engine = None
_session_factory = None


def _get_engine():
    global _engine, _session_factory
    if _engine is None:
        url = get_database_url()
        host = urlsplit(url).hostname or ""
        is_local = host in ("localhost", "127.0.0.1") or host.startswith("192.168.")
        connect_args = {} if is_local else {"ssl": "require"}
        _engine = create_async_engine(
            url,
            pool_pre_ping=True,
            connect_args=connect_args,
        )
        _session_factory = async_sessionmaker(_engine, expire_on_commit=False)
    return _engine


@asynccontextmanager
async def get_session() -> AsyncSession:
    _get_engine()
    assert _session_factory is not None
    async with _session_factory() as session:
        yield session
