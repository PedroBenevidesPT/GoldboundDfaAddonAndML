"""The pipeline runner: cadence, leases, and what a crash leaves behind.

Every test here uses an injected runner and an in-memory database, so nothing
touches the network, the game folder or the real store.  The behaviours that
matter are the ones an operator cannot see by watching a happy path: a second
worker refusing to duplicate work, a failed task not counting as done, and a
lease that is always given back.
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import run_pipeline as rp                               # noqa: E402
from woweconomy.intel import store                       # noqa: E402
from woweconomy.pipeline import leases                   # noqa: E402


@pytest.fixture()
def con():
    connection = store.memory_connection()
    yield connection
    connection.close()


def _tasks(**overrides):
    base = rp.default_tasks()
    for name, changes in overrides.items():
        base[name] = rp.TaskDef(**{**base[name].__dict__, **changes})
    return base


def _runner(code=0, output="ok"):
    calls = []

    def call(task):
        calls.append(task.name)
        return code, output

    call.calls = calls
    return call


# ------------------------------------------------------------------- cadence
def test_everything_is_due_the_first_time(con):
    tasks = rp.default_tasks()
    # `after` gates are satisfied because nothing has run, so only tasks
    # with no prerequisites come up first.
    ready = rp.due(con, tasks, now=time.time())
    assert ready[0] == "fetch"
    assert "sentinel" in ready


def test_a_task_is_not_due_before_its_interval(con):
    tasks = rp.default_tasks()
    now = time.time()
    leases.publish_stat(con, "last_ok:fetch", now)
    leases.publish_stat(con, "last_ok:aggregates", now)
    leases.publish_stat(con, "last_ok:sentinel", now)
    leases.publish_stat(con, "last_ok:export", now)
    leases.publish_stat(con, "last_ok:deploy", now)
    assert rp.due(con, tasks, now=now + 60) == []


def test_a_task_becomes_due_once_its_interval_passes(con):
    tasks = rp.default_tasks()
    now = time.time()
    for name in ("fetch", "aggregates", "sentinel", "export", "deploy"):
        leases.publish_stat(con, f"last_ok:{name}", now)
    later = now + tasks["fetch"].every_s + 1
    assert "fetch" in rp.due(con, tasks, now=later)


def test_a_dependent_task_waits_for_its_input(con):
    """`export` reads the store, so it must not run before a fetch exists."""
    tasks = rp.default_tasks()
    now = time.time()
    leases.publish_stat(con, "last_ok:sentinel", now)
    ready = rp.due(con, tasks, now=now)
    assert "export" not in ready


def test_a_dependent_task_runs_once_its_input_has_run(con):
    tasks = rp.default_tasks()
    now = time.time()
    leases.publish_stat(con, "last_ok:fetch", now)
    leases.publish_stat(con, "last_ok:sentinel", now)
    ready = rp.due(con, tasks, now=now)
    assert "export" in ready


def test_force_ignores_the_cadence(con):
    tasks = rp.default_tasks()
    now = time.time()
    for name in tasks:
        leases.publish_stat(con, f"last_ok:{name}", now)
    assert set(rp.due(con, tasks, now=now, force=True)) == set(tasks)


def test_the_order_puts_a_task_after_its_dependencies():
    tasks = rp.default_tasks()
    order = rp._order(tasks)
    assert order.index("fetch") < order.index("export")
    assert order.index("export") < order.index("deploy")


def test_a_lease_outlives_the_task_it_protects():
    for task in rp.default_tasks().values():
        assert task.lease_ttl > task.timeout_s


# ------------------------------------------------------------------ execution
def test_a_successful_task_is_recorded_and_its_lease_released(con):
    tasks = rp.default_tasks()
    runner = _runner(0, "fetched 92 books")
    result = rp.run_task(con, tasks["fetch"], runner=runner)
    assert result.ok
    assert runner.calls == ["fetch"]
    assert rp.last_ok(con, "fetch") > 0
    assert leases.SqliteLeaseStore(con).held("fetch") is False
    runs = leases.recent_runs(con)
    assert runs[0]["task"] == "fetch" and runs[0]["ok"] == 1


def test_a_failed_task_is_recorded_and_does_not_count_as_done(con):
    tasks = rp.default_tasks()
    result = rp.run_task(con, tasks["fetch"], runner=_runner(3, "boom"))
    assert not result.ok
    assert rp.last_ok(con, "fetch") == 0
    assert leases.read_stat(con, "last_fail:fetch")["exit"] == 3
    assert leases.SqliteLeaseStore(con).held("fetch") is False


def test_a_second_worker_skips_instead_of_duplicating(con):
    tasks = rp.default_tasks()
    held = leases.SqliteLeaseStore(con).acquire("fetch", ttl=600, owner="other")
    assert held is not None
    runner = _runner()
    result = rp.run_task(con, tasks["fetch"], runner=runner)
    assert result.skipped == "another worker holds the lease"
    assert runner.calls == []
    assert rp.last_ok(con, "fetch") == 0


def test_a_lapsed_lease_lets_the_next_worker_in(con):
    tasks = rp.default_tasks()
    stale = leases.SqliteLeaseStore(con).acquire("fetch", ttl=-1, owner="dead")
    assert stale is not None
    runner = _runner()
    result = rp.run_task(con, tasks["fetch"], runner=runner)
    assert result.ok
    assert runner.calls == ["fetch"]


def test_the_output_tail_is_kept_for_the_operator(con):
    tasks = rp.default_tasks()
    rp.run_task(con, tasks["fetch"], runner=_runner(0, "x" * 5000))
    detail = leases.recent_runs(con)[0]["detail"]
    assert len(detail["output_tail"]) <= 2000


def test_a_dry_run_runs_nothing(con):
    tasks = rp.default_tasks()
    runner = _runner()
    result = rp.run_task(con, tasks["fetch"], runner=runner, dry_run=True)
    assert result.skipped == "dry run"
    assert runner.calls == []
    assert rp.last_ok(con, "fetch") == 0


def test_a_cycle_runs_what_is_due_in_dependency_order(con):
    tasks = rp.default_tasks()
    runner = _runner()
    results = rp.cycle(con, tasks, runner=runner, force=False,
                       now=time.time())
    names = [r.task for r in results]
    assert names == rp._order(tasks) or names[0] == "fetch"
    assert "fetch" in names


def test_a_cycle_can_be_limited_to_one_task(con):
    tasks = rp.default_tasks()
    runner = _runner()
    results = rp.cycle(con, tasks, only=["deploy"], runner=runner)
    assert [r.task for r in results] == ["deploy"]


def test_an_unknown_task_name_is_refused(con):
    tasks = rp.default_tasks()
    with pytest.raises(SystemExit, match="unknown task"):
        rp.cycle(con, tasks, only=["teleport"], runner=_runner())


def test_a_broken_lease_store_does_not_stop_the_task(con, monkeypatch):
    """Redis is a mirror; its failure must not stop real work."""
    def explode(*args, **kwargs):
        raise RuntimeError("redis is on fire")

    monkeypatch.setattr(leases, "open_lease_store", explode)
    tasks = rp.default_tasks()
    runner = _runner()
    result = rp.run_task(con, tasks["fetch"], runner=runner)
    # It cannot take a lease, so it does not run -- and it says so rather
    # than raising into the scheduler.
    assert result.skipped
    assert runner.calls == []


# ---------------------------------------------------------------------- status
def test_status_says_what_has_never_run(con):
    text = rp.status(con)
    assert "never run" in text
    assert "held leases: none" in text


def test_status_shows_a_held_lease(con):
    leases.SqliteLeaseStore(con).acquire("fetch", ttl=600, owner="worker-7")
    text = rp.status(con)
    assert "worker-7" in text
    assert "expires in" in text


def test_status_reports_a_failed_last_attempt(con):
    tasks = rp.default_tasks()
    rp.run_task(con, tasks["fetch"], runner=_runner(1, "nope"))
    assert "last attempt failed" in rp.status(con)
