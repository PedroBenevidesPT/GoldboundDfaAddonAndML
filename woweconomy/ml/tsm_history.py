"""Point-in-time reconstruction of the TSM market state.

``tsm_price_change`` is a *change* table: it stores a row only for the keys
whose prices moved in that snapshot, so the state of a key at time ``T`` is
the value of the **last change at or before** ``T``.  Anything later than
``T`` is, by construction, in the future and must not be visible.

Two clocks exist for every snapshot and the engine has to pick one:

``updated_at``
    the timestamp TSM itself put on the data -- when the market state was
    true.  This is the economically correct clock: a buyer standing in the
    realm at time ``T`` could see this data.

``fetched_at``
    when *this project* downloaded the file.  Using it is the pessimistic
    view: it never leaks, but it throws away data that was public.

The default is ``updated_at``; :class:`MLConfig` switches it.

The legacy ``market_snapshots`` table holds *complete* region states from
older imports (2026-07-28 and 2026-10-13).  They are folded into the region
timeline so the regional reference has a real history instead of two points.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

#: The value fields a realm row carries.
REALM_FIELDS = ("market_value", "min_buyout", "recent", "historical")
#: The value fields a region row carries.
REGION_FIELDS = ("market_value", "historical", "avg_sale_price",
                 "sale_rate", "sold_per_day")

ALL_FIELDS = ("market_value", "min_buyout", "recent", "historical",
              "avg_sale_price", "sale_rate", "sold_per_day")


def parse_ts(value: str) -> int:
    """ISO-8601 -> epoch seconds.  Accepts the trailing ``Z``."""
    if not value:
        return 0
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return 0
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp())


def iso(ts: int) -> str:
    if not ts:
        return ""
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass
class Snapshot:
    """One TSM snapshot of one scope."""

    snapshot_id: int
    scope: str
    kind: str
    updated_at: int
    fetched_at: int

    @property
    def availability(self) -> int:
        return self.updated_at


@dataclass
class _Series:
    """The change history of one (scope, kind).

    The snapshots are kept in *time* order, which is the order the state
    has to be replayed in.  The snapshot ids are not in time order -- the
    legacy whole-state imports were injected with fresh ids -- so the
    replay walks the list, never the ids.
    """

    scope: str
    kind: str
    snapshots: List[Snapshot] = field(default_factory=list)
    #: snapshot_id -> {key_id: values tuple}
    changes: Dict[int, Dict[int, Tuple]] = field(default_factory=dict)
    #: memoised full states, one per snapshot boundary
    _states: Dict[int, Dict[int, Tuple]] = field(default_factory=dict)

    def sort_snapshots(self) -> None:
        self.snapshots.sort(key=lambda s: (s.updated_at, s.snapshot_id))
        self._states.clear()

    def state_at_snapshot(self, snapshot_id: int) -> Dict[int, Tuple]:
        cached = self._states.get(snapshot_id)
        if cached is not None:
            return cached
        state: Dict[int, Tuple] = {}
        for snap in self.snapshots:
            state.update(self.changes.get(snap.snapshot_id, {}))
            self._states[snap.snapshot_id] = dict(state)
            if snap.snapshot_id == snapshot_id:
                break
        return self._states.get(snapshot_id, {})

    def snapshot_for(self, ts: int, clock: str = "updated") -> Optional[Snapshot]:
        """The newest snapshot whose availability is at or before ``ts``."""
        if clock == "updated":
            # the snapshots are in time order, so the answer is the last
            # one at or before the moment asked about
            best: Optional[Snapshot] = None
            for snap in self.snapshots:
                if snap.updated_at <= ts:
                    best = snap
                else:
                    break
            return best
        best = None
        for snap in self.snapshots:
            if snap.fetched_at <= ts:
                if best is None or snap.fetched_at > best.fetched_at:
                    best = snap
        return best


class TSMHistory:
    """Point-in-time access to the TSM mirror."""

    def __init__(self, db_path: Path | str,
                 scopes: Optional[Iterable[str]] = None,
                 kinds: Sequence[str] = ("realm_items", "region_items"),
                 availability_clock: str = "updated") -> None:
        self.db_path = Path(db_path)
        self.availability_clock = availability_clock
        self._series: Dict[Tuple[str, str], _Series] = {}
        self._realm_slug: Dict[str, str] = {}
        self._canonical: Dict[str, Tuple[str, str, int]] = {}
        self._item_names: Dict[str, Dict[int, str]] = {}
        self._load_realm_map()
        self._load(kinds, scopes)

    # -- loading ---------------------------------------------------------
    def _load_realm_map(self) -> None:
        con = sqlite3.connect(self.db_path)
        try:
            for name, slug, canon_slug, canon_name, gid in con.execute(
                    "SELECT realm_name, realm_slug, canonical_slug, "
                    "canonical_name, group_id FROM tsm_realm_group"):
                self._realm_slug[str(name)] = str(slug)
                self._canonical[str(name)] = (str(canon_slug), str(canon_name), int(gid))
        except sqlite3.OperationalError:
            pass
        finally:
            con.close()

    def _load(self, kinds: Sequence[str], scopes: Optional[Iterable[str]]) -> None:
        con = sqlite3.connect(self.db_path)
        try:
            # The scope filter is about *realms*; the region is a single
            # scope that every realm needs, so it is always loaded.
            realm_kinds = [k for k in kinds if k.startswith("realm")]
            other_kinds = [k for k in kinds if not k.startswith("realm")]

            def fetch(scope_filter: Optional[Iterable[str]], want: Sequence[str]) -> list:
                if not want:
                    return []
                sql = ("SELECT s.snapshot_id, s.scope, s.kind, s.updated_at, s.fetched_at "
                       "FROM tsm_snapshot s WHERE s.kind IN (%s)" % ",".join("?" * len(want)))
                params: List = list(want)
                if scope_filter:
                    slugs = list(scope_filter)
                    sql += " AND s.scope IN (%s)" % ",".join("?" * len(slugs))
                    params.extend(slugs)
                sql += " ORDER BY s.scope, s.kind, s.updated_at"
                return con.execute(sql, params).fetchall()

            rows = fetch(scopes, realm_kinds) + fetch(None, other_kinds)
            for sid, scope, kind, upd, fet in rows:
                key = (str(scope), str(kind))
                series = self._series.setdefault(key, _Series(scope=str(scope), kind=str(kind)))
                series.snapshots.append(Snapshot(int(sid), str(scope), str(kind),
                                                 parse_ts(upd), parse_ts(fet)))
            # load the change rows
            for (scope, kind), series in self._series.items():
                changes: Dict[int, Dict[int, Tuple]] = {}
                cur = con.execute(
                    "SELECT snapshot_id, key_id, market_value, min_buyout, recent, "
                    "historical, avg_sale_price, sale_rate, sold_per_day "
                    "FROM tsm_price_change WHERE scope=? AND kind=?", (scope, kind))
                for (sid, kid, mv, mb, rc, hi, asp, sr, spd) in cur:
                    changes.setdefault(int(sid), {})[int(kid)] = (
                        int(mv or 0), int(mb or 0), int(rc or 0), int(hi or 0),
                        int(asp or 0), float(sr or 0.0), float(spd or 0.0))
                series.changes = changes
            # names, for provenance
            for scope, kind in self._series:
                names: Dict[int, str] = {}
                if kind == "realm_items":
                    for kid, nm in con.execute(
                            "SELECT item_id, name FROM tsm_realm_price WHERE scope=?",
                            (scope,)):
                        names[int(kid)] = str(nm or "")
                else:
                    for kid, nm in con.execute(
                            "SELECT key_id, name FROM tsm_region_price"):
                        names[int(kid)] = str(nm or "")
                self._item_names[(scope, kind)] = names
        finally:
            con.close()

    # -- queries ---------------------------------------------------------
    def series(self, scope: str, kind: str) -> Optional[_Series]:
        return self._series.get((scope, kind))

    def state_at(self, scope: str, kind: str, ts: int) -> Dict[int, Tuple]:
        """The market state of ``scope`` as it was known at ``ts``."""
        series = self._series.get((scope, kind))
        if series is None:
            return {}
        snap = series.snapshot_for(ts, self.availability_clock)
        if snap is None:
            return {}
        return series.state_at_snapshot(snap.snapshot_id)

    def snapshot_time_at(self, scope: str, kind: str, ts: int) -> int:
        """When the state used by :meth:`state_at` was itself measured."""
        series = self._series.get((scope, kind))
        if series is None:
            return 0
        snap = series.snapshot_for(ts, self.availability_clock)
        return snap.updated_at if snap else 0

    def snapshot_times(self, scope: str, kind: str) -> List[int]:
        series = self._series.get((scope, kind))
        if series is None:
            return []
        return [s.updated_at for s in series.snapshots]

    def latest_state(self, scope: str, kind: str) -> Dict[int, Tuple]:
        series = self._series.get((scope, kind))
        if series is None or not series.snapshots:
            return {}
        return series.state_at_snapshot(series.snapshots[-1].snapshot_id)

    def item_name(self, scope: str, kind: str, key_id: int) -> str:
        return self._item_names.get((scope, kind), {}).get(int(key_id), "")

    # -- injecting older, complete states ---------------------------------
    def inject_full_states(self, scope: str, kind: str,
                           states: Sequence[Tuple[int, Dict[int, Tuple]]]) -> None:
        """Add complete states that predate the change table.

        The legacy ``market_snapshots`` imports are whole-region states
        from July and October.  They are *complete*, so they are injected
        as a snapshot whose change set is the whole state; the change
        table then continues on top of them in time order.
        """
        series = self._series.get((scope, kind))
        if series is None or not states:
            return
        existing = {s.updated_at for s in series.snapshots}
        next_id = max((s.snapshot_id for s in series.snapshots), default=0) + 1
        added: List[Snapshot] = []
        for ts, state in states:
            if ts in existing:
                continue
            snap = Snapshot(next_id, scope, kind, ts, ts)
            added.append(snap)
            series.changes[next_id] = dict(state)
            next_id += 1
        if added:
            series.snapshots.extend(added)
            series.sort_snapshots()

    # -- realm mapping ---------------------------------------------------
    def canonical_scope(self, realm_name: str) -> str:
        """The TSM scope that holds the auction house of ``realm_name``.

        Realms of one connected group share a single auction house, so TSM
        files them under the group's canonical realm.  Using the member's
        own slug would silently produce *no* data.
        """
        entry = self._canonical.get(str(realm_name))
        if entry:
            return entry[0]
        return self._realm_slug.get(str(realm_name), "")

    def realm_group(self, realm_name: str) -> Tuple[str, str, int]:
        return self._canonical.get(str(realm_name), ("", "", 0))

    def realm_groups(self) -> Dict[str, str]:
        """Every known realm -> the canonical scope of its auction house.

        The dataset ships this map so the addon can name the auction house
        of a realm the account has never traded in.  It is deliberately
        separate from the per-realm records: a realm can have a mapping
        and no estimate, and the absence of an estimate is not hidden.
        """
        return {name: entry[0] for name, entry in self._canonical.items() if entry[0]}


def load_region_history(db_path: Path | str) -> List[Tuple[int, Dict[int, Tuple]]]:
    """The legacy *complete* region states, oldest first.

    ``market_snapshots`` holds a full row for every item for each older
    import, which is the only regional history that reaches back before the
    snapshot service started.
    """
    con = sqlite3.connect(db_path)
    out: List[Tuple[int, Dict[int, Tuple]]] = []
    try:
        snaps = con.execute(
            "SELECT s.snapshotId, s.snapshotTimestamp, s.importFileName "
            "FROM snapshots s JOIN market_snapshots m ON m.snapshotId = s.snapshotId "
            "GROUP BY s.snapshotId ORDER BY s.snapshotTimestamp").fetchall()
        for sid, ts, fname in snaps:
            if "region_items" not in str(fname):
                continue  # the item catalogue import is not a market state
            state: Dict[int, Tuple] = {}
            for kid, mv, hi, asp, sr, spd in con.execute(
                    "SELECT itemId, marketValue, historical, avgSalePrice, "
                    "saleRate, soldPerDay FROM market_snapshots WHERE snapshotId=?",
                    (sid,)):
                state[int(kid)] = (int(mv or 0), 0, 0, int(hi or 0),
                                   int(asp or 0), float(sr or 0.0), float(spd or 0.0))
            out.append((parse_ts(str(ts)), state))
    except sqlite3.OperationalError:
        pass
    finally:
        con.close()
    return out
