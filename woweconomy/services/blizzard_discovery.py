r"""Discover WoW characters via Blizzard Mythic+, PvP, Hall of Fame and seed scan.

Discovery uses collectorproject's vetted harvesters, isolated in their own
SQLite database; its discovered character identities are merged into this
project's `collector` table. Collection ownership and weighted missing demand
are written directly to this project's `dfa.db` with its existing bitmap codec.

Commands (run from C:\\economics):

    python -u -m woweconomy.services.blizzard_discovery discover
    python -u -m woweconomy.services.blizzard_discovery merge
    python -u -m woweconomy.services.blizzard_discovery status
    python -u -m woweconomy.services.blizzard_discovery scan --accounts 5000 --all-realms
    python -u -m woweconomy.services.blizzard_discovery run --accounts 5000 --all-realms

Every answer the API gives is kept in ``--cache-dir`` (one SQLite file), so a
second run asks "has this changed?" instead of "give me this again?".  A 304
costs no body, ``--offline`` answers only from what is already stored, and
``status`` says where the work stopped without a single request.

`discover` needs C:\\collectorproject (or COLLECTOR_PROJECT) for the upstream
harvesters and credentials. `scan` starts with this project's 20,550 DfA names.
The scan's `--accounts` is a total cap when `--all-realms` is present. It is
resumable by the `scanned` rows in dfa.db and stores each successful character
atomically. The profile API only exposes character id, not Battle.net account
id: existing DfA account mappings are retained; otherwise each character uses a
stable character-specific key and is NOT merged with alts.
"""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import importlib
import json
import logging
import os
import sqlite3
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from woweconomy.services import blizzard_collections as BC
from woweconomy.services.dfa_store import (
    DEFAULT_OUT_DIR,
    _now,
    _obtainable_for,
    bump_missing,
    collector_weight,
    open_db,
    slot_index,
    store_owned,
    unpack_owned,
)

logger = logging.getLogger(__name__)
DEFAULT_KINDS = ("mounts", "recipes", "appearances")
DISCOVERY_SOURCES = ("mythic", "pvp", "hall_of_fame")


def _pending_rows(con: sqlite3.Connection, kinds: Sequence[str], score_cut: int = 0):
    """Characters with at least one missing bitmap for the requested kinds."""
    sql = "SELECT c.region,c.realm,c.name,COALESCE(c.score,0),c.account "
    sql += "FROM collector c WHERE NOT EXISTS ("
    sql += "SELECT 1 FROM account a WHERE a.account=c.account AND NOT EXISTS ("
    sql += "SELECT 1 FROM owned o WHERE o.account=a.account AND o.kind IN (" + ",".join("?" for _ in kinds) + "))) "
    params: list[Any] = list(kinds)
    if score_cut:
        sql += " AND COALESCE(c.score,0)>=?"
        params.append(score_cut)
    sql += " ORDER BY COALESCE(c.score,0) DESC,c.realm,c.name"
    out = []
    placeholders = ",".join("?" for _ in kinds)
    for region, realm, name, score, account in con.execute(sql, params):
        out.append((str(region), str(realm), str(name), int(score or 0), account))
    return out


def _account_key(con: sqlite3.Connection, region: str, realm: str,
                 name: str, character_id: Any) -> str:
    row = con.execute(
        "SELECT account FROM collector WHERE UPPER(region)=? AND LOWER(realm)=LOWER(?) "
        "AND LOWER(name)=LOWER(?)", (region, realm, name)).fetchone()
    if row and row[0]:
        return str(row[0])
    if character_id not in (None, ""):
        existing = con.execute(
            "SELECT account FROM account WHERE region=? AND LOWER(realm)=LOWER(?) "
            "AND LOWER(name)=LOWER(?)", (region, realm, name),
        ).fetchone()
        if existing:
            return str(existing[0])
        return f"blizzard:{region.casefold()}:{int(character_id)}"
    return f"blizzard:{region.casefold()}:{BC.realm_slug(realm)}:{name.casefold()}"


