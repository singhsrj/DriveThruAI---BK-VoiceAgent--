"""
Async engine + session setup for Neon Postgres.

Uses asyncpg as the driver (psycopg2 is sync-only and won't work here).
DATABASE_URL must use the `postgresql+asyncpg://` scheme, not
`postgresql+psycopg2://` or plain `postgresql://`.

Reads DATABASE_URL from the environment (via .env). Use the pooled
connection string here for the running application; Alembic uses
DATABASE_URL_DIRECT instead (see alembic/env.py) since migrations
should not run through a connection pooler.
"""

import os
from dotenv import load_dotenv
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession

load_dotenv()


def _to_asyncpg_url(raw_url: str) -> str:
    """
    Normalizes a Postgres URL for the asyncpg driver:
    - forces the postgresql+asyncpg:// scheme
    - renames sslmode= to ssl= (asyncpg's connect() takes `ssl`, not
      `sslmode` -- that's psycopg2/libpq-only. Passing sslmode= through
      SQLAlchemy's asyncpg dialect raises:
      TypeError: connect() got an unexpected keyword argument 'sslmode')
    - drops channel_binding= entirely -- Neon's default connection
      strings include this, but asyncpg has no matching connect()
      parameter (it negotiates SCRAM channel binding automatically
      once SSL is active). Passing it through raises the same kind
      of TypeError as sslmode does.
    """
    url = make_url(raw_url)
    if url.drivername in ("postgresql", "postgres"):
        url = url.set(drivername="postgresql+asyncpg")
    elif url.drivername == "postgresql+psycopg2":
        url = url.set(drivername="postgresql+asyncpg")

    query = dict(url.query)
    if "sslmode" in query:
        query["ssl"] = query.pop("sslmode")
    query.pop("channel_binding", None)
    url = url.set(query=query)

    return url.render_as_string(hide_password=False)


DATABASE_URL = os.environ.get("DATABASE_URL")
if not DATABASE_URL:
    raise RuntimeError(
        "DATABASE_URL is not set. Copy .env.example to .env and fill in "
        "your Neon connection string (postgresql+asyncpg://...)."
    )
DATABASE_URL = _to_asyncpg_url(DATABASE_URL)

# pool_pre_ping avoids stale-connection errors -- Neon can idle out
# connections, and pre_ping checks the connection is alive before use.
engine = create_async_engine(DATABASE_URL, pool_pre_ping=True)

AsyncSessionLocal = async_sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


async def get_session() -> AsyncSession:
    """FastAPI-style async dependency."""
    async with AsyncSessionLocal() as session:
        yield session

