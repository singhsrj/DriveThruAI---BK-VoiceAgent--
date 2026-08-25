import os
import asyncio
from logging.config import fileConfig

from dotenv import load_dotenv
from sqlalchemy import pool
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_engine_from_config

from alembic import context

# Make sure `import models` resolves when alembic is run from the project root.
import sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from models import Base  # noqa: E402

load_dotenv()

# this is the Alembic Config object, which provides
# access to the values within the .ini file in use.
config = context.config


def _to_asyncpg_url(raw_url: str) -> str:
    """Same normalization as database.py -- see that file for why:
    forces postgresql+asyncpg://, renames sslmode= to ssl=, and drops
    channel_binding= entirely, since asyncpg's connect() recognizes
    neither the psycopg2/libpq `sslmode` nor `channel_binding` keywords."""
    url = make_url(raw_url)
    if url.drivername in ("postgresql", "postgres", "postgresql+psycopg2"):
        url = url.set(drivername="postgresql+asyncpg")
    query = dict(url.query)
    if "sslmode" in query:
        query["ssl"] = query.pop("sslmode")
    query.pop("channel_binding", None)
    url = url.set(query=query)
    return url.render_as_string(hide_password=False)


# Migrations should run against the DIRECT (non-pooled) Neon connection,
# not the pooled one the app uses -- pgbouncer-style poolers can break
# the DDL locking Alembic relies on. Must use the asyncpg driver scheme.
db_url = os.environ.get("DATABASE_URL_DIRECT") or os.environ.get("DATABASE_URL")
if not db_url:
    raise RuntimeError(
        "Set DATABASE_URL_DIRECT (or DATABASE_URL) in your .env before running Alembic."
    )
db_url = _to_asyncpg_url(db_url)
config.set_main_option("sqlalchemy.url", db_url)

# Interpret the config file for Python logging.
# This line sets up loggers basically.
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# add your model's MetaData object here
# for 'autogenerate' support
target_metadata = Base.metadata

# other values from the config, defined by the needs of env.py,
# can be acquired:
# my_important_option = config.get_main_option("my_important_option")
# ... etc.


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode.

    This configures the context with just a URL
    and not an Engine, though an Engine is acceptable
    here as well.  By skipping the Engine creation
    we don't even need a DBAPI to be available.

    Calls to context.execute() here emit the given string to the
    script output.

    """
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )

    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection) -> None:
    context.configure(connection=connection, target_metadata=target_metadata)
    with context.begin_transaction():
        context.run_migrations()


async def run_migrations_online() -> None:
    """Run migrations in 'online' mode using an async engine.

    Alembic's migration runner itself is sync, so we bridge with
    connection.run_sync() -- this is the standard pattern for async
    SQLAlchemy + Alembic.
    """
    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)

    await connectable.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_migrations_online())