def _store_result(con: sqlite3.Connection, row: Mapping[str, Any], score: int,
                  kinds: Sequence[str], obtainable: Mapping[str, set[int]],
                  slots: Mapping[str, tuple[dict[int, int], list[int]]]) -> bool:
    """Commit complete kinds once; a hidden/missing route never becomes empty."""
    if row.get("outcome") != "OK":
        return False
    statuses = row.get("statuses") or {}
    owned = row.get("owned") or {}
    complete = [kind for kind in kinds if statuses.get(kind) == 200]
    if not complete:
        return False
    region = str(row.get("region") or "EU").upper()
    realm = str(row.get("realm") or "")
    name = str(row.get("name") or "")
    account = _account_key(con, region, realm, name, row.get("account_id"))
    weight = collector_weight(score)
    # Snapshot the old bitmaps against this run's slot map before assigning any
    # new slots (the catalogue may have expanded since the last fetch).
    old_owned: dict[str, set[int]] = {}
    for kind in complete:
        previous = con.execute("SELECT bits FROM owned WHERE kind=? AND account=?",
                               (kind, account)).fetchone()
        if previous:
            old_owned[kind] = unpack_owned(previous[0], slots[kind][1])

    con.execute("BEGIN")
    try:
        for kind in complete:
            own = set(int(i) for i in owned.get(kind, ())) & obtainable[kind]
            old = old_owned.get(kind)
            scanned = con.execute(
                "SELECT weight FROM scanned WHERE kind=? AND account=?", (kind, account)
            ).fetchone()
            if old is None:
                # Legacy DfA scans already contributed demand votes, but discarded
                # the ownership bitmap. Backfill owned only; do not double-count.
                if scanned is None:
                    bump_missing(con, kind, obtainable[kind] - own, weight, region, realm)
            else:
                # Replacing a bitmap: undo its old miss vote with the stored
                # historical weight, then record the new set and weight.
                old_weight = float(scanned[0]) if scanned and scanned[0] is not None else weight
                old_missing = obtainable[kind] - old
                for item_id in old_missing:
                    con.execute("UPDATE missing SET n=MAX(0,n-1),w=MAX(0,w-?) "
                                "WHERE kind=? AND item_id=?", (old_weight, kind, item_id))
                    con.execute("UPDATE missing_realm SET n=MAX(0,n-1),w=MAX(0,w-?) "
                                "WHERE kind=? AND region=? AND realm=? AND item_id=?",
                                (old_weight, kind, region, realm, item_id))
                bump_missing(con, kind, obtainable[kind] - own, weight, region, realm)
            index, by_slot = slots[kind]
            store_owned(con, kind, account, own, index, len(by_slot), src="blizzard")
            con.execute(
                "INSERT INTO scanned(kind,account,scanned_at,weight,by_realm) VALUES(?,?,?,?,1) "
                "ON CONFLICT(kind,account) DO UPDATE SET scanned_at=excluded.scanned_at,"
                "weight=excluded.weight,by_realm=1",
                (kind, account, _now(), weight),
            )
        con.execute(
            "UPDATE collector SET account=COALESCE(account,?),probed_at=? "
            "WHERE UPPER(region)=? AND LOWER(realm)=LOWER(?) AND LOWER(name)=LOWER(?)",
            (account, _now(), region, realm, name),
        )
        con.execute(
            "INSERT INTO account(account,region,realm,name,scanned_at,characters,board) "
            "VALUES(?,?,?,?,?,1,'blizzard') ON CONFLICT(account) DO UPDATE SET "
            "scanned_at=COALESCE(account.scanned_at,excluded.scanned_at)",
            (account, region, realm, name, _now()),
        )
        con.commit()
    except Exception:
        con.rollback()
        raise
    return True


