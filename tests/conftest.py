"""Test-session guards.

The suite runs against throwaway SQLite files, deliberately: it must be safe
to run on a workstation, in CI, and against a checkout whose ``.env`` points
at a real database.  Before this file existed, a shell that happened to export
``DATABASE_URL`` silently redirected every ``dbcompat.connect()`` in the suite
to the shared Postgres instance -- which is how a killed run left
``idle in transaction`` sessions holding locks on the real tables and blocked a
migration behind them.

The Postgres conformance tests are unaffected: they open psycopg2 connections
themselves and confine every test to a private schema they create and drop.
They read their DSN from ``TEST_DATABASE_URL``/``DATABASE_URL`` *before* this
hook runs, or fall back to the local stack.
"""
from __future__ import annotations

import os

import pytest


@pytest.fixture(scope="session", autouse=True)
def _never_the_shared_database():
    """Force the SQLite backend for the whole session, then restore it."""
    saved = {k: os.environ.get(k)
             for k in ("DATABASE_URL", "WOW_PIPELINE_BACKEND")}
    os.environ.pop("DATABASE_URL", None)
    os.environ["WOW_PIPELINE_BACKEND"] = "sqlite"
    try:
        yield
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
