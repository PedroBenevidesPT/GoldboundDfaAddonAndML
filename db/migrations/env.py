"""Alembic environment config.

Alembic owns the Postgres schema.  That is a deliberate change of authority:
the tables used to be created by ``executescript(SCHEMA)`` calls spread across
``woweconomy/intel/store.py``, ``woweconomy/services/tsm_snapshots.py`` and
``woweconomy/aggregates.py``, which meant there was no single description of
what the database should look like and no way to evolve it in order.  Those
calls still run against SQLite -- a workstation and the test suite need a
throwaway file -- but on Postgres the schema comes from here.
"""
from logging.config import fileConfig
from sqlalchemy import engine_from_config, pool
from alembic import context

import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# The URL comes from DATABASE_URL, so migrations target the shared Postgres
# stack instead of the SQLite file baked into alembic.ini.  Without this,
# `alembic upgrade head` would silently migrate a local file while the running
# pipeline used Postgres.
_env_url = os.environ.get("DATABASE_URL", "").strip()
if _env_url:
    config.set_main_option("sqlalchemy.url", _env_url)


def _psycopg2_url(url: str) -> str:
    """Pin the driver to psycopg2, which is what the project installs.

    SQLAlchemy 2.1 resolves a bare ``postgresql://`` URL to the psycopg (v3)
    dialect, which is not in requirements.txt, so `alembic upgrade head` failed
    with ``No module named 'psycopg'`` inside the container.  Naming the driver
    keeps one Postgres driver across the whole project instead of adding a
    second one just for migrations.
    """
    if url.startswith("postgresql://"):
        return "postgresql+psycopg2://" + url[len("postgresql://"):]
    if url.startswith("postgres://"):
        return "postgresql+psycopg2://" + url[len("postgres://"):]
    return url


config.set_main_option("sqlalchemy.url",
                       _psycopg2_url(config.get_main_option("sqlalchemy.url")))

# No autogenerate.  `db/models.py` declares four legacy tables; the other
# forty-four are defined only by the migrations.  Pointing `--autogenerate` at
# that metadata would produce a revision that drops everything it cannot see,
# so it is refused rather than made to look supported.  Revisions are written
# by hand and applied in order.
target_metadata = None


def run_migrations_offline() -> None:
    url = config.get_main_option("sqlalchemy.url")
    context.configure(url=url, target_metadata=target_metadata, literal_binds=True)
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