def _merge_discovered(con: sqlite3.Connection, source: sqlite3.Connection) -> int:
    """Copy the discovered identities into this project's ``collector``.

    One statement for the whole set.  The first version issued an ``UPDATE``
    per character with ``UPPER(region)``/``LOWER(realm)``/``LOWER(name)`` in
    the predicate, which SQLite can only answer with a full table scan: on
    the ~900k characters a PvP harvest produces that is quadratic and runs
    for a day while holding the database's write lock.  The account key is
    character-specific and is set in the insert itself, so the extra update
    bought nothing.
    """
    count = 0

    def rows():
        nonlocal count
        cursor = source.execute(
            "SELECT region,realm_slug,character_name,character_id FROM characters "
            "WHERE realm_slug IS NOT NULL AND character_name IS NOT NULL")
        for region, realm, name, char_id in cursor:
            count += 1
            account = (f"blizzard:{str(region).casefold()}:{int(char_id)}"
                       if char_id is not None else None)
            yield (str(region).upper(), str(realm), str(name), account)

    con.executemany(
        "INSERT INTO collector(region,realm,name,score,board,probed_at,account) "
        "VALUES(?,?,?,0,'blizzard_discovery',NULL,?) "
        "ON CONFLICT(region,realm,name) DO UPDATE SET "
        "account=COALESCE(collector.account,excluded.account)",
        rows(),
    )
    con.commit()
    return count


def _discover_source_root() -> Path:
    root = Path(os.environ.get("COLLECTOR_PROJECT", r"C:/collectorproject"))
    if not (root / "wow_discovery.py").is_file():
        raise RuntimeError(f"não encontrei wow_discovery.py em {root}")
    return root


class _CachedSourceClient:
    """The collectorproject client, with every answer kept on disk.

    Discovery talks to Blizzard through another project's client, so this wraps
    it instead of copying it: the harvesters only ever call ``get_json``.  A
    rerun of a harvest already done then costs nothing -- the PvP walk alone is
    1372 leaderboard responses -- while the leaderboards are still asked again
    once ``--cache-ttl`` has expired.  There is no ``If-None-Match`` here (that
    client does not take extra headers), so the TTL, not a 304, is the lever.
    """

    def __init__(self, inner: Any, cache: Any = None, ttl_seconds: float = 0.0,
                 offline: bool = False) -> None:
        self.inner = inner
        self.cache = cache
        self.ttl_seconds = ttl_seconds
        self.offline = offline
        self.hits = 0
        self.stored = 0

    async def __aenter__(self) -> "_CachedSourceClient":
        await self.inner.__aenter__()
        return self

    async def __aexit__(self, *exc: object) -> Any:
        return await self.inner.__aexit__(*exc)

    def report(self) -> dict[str, int]:
        return {"cache_hits": self.hits, "cache_stores": self.stored}

    async def get_json(
        self,
        path: str,
        params: Mapping[str, Any] | None = None,
    ) -> Mapping[str, Any]:
        url = (path if path.startswith("https://")
               else f"{self.inner.region_config.api_url}{path}")
        if self.cache is not None:
            entry = self.cache.get(url, params)
            if entry is not None and (self.offline or
                                      self.cache.is_fresh(entry, self.ttl_seconds)):
                self.hits += 1
                return json.loads(entry.raw_text)
            if self.offline:
                raise RuntimeError(f"sem cache para {url} (modo offline)")
        result = await self.inner.get_json_with_meta(path, params)
        if self.cache is not None and result.raw_text:
            self.cache.put(url, params, status=result.http_status,
                           raw_text=result.raw_text)
            self.stored += 1
        return result.payload


