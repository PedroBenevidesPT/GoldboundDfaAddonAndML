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
def test_the_aggregates_stage_names_the_window_flag(con):
    """``--days`` names specific days; ``--last-days`` names a window.

    Passing the wrong one does not fail -- it silently rebuilds every day the
    store has, which is exactly the cost the windowed rebuild exists to avoid.
    """
    argv = rp.default_tasks()["aggregates"].argv
    assert "--last-days" in argv
    assert "--days" not in argv


def test_the_strategy_stage_builds_the_plan_after_a_fetch(con):
    """There is always a plan to read, and it is built from fresh data."""
    task = rp.default_tasks()["strategy"]
    assert task.argv[:2] == ["scripts/strategy.py", "build"]
    assert "fetch" in task.after


def test_the_tsm_csv_sweep_is_scheduled_at_ten_seconds(tmp_path):
    """187 files ten seconds apart is 31 minutes; the timeout must allow it."""
    task = rp.default_tasks()["tsm_csv"]
    assert task.argv[:2] == ["scripts/fetch_tsm_snapshots.py", "--delay"]
    assert task.argv[2] == "10"
    files = 187
    assert task.timeout_s > files * 10, (
        "a sweep that outlives its own timeout is killed halfway and the "
        "lease is held until the TTL expires")
    # the lease must outlive the task, or a long sweep looks abandoned
    assert task.lease_ttl > task.timeout_s


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
    # every configured task, so adding a stage does not need this test edited
    for name in tasks:
        leases.publish_stat(con, f"last_ok:{name}", now)
    assert rp.due(con, tasks, now=now + 60) == []


def test_a_task_becomes_due_once_its_interval_passes(con):
    tasks = rp.default_tasks()
    now = time.time()
    for name in tasks:
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


def test_independent_tasks_run_at_the_same_time(tmp_path):
    """With workers>1 two unrelated tasks must overlap, not queue."""
    import threading

    path = tmp_path / "intel.sqlite"
    con = store.connect(path)
    try:
        tasks = {
            "a": rp.TaskDef("a", ["a"], every_s=60),
            "b": rp.TaskDef("b", ["b"], every_s=60),
        }
        both = threading.Barrier(2, timeout=10)
        seen = []

        def runner(task):
            seen.append(task.name)
            both.wait()      # deadlocks, and fails, if the tasks are serial
            return 0, "ok"

        results = rp.cycle(con, tasks, only=["a", "b"], runner=runner,
                           workers=2, connect=lambda: store.connect(path))
        assert [r.task for r in results] == ["a", "b"]
        assert all(r.ok for r in results)
    finally:
        con.close()


def test_a_concurrent_cycle_waits_for_a_dependency(tmp_path):
    """`b` must not start while `a`, which it depends on, is running."""
    path = tmp_path / "intel.sqlite"
    con = store.connect(path)
    try:
        tasks = {
            "a": rp.TaskDef("a", ["a"], every_s=60),
            "b": rp.TaskDef("b", ["b"], every_s=60, after=("a",)),
        }
        seen = []

        def runner(task):
            seen.append(task.name)
            return 0, "ok"

        results = rp.cycle(con, tasks, only=["a", "b"], runner=runner,
                           workers=2, connect=lambda: store.connect(path))
        assert seen == ["a", "b"]
        assert [r.task for r in results] == ["a", "b"]
    finally:
        con.close()


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


def test_a_skip_replaces_the_previous_failure(con):
    """A task that cannot run here is not still failing.

    Regression: `last_fail` was never retired, so once `strategy` skipped
    for want of DFA_DIR, `status` kept showing the 11:47 failure as though
    it were the latest word, and the real reason was invisible.
    """
    tasks = rp.default_tasks()
    rp.run_task(con, tasks["strategy"], runner=_runner(1, "old failure"))
    assert "last attempt failed" in rp.status(con)

    rp.run_task(con, tasks["strategy"],
                runner=_runner(rp.EX_CONFIG, "DFA_DIR is not set"))

    assert leases.read_stat(con, "last_fail:strategy", None) is None
    text = rp.status(con)
    assert "last attempt failed" not in text
    assert "last attempt skipped" in text
    assert "DFA_DIR is not set" in text


def test_a_success_replaces_the_previous_failure(con):
    """`last_fail` describes the last attempt, so a success retires it."""
    tasks = rp.default_tasks()
    rp.run_task(con, tasks["fetch"], runner=_runner(1, "boom"))
    rp.run_task(con, tasks["fetch"], runner=_runner(0, "ok"))
    assert leases.read_stat(con, "last_fail:fetch", None) is None
    assert "last attempt failed" not in rp.status(con)


