"""Leases: one worker per task, and no permanent block after a crash.

The two properties that matter are tested against real SQLite, not a fake,
because they rest on SQLite's own atomicity: the upsert's ``WHERE`` is what
stops a live holder being displaced, and ``rowcount`` is what tells the
loser it lost.  A mock would test the mock.

Every test drives the clock explicitly, so "the worker died" is simulated by
simply not renewing and moving time forward -- no sleeping, no threads, no
flakiness.
"""
from __future__ import annotations

import sqlite3

import pytest

from woweconomy.intel import store
from woweconomy.pipeline import leases

T0 = 1_792_500_000.0


class Clock:
    """A clock the test moves by hand."""

    def __init__(self, now: float = T0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture()
def con():
    connection = store.connect(":memory:")
    yield connection
    connection.close()


@pytest.fixture()
def clock():
    return Clock()


@pytest.fixture()
def store_(con, clock):
    return leases.SqliteLeaseStore(con, clock=clock)


# -- acquisition -----------------------------------------------------------

def test_one_worker_wins_and_the_other_is_told_no(store_):
    first = store_.acquire("ah-fetch", ttl=60)
    second = store_.acquire("ah-fetch", ttl=60)
    assert first is not None
    assert second is None, "two workers must not both hold the same lease"


def test_different_tasks_do_not_block_each_other(store_):
    assert store_.acquire("ah-fetch", ttl=60) is not None
    assert store_.acquire("train", ttl=60) is not None


def test_a_lapsed_lease_can_be_taken_over(store_, clock):
    """This is the crash recovery: nobody released, the lease expired."""
    dead = store_.acquire("ah-fetch", ttl=60)
    assert dead is not None
    clock.advance(61)
    revived = store_.acquire("ah-fetch", ttl=60)
    assert revived is not None
    assert revived.owner != dead.owner


def test_the_takeover_is_recorded(store_, clock, con):
    store_.acquire("ah-fetch", ttl=60)
    clock.advance(61)
    store_.acquire("ah-fetch", ttl=60)
    runs = con.execute("SELECT runs FROM pipeline_lease WHERE name='ah-fetch'"
                       ).fetchone()[0]
    assert runs == 2


def test_a_lease_is_not_stolen_one_second_before_it_expires(store_, clock):
    store_.acquire("ah-fetch", ttl=60)
    clock.advance(59)
    assert store_.acquire("ah-fetch", ttl=60) is None


# -- ownership -------------------------------------------------------------

def test_only_the_owner_can_release(store_):
    mine = store_.acquire("ah-fetch", ttl=60)
    someone_else = leases.Lease(name="ah-fetch", owner="impostor",
                                expires_at=T0 + 60)
    assert store_.release(someone_else) is False
    assert store_.held("ah-fetch")
    assert store_.release(mine) is True
    assert not store_.held("ah-fetch")


def test_releasing_twice_is_not_an_error_the_second_time(store_):
    mine = store_.acquire("ah-fetch", ttl=60)
    assert store_.release(mine) is True
    assert store_.release(mine) is False


def test_a_stale_worker_cannot_renew_after_losing_the_lease(store_, clock):
    """The corruption case: worker A stalls, B takes over, A wakes up."""
    a = store_.acquire("ah-fetch", ttl=60)
    clock.advance(61)
    b = store_.acquire("ah-fetch", ttl=60)
    assert b is not None
    with pytest.raises(leases.LeaseLost):
        store_.renew(a, ttl=60)
    # and B is untouched
    assert store_.holder("ah-fetch") == b.owner


def test_a_stale_worker_cannot_release_the_new_owners_lease(store_, clock):
    a = store_.acquire("ah-fetch", ttl=60)
    clock.advance(61)
    b = store_.acquire("ah-fetch", ttl=60)
    assert store_.release(a) is False
    assert store_.holder("ah-fetch") == b.owner


def test_renewing_extends_the_lease(store_, clock):
    lease = store_.acquire("ah-fetch", ttl=60)
    clock.advance(30)
    renewed = store_.renew(lease, ttl=60)
    clock.advance(40)          # 70s after acquisition, 40s after renewal
    assert not renewed.expired(clock.now)
    assert store_.held("ah-fetch")


# -- inspection ------------------------------------------------------------

def test_holder_only_reports_live_leases(store_, clock):
    store_.acquire("ah-fetch", ttl=60)
    assert store_.holder("ah-fetch") is not None
    clock.advance(61)
    assert store_.holder("ah-fetch") is None


def test_the_snapshot_shows_lapsed_leases_as_not_live(store_, clock):
    store_.acquire("ah-fetch", ttl=60)
    store_.acquire("train", ttl=600)
    clock.advance(120)
    by_name = {row["name"]: row for row in store_.snapshot()}
    assert by_name["ah-fetch"]["live"] is False
    assert by_name["train"]["live"] is True


def test_sweep_removes_only_lapsed_leases(store_, clock):
    store_.acquire("ah-fetch", ttl=60)
    store_.acquire("train", ttl=600)
    clock.advance(120)
    assert store_.sweep() == 1
    assert [row["name"] for row in store_.snapshot()] == ["train"]


def test_renew_at_leaves_room_for_missed_heartbeats(store_):
    lease = store_.acquire("ah-fetch", ttl=90)
    assert lease.renew_at() < lease.expires_at
    # a third of the life: two renewals may be missed before anyone may take over
    assert lease.renew_at() == pytest.approx(lease.acquired_at + 30, abs=0.01)


# -- runs and stats --------------------------------------------------------

def test_a_run_is_recorded_whether_it_succeeded_or_not(con):
    leases.record_run(con, "ah-fetch", "w1", started_at=T0,
                      finished_at=T0 + 5, ok=True, detail={"books": 92})
    leases.record_run(con, "train", "w1", started_at=T0,
                      finished_at=T0 + 2, ok=False, detail={"error": "no data"})
    runs = {r["task"]: r for r in leases.recent_runs(con)}
    assert runs["ah-fetch"]["ok"] is True
    assert runs["ah-fetch"]["detail"]["books"] == 92
    assert runs["train"]["ok"] is False
    assert runs["train"]["detail"]["error"] == "no data"


def test_recent_runs_are_newest_first(con):
    for index in range(3):
        leases.record_run(con, f"t{index}", "w", started_at=T0 + index,
                          finished_at=T0 + index, ok=True)
    assert [r["task"] for r in leases.recent_runs(con)] == ["t2", "t1", "t0"]


def test_a_published_stat_is_one_row_that_moves_forward(con):
    leases.publish_stat(con, "ah.books", 10, "w1", now=T0)
    leases.publish_stat(con, "ah.books", 92, "w2", now=T0 + 60)
    assert leases.read_stat(con, "ah.books") == 92
    assert con.execute("SELECT COUNT(*) FROM pipeline_stat").fetchone()[0] == 1


def test_an_unknown_stat_returns_the_default_not_none(con):
    assert leases.read_stat(con, "nope", default={}) == {}


def test_stats_round_trip_structures(con):
    value = {"books": 92, "failed": [509, 3391], "at": T0}
    leases.publish_stat(con, "sweep", value, now=T0)
    assert leases.all_stats(con)["sweep"] == value


# -- the shared-stat mirror ------------------------------------------------

class FakeHash:
    """Just enough Redis for the stats mirror: hset and hgetall."""

    def __init__(self):
        self.hashes = {}

    def hset(self, name, key, value):
        self.hashes.setdefault(name, {})[key] = value

    def hgetall(self, name):
        return dict(self.hashes.get(name, {}))


def test_a_published_stat_is_mirrored_to_redis(con):
    mirror = FakeHash()
    leases.publish_stat(con, "ah.books", 92, "w1", now=T0, client=mirror)
    assert mirror.hashes[leases.STATS_HASH]["ah.books"] == "92"


def test_the_mirror_never_replaces_the_database_as_the_authority(con):
    mirror = FakeHash()
    leases.publish_stat(con, "ah.books", 92, "w1", now=T0, client=mirror)
    # The database is asked for the authoritative answer, and it is right
    # even if the mirror is stale or lying.
    mirror.hset(leases.STATS_HASH, "ah.books", "1")
    assert leases.all_stats(con)["ah.books"] == 92
    assert leases.read_stat(con, "ah.books") == 92


def test_hot_stats_prefers_the_mirror_when_it_has_the_value(con):
    mirror = FakeHash()
    leases.publish_stat(con, "ah.books", 92, "w1", now=T0, client=mirror)
    assert leases.hot_stats(con, client=mirror)["ah.books"] == 92


def test_hot_stats_falls_back_to_the_database_when_the_mirror_is_empty(con):
    leases.publish_stat(con, "ah.books", 92, "w1", now=T0, client=FakeHash())
    assert leases.hot_stats(con, client=FakeHash())["ah.books"] == 92


def test_hot_stats_survives_a_broken_mirror(con):
    class Broken:
        def hgetall(self, name):
            raise RuntimeError("redis is down")

        def hset(self, *a, **k):
            raise RuntimeError("redis is down")

    leases.publish_stat(con, "ah.books", 92, "w1", now=T0, client=Broken())
    assert leases.hot_stats(con, client=Broken())["ah.books"] == 92


def test_a_broken_mirror_does_not_lose_the_fact(con):
    class Broken:
        def hset(self, *a, **k):
            raise RuntimeError("redis is down")

    leases.publish_stat(con, "ah.books", 92, "w1", now=T0, client=Broken())
    assert leases.read_stat(con, "ah.books") == 92


# -- the optional mirror ---------------------------------------------------

class RecordingMirror:
    def __init__(self):
        self.calls = []

    def acquire(self, name, ttl=leases.DEFAULT_TTL, owner=None):
        self.calls.append(("acquire", name))
        return leases.Lease(name=name, owner=owner or "m", expires_at=T0 + ttl)

    def renew(self, lease, ttl=leases.DEFAULT_TTL):
        self.calls.append(("renew", lease.name))
        return lease

    def release(self, lease):
        self.calls.append(("release", lease.name))
        return True

    def holder(self, name):
        return None

    def held(self, name):
        return False


def test_the_mirror_is_told_but_never_decides(store_, con):
    mirror = RecordingMirror()
    mirrored = leases._MirroredLeaseStore(store_, mirror)
    lease = mirrored.acquire("ah-fetch", ttl=60)
    assert lease is not None
    assert ("acquire", "ah-fetch") in mirror.calls
    assert mirrored.release(lease) is True
    assert ("release", "ah-fetch") in mirror.calls


def test_a_broken_mirror_does_not_block_the_work(store_):
    class Broken:
        def acquire(self, *a, **k):
            raise RuntimeError("redis is down")

        def renew(self, *a, **k):
            raise RuntimeError("redis is down")

        def release(self, *a, **k):
            raise RuntimeError("redis is down")

    mirrored = leases._MirroredLeaseStore(store_, Broken())
    lease = mirrored.acquire("ah-fetch", ttl=60)
    assert lease is not None, "a dead mirror must not veto real work"
    assert mirrored.release(lease) is True


def test_without_a_url_the_sqlite_store_is_used(con):
    assert isinstance(leases.open_lease_store(con, url=""),
                      leases.SqliteLeaseStore)


def test_an_unreachable_url_falls_back_instead_of_raising(con):
    """No Redis server here; the pipeline must still run."""
    got = leases.open_lease_store(con, url="redis://127.0.0.1:6399/0")
    assert isinstance(got, (leases.SqliteLeaseStore, leases._MirroredLeaseStore))
