"""
Alembic environment.

Two decisions worth knowing about:

1. **The URL comes from the application**, not from alembic.ini. ``src.database.
   session`` already resolves DATABASE_URL / SUPABASE_DB_URL / USE_SQLITE /
   discrete POSTGRES_* components; duplicating that here is how you end up
   migrating a database the app never opens.

2. **Models are imported through ``import_all_models``**, the same registry
   ``create_all`` uses. A model module nobody imports is invisible to
   autogenerate, and the resulting migration silently omits its tables.

Autogenerate is run offline by a developer, so both online and offline modes are
supported. Online mode uses an async engine because the configured driver is
asyncpg/aiosqlite.
"""

from __future__ import annotations

import asyncio
from logging.config import fileConfig

from alembic import context
from sqlalchemy.ext.asyncio import async_engine_from_config
from sqlalchemy.pool import NullPool

from src.database.models import Base, import_all_models
from src.database.session import DATABASE_URL

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# Populate Base.metadata with every model before autogenerate compares it.
import_all_models()
target_metadata = Base.metadata

# A caller that already set a URL (the migration tests, or `alembic -x`) keeps
# it; everyone else gets the application's. Escaping '%' matters because
# ConfigParser interpolates it, and Postgres URLs carry percent-encoded
# passwords.
if not config.get_main_option("sqlalchemy.url", None):
    config.set_main_option("sqlalchemy.url", DATABASE_URL.replace("%", "%%"))


def _configure(connection=None, **kwargs) -> None:
    """Shared configuration for both modes."""
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        compare_type=True,
        compare_server_default=True,
        # SQLite cannot ALTER a column in place; batch mode rewrites the table
        # instead. Harmless on Postgres, essential for local SQLite runs.
        render_as_batch=connection is not None and connection.dialect.name == "sqlite",
        **kwargs,
    )


def run_migrations_offline() -> None:
    """Emit SQL to stdout without connecting — `alembic upgrade head --sql`."""
    _configure(
        url=config.get_main_option("sqlalchemy.url"),
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )

    with context.begin_transaction():
        context.run_migrations()


def _run(connection) -> None:
    _configure(connection)
    with context.begin_transaction():
        context.run_migrations()


async def run_migrations_online() -> None:
    """Connect and run migrations against the live database."""
    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=NullPool,
    )

    async with connectable.connect() as connection:
        await connection.run_sync(_run)

    await connectable.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_migrations_online())
