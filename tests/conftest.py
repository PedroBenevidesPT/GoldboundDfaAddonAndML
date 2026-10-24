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

The addon tests import the lupa harness as a bare module -- ``from
dfa_lua_harness import ...`` -- which works because pytest puts ``tests/`` on
``sys.path`` for a directory that is not a package.  This file must therefore
never create ``tests/__init__.py``: that file switches pytest to
package-relative imports, and every bare harness import then dies with
``ModuleNotFoundError: No module named 'dfa_lua_harness'``.  An earlier
version of this hook wrote one and broke collection in every image that ran
the suite.
"""
from __future__ import annotations

import os
import sys

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


def _remove_stale_tests_package():
    """Delete a ``tests/__init__.py`` an older conftest left behind.

    The file breaks ``from dfa_lua_harness import ...`` for every test that
    uses the shared WoW-client mock, so a checkout that still has one fails
    to collect.  Removing it here means a single test run repairs the tree.
    """
    root = os.path.dirname(os.path.abspath(__file__))
    pkg = os.path.join(root, "__init__.py")
    if os.path.exists(pkg):
        os.remove(pkg)


_remove_stale_tests_package()