def _discover(args, con: sqlite3.Connection) -> dict[str, Any]:
    """Run the collectorproject harvesters in their own persistent store."""
    source_root = _discover_source_root()
    sys.path.insert(0, str(source_root))
    store = None
    cache = None
    cache_dir = getattr(args, "cache_dir", None)
    if cache_dir:
        from .http_cache import ResponseCache
        cache = ResponseCache(Path(cache_dir) / "blizzard.sqlite")
    previous_env = {key: os.environ.get(key) for key in
                    ("BLIZZARD_CLIENT_ID", "BLIZZARD_CLIENT_SECRET")}
    modules = ("project_store", "wow_blizzard_client", "wow_realm_db",
               "wow_discovery", "wow_mythic_keystone", "wow_pvp_harvest",
               "wow_hall_of_fame", "wow_blizzard_data")
    try:
        for name in modules:
            sys.modules.pop(name, None)
        client_id, client_secret = BC.load_credentials(args.client_id, args.client_secret)
        os.environ["BLIZZARD_CLIENT_ID"] = client_id
        os.environ["BLIZZARD_CLIENT_SECRET"] = client_secret
        source_store = importlib.import_module("project_store")
        source_client = importlib.import_module("wow_blizzard_client").AsyncBlizzardClient
        realm_db = importlib.import_module("wow_realm_db")
        discovery = importlib.import_module("wow_discovery")
        realm_db.REGION = args.region
        factions = [s.strip() for s in args.hall_of_fame_factions.split(",") if s.strip()]
        store = source_store.ProjectDataStore(args.discovery_store)
        report: dict[str, Any] = {}

        wrapped = _CachedSourceClient(
            source_client(region=args.region, concurrency=args.concurrency),
            cache=cache,
            ttl_seconds=float(getattr(args, "cache_ttl", 0.0)) * 3600.0,
            offline=bool(getattr(args, "offline", False)),
        )

        async def run() -> None:
            async with wrapped as client:
                for source in args.sources:
                    if source == "mythic":
                        report[source] = await discovery.run_mythic_plus(
                            store, client, concurrency=args.concurrency,
                            max_realms=args.max_realms,
                            max_leaderboards=args.max_leaderboards,
                            season_id=args.mythic_season_id, verbose=args.verbose)
                    elif source == "pvp":
                        report[source] = await discovery.run_pvp(
                            store, client, concurrency=args.concurrency,
                            max_seasons=args.max_pvp_seasons,
                            max_brackets=args.max_brackets,
                            season_id=args.pvp_season_id, verbose=args.verbose)
                    elif source == "hall_of_fame":
                        report[source] = await discovery.run_hall_of_fame(
                            store, client, concurrency=args.concurrency,
                            max_raids=args.max_raids,
                            raid_slug=args.hall_of_fame_raid,
                            factions=factions, verbose=args.verbose)

        asyncio.run(run())
        report["characters_merged"] = _merge_discovered(con, store.connection)
        report.update(wrapped.report())
        return report
    finally:
        for key, value in previous_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        if store is not None:
            store.close()
        if cache is not None:
            cache.close()
        with contextlib.suppress(ValueError):
            sys.path.remove(str(source_root))
        for name in modules:
            sys.modules.pop(name, None)


async def _scan(args, con: sqlite3.Connection, limit: int = 0) -> dict[str, Any]:
    kinds = tuple(k.strip() for k in args.kinds.split(",") if k.strip())
    if not kinds or any(k not in BC.ROUTES for k in kinds):
        raise ValueError(f"kinds inválidos: {kinds}")
    obtainable = {k: _obtainable_for(con, args.out_dir, k) for k in kinds}
    absent = [k for k in kinds if not obtainable[k]]
    if absent:
        # Two different fixes, and the DfA one is a dead end for the kinds it
        # never served: its feed has mounts, recipes and appearances only.
        indexed = [k for k in absent if k in BC.UNIVERSE_ROUTES]
        dfa = [k for k in absent if k not in BC.UNIVERSE_ROUTES]
        hint = []
        if indexed:
            hint.append("catalogue --universe-kinds " + ",".join(indexed))
        if dfa:
            hint.append("catalogue para " + ",".join(dfa)
                        + " (a API não publica índice destes tipos)")
        raise RuntimeError("falta catalogue para " + ", ".join(absent) +
                           "; corra: " + " e ".join(hint))
    slots = {k: slot_index(con, k, obtainable[k]) for k in kinds}
    rows = _pending_rows(con, kinds, args.min_score)
    # Counts are account-wide; the first character already stored for an account
    # covers its alts. For unknown mappings the character key becomes the account
    # key after first fetch, so filtering within this batch prevents duplicate calls.
    seen_accounts: set[str] = set()
    selected_rows = []
    for row in rows:
        account = row[4]
        if account and account in seen_accounts:
            continue
        selected_rows.append(row)
        if account:
            seen_accounts.add(account)
    rows = selected_rows
    if not args.all_realms:
        selected: list[Any] = []
        counts: dict[str, int] = {}
        for row in rows:
            if counts.get(row[1], 0) < args.accounts:
                selected.append(row)
                counts[row[1]] = counts.get(row[1], 0) + 1
        rows = selected
    elif args.accounts > 0:
        rows = rows[:args.accounts]
    if limit > 0:
        rows = rows[:limit]
    logger.warning("scan candidates: %d", len(rows))

    client = BC._client(args)
    fetcher = BC.CollectionFetcher(client, BC._pacer(args), args.region, kinds)
    counters = {"stored": 0, "not_found": 0, "errors": 0}
    async with client:
        for i, (_region, realm, name, score, _account) in enumerate(rows, 1):
            result = await fetcher.character(realm, name)
            if result["outcome"] == "NOT_FOUND":
                counters["not_found"] += 1
            elif result["outcome"] != "OK":
                counters["errors"] += 1
            elif _store_result(con, result, score, kinds, obtainable, slots):
                counters["stored"] += 1
            if i % 25 == 0:
                print(f"{i}/{len(rows)} stored={counters['stored']} "
                      f"missing={counters['not_found']} errors={counters['errors']}", flush=True)
    return {**fetcher.stats.report(fetcher.pacer), **counters, "kinds": list(kinds)}