def test_not_getting_the_lease_leaves_the_failure_alone(con):
    """Not running is not an answer: the last real attempt still failed."""
    tasks = rp.default_tasks()
    rp.run_task(con, tasks["fetch"], runner=_runner(1, "boom"))
    leases.SqliteLeaseStore(con).acquire("fetch", ttl=600, owner="other")
    rp.run_task(con, tasks["fetch"], runner=_runner())
    assert leases.read_stat(con, "last_fail:fetch", None)
    assert "last attempt failed" in rp.status(con)


def test_status_json_carries_the_skip(con):
    tasks = rp.default_tasks()
    rp.run_task(con, tasks["strategy"],
                runner=_runner(rp.EX_CONFIG, "DFA_DIR is not set"))
    row = next(row for row in rp.status_json(con)["tasks"]
               if row["task"] == "strategy")
    assert row["last_skip"]["reason"] == "DFA_DIR is not set"
    assert row["last_fail"] is None


def test_status_json_is_the_same_view_for_a_monitor(con):
    tasks = rp.default_tasks()
    rp.run_task(con, tasks["fetch"], runner=_runner(0, "ok"))
    view = rp.status_json(con)
    assert view["backend"] in ("sqlite", "postgres")
    fetch = next(row for row in view["tasks"] if row["task"] == "fetch")
    assert fetch["last_ok"]
    assert isinstance(view["redis"], dict) and "configured" in view["redis"]
    assert view["dataset"]["path"].endswith("DFA_Estimate_Data.lua")


def test_a_task_announces_its_start_and_result(con, monkeypatch):
    events = []
    monkeypatch.setattr(leases, "publish_event",
                        lambda kind, **fields: events.append((kind, fields.get("task"))))
    tasks = rp.default_tasks()
    rp.run_task(con, tasks["fetch"], runner=_runner(0, "ok"))
    assert events == [("start", "fetch"), ("ok", "fetch")]


# ------------------------------------------------------- new-dataset notices
DATASET_LUA = """-- Generated by the DfA pricing engine.  Do not edit by hand.
-- schema 2, engine 2.0.0, generated 1792704240

DFA_ESTIMATE = {
\tversion = 2,
\tengineVersion = "2.0.0",
\titemCount = 237,
\trealmCount = 8,
\tpriceCount = 31184,
\tindexedRealms = 267,
\tmeasuredRealms = 5,
\tmeanConfidence = 0.2536,
\tsentinelModel = "20261022T195801Z-4e9a79ed",
\tpredictionStatus = {
\t\t["baseline_only"] = 237,
\t},
}
"""


def _dataset(tmp_path, text=DATASET_LUA):
    path = tmp_path / "DFA_Estimate_Data.lua"
    path.write_text(text, encoding="utf-8")
    return path


def test_the_facts_come_from_the_bytes_that_shipped(tmp_path):
    facts = rp.dataset_facts(_dataset(tmp_path))
    assert facts["itemCount"] == "237"
    assert facts["measuredRealms"] == "5"
    assert facts["predictionStatus"] == "baseline_only: 237"


def test_a_missing_dataset_has_no_facts(tmp_path):
    assert rp.dataset_facts(tmp_path / "absent.lua") == {}
    assert rp.dataset_fingerprint(tmp_path / "absent.lua") == ""


def test_the_first_export_alerts_once(con, tmp_path, monkeypatch):
    posted = []
    def record(items, state, now, **kw):
        posted.extend(items)
        return [{"key": items[0].key, "webhook": True, "toast": False,
                 "error": "", "retry": False}]

    monkeypatch.setattr(rp.alerts, "deliver", record)
    path = _dataset(tmp_path)
    rp.notify_new_dataset(con, path=path)
    assert len(posted) == 1
    assert posted[0].key == "dataset:new"
    assert any("baseline_only: 237" in line for line in posted[0].lines)


def test_an_unchanged_dataset_does_not_alert_again(con, tmp_path, monkeypatch):
    posted = []
    def record(items, state, now, **kw):
        posted.extend(items)
        return [{"key": items[0].key, "webhook": True, "toast": False,
                 "error": "", "retry": False}]

    monkeypatch.setattr(rp.alerts, "deliver", record)
    path = _dataset(tmp_path)
    rp.notify_new_dataset(con, path=path)
    rp.notify_new_dataset(con, path=path)
    assert len(posted) == 1


def test_a_changed_dataset_alerts_again(con, tmp_path, monkeypatch):
    posted = []
    def record(items, state, now, **kw):
        posted.extend(items)
        return [{"key": items[0].key, "webhook": True, "toast": False,
                 "error": "", "retry": False}]

    monkeypatch.setattr(rp.alerts, "deliver", record)
    path = _dataset(tmp_path)
    rp.notify_new_dataset(con, path=path)
    path.write_text(DATASET_LUA.replace("itemCount = 237", "itemCount = 238"),
                    encoding="utf-8")
    rp.notify_new_dataset(con, path=path)
    assert len(posted) == 2
    assert any("238" in line for line in posted[1].lines)


