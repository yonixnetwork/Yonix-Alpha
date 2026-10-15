"""Alembic environment for the `crypto` schema only. Its version table is
crypto.alembic_version, so this history never touches the main one
(public.alembic_version, apps/api/migrations). Run with
`python -m yonixalpha_crypto.migrate upgrade`."""

import asyncio

from alembic import context
from sqlalchemy import pool, text
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

from yonixalpha_crypto import SCHEMA
from yonixalpha_crypto.db import Base

config = context.config
target_metadata = Base.metadata


def include_name(name, type_, parent_names):
    # Compare / migrate only our own schema.
    if type_ == "schema":
        return name == SCHEMA
    return True


def do_run_migrations(connection: Connection) -> None:
    connection.execute(text(f'CREATE SCHEMA IF NOT EXISTS "{SCHEMA}"'))
    context.configure(connection=connection, target_metadata=target_metadata, include_schemas=True,
                      include_name=include_name, version_table="alembic_version", version_table_schema=SCHEMA,
                      compare_type=True)
    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    connectable = async_engine_from_config(config.get_section(config.config_ini_section, {}), prefix="sqlalchemy.",
                                           poolclass=pool.NullPool)
    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)
        await connection.commit()
    await connectable.dispose()


asyncio.run(run_async_migrations())