def cmd_discover(args) -> int:
    con = open_db(args.out_dir / "dfa.db")
    try:
        args.sources = [s.strip() for s in args.sources.split(",") if s.strip()]
        bad = set(args.sources) - set(DISCOVERY_SOURCES)
        if bad:
            raise ValueError(f"sources inválidas: {', '.join(sorted(bad))}")
        print(json.dumps(_discover(args, con), indent=2))
        return 0
    except Exception as exc:
        logger.exception("discovery falhou")
        print(f"error: {exc}", file=sys.stderr)
        return 4
    finally:
        con.close()


def _pending_count(con: sqlite3.Connection, kinds: Sequence[str]) -> int:
    """How many characters still have a kind their account never covered."""
    placeholders = ",".join("?" for _ in kinds)
    sql = ("SELECT COUNT(1) FROM collector c WHERE NOT EXISTS ("
           "SELECT 1 FROM account a WHERE a.account=c.account AND NOT EXISTS ("
           "SELECT 1 FROM owned o WHERE o.account=a.account AND o.kind IN ("
           + placeholders + ")))")
    return int(con.execute(sql, list(kinds)).fetchone()[0])


def cmd_status(args) -> int:
    """Where the work stopped: no request, no guess."""
    kinds = tuple(k.strip() for k in args.kinds.split(",") if k.strip())
    path = Path(args.out_dir) / "dfa.db"
    if not path.is_file():
        print(f"error: não encontrei {path}", file=sys.stderr)
        return 2
    # read-only on purpose: a report must not migrate, write, or wait behind
    # the scan it is reporting on
    con = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    try:
        total = int(con.execute("SELECT COUNT(1) FROM collector").fetchone()[0])
        boards = {str(board or "?"): int(count) for board, count in con.execute(
            "SELECT board, COUNT(1) FROM collector GROUP BY 1 ORDER BY 2 DESC")}
        accounts = int(con.execute("SELECT COUNT(1) FROM account").fetchone()[0])
        scanned = {kind: int(con.execute(
            "SELECT COUNT(1) FROM scanned WHERE kind=?", (kind,)).fetchone()[0])
            for kind in kinds}
        last_scanned = con.execute("SELECT MAX(scanned_at) FROM scanned").fetchone()[0]
        pending = _pending_count(con, kinds)
    finally:
        con.close()

    cache_block = None
    cache_path = Path(args.cache_dir) / "blizzard.sqlite"
    if cache_path.is_file():
        from .http_cache import ResponseCache
        with ResponseCache(cache_path) as cache:
            cache_block = cache.summary()
    print(json.dumps({
        "collector": total,
        "by_board": boards,
        "accounts_scanned": accounts,
        "scanned": scanned,
        "last_scanned_at": last_scanned,
        "pending_characters": pending,
        "cache": cache_block,
    }, indent=2, default=str))
    return 0