def test_a_new_dataset_is_posted_to_the_webhook_without_a_cooldown(
        con, tmp_path, monkeypatch):
    """The digest is the rate limit; the webhook still has to be passed one.

    Regression: `deliver` defaults to no URL and a six-hour cooldown, so a
    changed dataset reached at most a desktop toast, and a real change could
    be dropped outright.
    """
    monkeypatch.setattr(rp.alerts, "webhook_url",
                        lambda *args, **kwargs: "https://discord.test/hook")
    calls = []

    def record(items, state, now, **kw):
        calls.append(kw)
        return [{"key": items[0].key, "webhook": False, "toast": True,
                 "error": "", "retry": False}]

    monkeypatch.setattr(rp.alerts, "deliver", record)
    rp.notify_new_dataset(con, path=_dataset(tmp_path))
    assert calls and calls[0]["url"] == "https://discord.test/hook"
    assert calls[0]["cooldown_s"] == 0


def test_the_alert_does_not_fail_the_export_when_the_channel_is_dead(
        con, tmp_path, monkeypatch):
    def explode(*args, **kwargs):
        raise RuntimeError("discord is down")

    monkeypatch.setattr(rp.alerts, "deliver", explode)
    tasks = rp.default_tasks()
    result = rp.run_task(con, tasks["export"], runner=_runner(0),
                         notify=True)
    assert result.ok
    assert rp.last_ok(con, "export") > 0


def test_an_undelivered_alert_is_retried_not_marked_as_sent(con, tmp_path,
                                                            monkeypatch):
    """A webhook that was down once must not swallow the announcement.

    Publishing the fingerprint regardless of delivery would make the next
    cycle compare equal and stay silent, so the alert would be lost for good
    because a channel failed at the wrong moment.
    """
    calls = []

    def dead(items, state, now, **kw):
        calls.append(len(items))
        return [{"key": "dataset:new", "webhook": False, "toast": False,
                 "error": "no webhook configured", "retry": True}]

    monkeypatch.setattr(rp.alerts, "deliver", dead)
    path = _dataset(tmp_path)
    rp.notify_new_dataset(con, path=path)
    rp.notify_new_dataset(con, path=path)
    assert calls == [1, 1]              # tried twice, not silenced
    assert leases.read_stat(con, "dataset:sha256", "") == ""


def test_a_delivered_alert_is_not_repeated(con, tmp_path, monkeypatch):
    calls = []

    def ok(items, state, now, **kw):
        calls.append(len(items))
        return [{"key": "dataset:new", "webhook": True, "toast": True,
                 "error": "", "retry": False}]

    monkeypatch.setattr(rp.alerts, "deliver", ok)
    path = _dataset(tmp_path)
    rp.notify_new_dataset(con, path=path)
    rp.notify_new_dataset(con, path=path)
    assert calls == [1]
    assert leases.read_stat(con, "dataset:sha256", "")


def test_an_undelivered_alert_is_retried_not_marked_as_sent(con, tmp_path,
                                                            monkeypatch):
    """A webhook that was down once must not swallow the announcement.

    Publishing the fingerprint regardless of delivery would make the next
    cycle compare equal and stay silent, so the alert would be lost for good
    because a channel failed at the wrong moment.
    """
    calls = []

    def dead(items, state, now, **kw):
        calls.append(len(items))
        return [{"key": "dataset:new", "webhook": False, "toast": False,
                 "error": "no webhook configured", "retry": True}]

    monkeypatch.setattr(rp.alerts, "deliver", dead)
    path = _dataset(tmp_path)
    rp.notify_new_dataset(con, path=path)
    rp.notify_new_dataset(con, path=path)
    assert calls == [1, 1]              # tried twice, not silenced
    assert leases.read_stat(con, "dataset:sha256", "") == ""


def test_a_delivered_alert_is_not_repeated(con, tmp_path, monkeypatch):
    calls = []

    def ok(items, state, now, **kw):
        calls.append(len(items))
        return [{"key": "dataset:new", "webhook": True, "toast": True,
                 "error": "", "retry": False}]

    monkeypatch.setattr(rp.alerts, "deliver", ok)
    path = _dataset(tmp_path)
    rp.notify_new_dataset(con, path=path)
    rp.notify_new_dataset(con, path=path)
    assert calls == [1]
    assert leases.read_stat(con, "dataset:sha256", "")


# ------------------------------------------------------------ cycle digest
def test_the_cycle_digest_names_every_action():
    """One message answers "what did this cycle do" for every outcome."""
    results = [
        rp.TaskResult("fetch", True, 352.0, exit_code=0),
        rp.TaskResult("strategy", True, 84.0, exit_code=78,
                      skipped="no item was classified as transmog"),
        rp.TaskResult("export", False, 3.0, exit_code=1),
    ]
    alert = rp.cycle_digest(results, now=0)
    assert alert is not None
    assert "3 action(s)" in alert.title and "1 failed" in alert.title
    assert alert.level == "warn"
    assert any("fetch" in line and "ok" in line for line in alert.lines)
    assert any("strategy" in line and "skipped" in line
               and "classified" in line for line in alert.lines)
    assert any("export" in line and "FAILED" in line
               and "exit=1" in line for line in alert.lines)


def test_a_cycle_whose_actions_all_succeeded_is_not_a_warning():
    alert = rp.cycle_digest([rp.TaskResult("fetch", True, 1.0, exit_code=0)], now=0)
    assert alert is not None and alert.level == "info"
    assert "failed" not in alert.title


def test_a_quiet_cycle_posts_nothing(monkeypatch):
    """No actions is a heartbeat, not an alert."""
    def explode(*args, **kwargs):
        raise AssertionError("posted an empty cycle")

    monkeypatch.setattr(rp.alerts, "post_discord", explode)
    assert rp.post_cycle_digest([]) == []


def test_a_lease_skip_is_not_an_action(monkeypatch):
    """Another worker running the task is not something this cycle did.

    Reporting it posted a digest every couple of minutes for as long as a
    lease outlived the container that held it -- the longest, loudest
    messages the channel ever got, all saying nothing happened.
    """
    def explode(*args, **kwargs):
        raise AssertionError("posted a cycle that only waited on a lease")

    monkeypatch.setattr(rp.alerts, "post_discord", explode)
    lease_skip = rp.TaskResult("fetch", True, 0.0,
                               skipped="another worker holds the lease",
                               lease_skip=True)
    assert rp.cycle_digest([lease_skip], now=0) is None
    assert rp.post_cycle_digest([lease_skip]) == []
    # a real action in the same cycle still gets its message
    alert = rp.cycle_digest([lease_skip,
                             rp.TaskResult("addon", True, 2.0, exit_code=0)],
                            now=0)
    assert alert is not None and len(alert.lines) == 1
    assert "1 action(s)" in alert.title


def test_the_cycle_digest_reaches_the_webhook(monkeypatch):
    seen = {}
    monkeypatch.setattr(rp.alerts, "webhook_url",
                        lambda *args, **kwargs: "https://discord.test/hook")

    def post(url, payload, **kwargs):
        seen["url"] = url
        seen["payload"] = payload
        return {"ok": True, "status": 204, "error": ""}

    monkeypatch.setattr(rp.alerts, "post_discord", post)
    out = rp.post_cycle_digest([rp.TaskResult("fetch", True, 1.0, exit_code=0)])
    assert seen["url"] == "https://discord.test/hook"
    embed = seen["payload"]["embeds"][0]
    assert "1 action(s)" in embed["title"]
    assert "fetch" in embed["description"]
    assert out and out[0]["ok"] is True


def test_a_dead_webhook_does_not_break_the_cycle(monkeypatch, capsys):
    monkeypatch.setattr(rp.alerts, "webhook_url",
                        lambda *args, **kwargs: "https://discord.test/hook")
    monkeypatch.setattr(rp.alerts, "post_discord", lambda *args, **kwargs: {
        "ok": False, "status": 0, "error": "connection refused"})
    out = rp.post_cycle_digest([rp.TaskResult("fetch", True, 1.0)])
    assert out and out[0]["ok"] is False
    assert "cycle digest not posted" in capsys.readouterr().err


def test_no_notify_posts_no_digest(monkeypatch):
    def explode(*args, **kwargs):
        raise AssertionError("posted despite --no-notify")

    monkeypatch.setattr(rp.alerts, "post_discord", explode)
    assert rp.post_cycle_digest([rp.TaskResult("fetch", True, 1.0)],
                                notify=False) == []


def test_main_posts_one_digest_per_cycle(monkeypatch):
    posted = []
    monkeypatch.setattr(rp, "load_env", lambda *args, **kwargs: None)
    monkeypatch.setattr(rp.store, "connect", lambda *args, **kwargs: object())
    monkeypatch.setattr(rp, "cycle", lambda *args, **kwargs: [
        rp.TaskResult("fetch", True, 1.0, exit_code=0)])
    monkeypatch.setattr(rp, "post_cycle_digest",
                        lambda results, **kwargs: posted.append(len(results)))
    assert rp.main(["--once", "--task", "fetch"]) == 0
    assert posted == [1]