def cmd_merge(args) -> int:
    """Merge what discovery already found, without a single API request.

    The harvesters write to ``discovery_store.sqlite``; this copies those
    identities into this project's ``collector`` so the scan has work to do,
    even when the discovery run itself was interrupted.
    """
    path = Path(args.discovery_store)
    if not path.is_file():
        print(f"error: não encontrei {path}", file=sys.stderr)
        return 2
    con = open_db(args.out_dir / "dfa.db")
    try:
        source = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
        try:
            merged = _merge_discovered(con, source)
        finally:
            source.close()
        print(json.dumps({"characters_merged": merged}, indent=2))
        return 0
    finally:
        con.close()


def cmd_catalogue(args) -> int:
    """Seed ``catalogue`` for a kind the DfA feed never served (pets, toys).

    ``scan`` refuses a kind with an empty catalogue, because `obtainable` is what
    gives a bitmap room for it.  For pets and toys the official index *is* the
    catalogue: one request, cached, and then `--kinds pets` means something.
    The index says nothing about availability, so everything is seeded as
    obtainable; the `owned` counts are what tell a retired pet apart later.
    """
    kinds = [k.strip() for k in args.universe_kinds.split(",") if k.strip()]
    unknown = [k for k in kinds if k not in BC.UNIVERSE_ROUTES]
    if unknown:
        print(f"error: sem índice para: {', '.join(unknown)}", file=sys.stderr)
        return 2
    path = args.out_dir / "dfa.db"
    if path.is_file():
        # Not `open_db`: this only adds catalogue rows, and a migration behind a
        # scan already holding the write lock is how the WAL filled up once.
        con = sqlite3.connect(path, timeout=60)
        con.execute("PRAGMA busy_timeout=60000")
    else:
        con = open_db(path)
    try:
        client = BC._client(args)

        async def run() -> dict[str, list[dict[str, Any]]]:
            async with client:
                fetcher = BC.CollectionFetcher(client, BC._pacer(args), args.region)
                return await BC.fetch_universe(fetcher, kinds)

        data = asyncio.run(run())
        report: dict[str, Any] = {}
        for kind, entries in data.items():
            con.executemany(
                "INSERT INTO catalogue(kind, item_id, name, source, faction, obtainable) "
                "VALUES(?,?,?,'blizzard index',NULL,1) "
                "ON CONFLICT(kind, item_id) DO UPDATE SET "
                "name=excluded.name WHERE catalogue.name IS NULL OR catalogue.name=''",
                [(kind, e["id"], str(e.get("name") or "")) for e in entries])
            con.commit()
            report[kind] = int(con.execute(
                "SELECT COUNT(1) FROM catalogue WHERE kind=?", (kind,)).fetchone()[0])
        print(json.dumps(report, indent=2))
        return 0
    finally:
        con.close()


def cmd_scan(args) -> int:
    con = open_db(args.out_dir / "dfa.db")
    try:
        result = asyncio.run(_scan(args, con, args.limit))
        print(json.dumps(result, indent=2))
        return 0 if not result["errors"] else 4
    except (RuntimeError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    finally:
        con.close()


def cmd_run(args) -> int:
    if not args.no_discovery:
        rc = cmd_discover(args)
        if rc:
            return rc
    return cmd_scan(args)


def _parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--region", default=argparse.SUPPRESS, choices=sorted(BC.REGION_CONFIGS))
    common.add_argument("--locale", default=argparse.SUPPRESS)
    common.add_argument("--client-id", default=argparse.SUPPRESS)
    common.add_argument("--client-secret", default=argparse.SUPPRESS)
    common.add_argument("--concurrency", type=int, default=argparse.SUPPRESS)
    common.add_argument("--rate", type=float, default=argparse.SUPPRESS)
    common.add_argument("--sustained-rate", type=float, default=argparse.SUPPRESS)
    common.add_argument("--out-dir", type=Path, default=argparse.SUPPRESS)
    common.add_argument("--kinds", default=argparse.SUPPRESS)
    common.add_argument("--cache-dir", type=Path, default=argparse.SUPPRESS)
    common.add_argument("--cache-ttl", type=float, default=argparse.SUPPRESS,
                        help="horas em que uma resposta guardada é servida sem "
                             "perguntar; 0 (default) revalida sempre")
    common.add_argument("--offline", action="store_true", default=argparse.SUPPRESS,
                        help="responder só a partir da cache, sem rede")
    common.add_argument("--accounts", type=int, default=argparse.SUPPRESS)
    common.add_argument("--all-realms", action="store_true", default=argparse.SUPPRESS)
    common.add_argument("--min-score", type=int, default=argparse.SUPPRESS)
    common.add_argument("--limit", type=int, default=argparse.SUPPRESS)
    common.add_argument("--no-discovery", action="store_true", default=argparse.SUPPRESS)
    common.add_argument("--sources", default=argparse.SUPPRESS)
    common.add_argument("--discovery-store", type=Path, default=argparse.SUPPRESS)
    common.add_argument("--max-realms", type=int, default=argparse.SUPPRESS)
    common.add_argument("--max-leaderboards", type=int, default=argparse.SUPPRESS)
    common.add_argument("--mythic-season-id", type=int, default=argparse.SUPPRESS)
    common.add_argument("--max-pvp-seasons", type=int, default=argparse.SUPPRESS)
    common.add_argument("--max-brackets", type=int, default=argparse.SUPPRESS)
    common.add_argument("--pvp-season-id", type=int, default=argparse.SUPPRESS)
    common.add_argument("--max-raids", type=int, default=argparse.SUPPRESS)
    common.add_argument("--hall-of-fame-raid", default=argparse.SUPPRESS)
    common.add_argument("--hall-of-fame-factions", default=argparse.SUPPRESS)
    common.add_argument("-v", "--verbose", action="count", default=argparse.SUPPRESS)
    parser = argparse.ArgumentParser(prog="blizzard_discovery", parents=[common])
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("discover", parents=[common], help="find characters via Blizzard sources").set_defaults(func=cmd_discover)
    sub.add_parser("status", parents=[common],
                   help="where the work stopped, no API calls").set_defaults(func=cmd_status)
    sub.add_parser("merge", parents=[common],
                   help="merge the discovered identities, no API calls").set_defaults(func=cmd_merge)
    cat = sub.add_parser("catalogue", parents=[common],
                         help="seed catalogue for pets/toys from the official index")
    cat.add_argument("--universe-kinds", dest="universe_kinds", default="pets,toys")
    cat.set_defaults(func=cmd_catalogue)
    sub.add_parser("scan", parents=[common], help="fetch collections into dfa.db").set_defaults(func=cmd_scan)
    sub.add_parser("run", parents=[common], help="discover, then fetch collections").set_defaults(func=cmd_run)
    parser.set_defaults(region="eu", locale="en_GB", client_id=None,
                        client_secret=None, concurrency=8, rate=100.0,
                        sustained_rate=10.0, out_dir=DEFAULT_OUT_DIR,
                        cache_dir=Path("woweconomy/dfa/cache"), cache_ttl=0.0,
                        offline=False,
                        kinds=",".join(DEFAULT_KINDS), accounts=5000,
                        all_realms=False, min_score=0, limit=0,
                        no_discovery=False, sources=",".join(DISCOVERY_SOURCES),
                        discovery_store=Path("woweconomy/dfa/discovery_store.sqlite"),
                        max_realms=None, max_leaderboards=None,
                        mythic_season_id=None, max_pvp_seasons=None,
                        max_brackets=None, pvp_season_id=None, max_raids=None,
                        hall_of_fame_raid=None,
                        hall_of_fame_factions="alliance,horde", verbose=0)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    logging.basicConfig(level=logging.INFO if args.verbose else logging.WARNING,
                        format="%(asctime)s %(levelname)s %(message)s")
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
