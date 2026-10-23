"""The farm ranking that ships inside the dataset.

Why it lives in the dataset at all
----------------------------------
The panel in the game has no database and no HTTP.  Anything it is to show
has to arrive with the file it already loads, so "show the farm ranking in
the panel" and "put the ranking in the dataset" are the same sentence.

Why it is built at export time
------------------------------
Because that is what makes it *always actual*.  The ranking rests on the
prices the model produces; building it in the same run, from the same
engine, means the ranking cannot be a day older than the prices it claims
to be worth.  A separate scheduled job would be a second clock to keep in
step, and the two would eventually disagree -- which is the failure mode
this project has spent the most effort removing.

One price source
----------------
Prices come from :func:`woweconomy.analytics.farm_ranking.resolve_market`,
the single place that decides where a ranking's prices come from.  The
engine is passed *in* rather than loaded here: the export has already paid
for it, and loading it again cost 94 seconds when this was measured.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Sequence

from woweconomy.analytics import farm_ranking as fr

#: Where the zone drop tables live.  Resolved from this file so the module
#: works from any working directory, which the pipeline does not promise.
ZONES_DIR = Path(__file__).resolve().parents[1] / "wowhead_zones"

#: How many rows each leaderboard ships.  The panel is a scroll box read on
#: a second monitor, not a spreadsheet: ten rows is what someone reads.
TOP_STOPS = 10
TOP_MOGS = 10
TOP_MATS = 10

#: Items named under each farm stop.  The point of the section is "go here,
#: pick these up", and three is enough to answer it without turning the
#: block into a copy of the drop tables.
ITEMS_PER_STOP = 3


class FarmRankingUnavailable(RuntimeError):
    """No ranking could be built.  The caller ships an empty block saying why."""


def zone_paths(zone_ids: Optional[Sequence[int]] = None,
               directory: Optional[Path] = None) -> List[Path]:
    """The zone files to rank, newest naming scheme first.

    A missing id is an error rather than a silent skip: a ranking over "the
    zones that happened to exist" is not the ranking that was asked for.
    """
    root = Path(directory or ZONES_DIR)
    if not zone_ids:
        return sorted(root.glob("zone_*.json"))
    paths = []
    for zone_id in zone_ids:
        path = root / f"zone_{int(zone_id)}.json"
        if not path.exists():
            raise FarmRankingUnavailable(f"no zone file for id {zone_id}: {path}")
        paths.append(path)
    return paths


def blizzard_facts(item_ids: Optional[Sequence[int]] = None
                   ) -> Dict[int, fr.BlizzardItem]:
    """Blizzard item facts, from the shared database, or nothing.

    They are what excludes a BoP, junk or era-Classic drop from a farm
    ranking and what gives a vendor value to "you could just sell it".

    Both tables are read, because neither is complete on its own:
    ``db2_items`` is the client's own item table and is the only source of
    ``era`` (an era-Classic row's price comes from a different game), while
    ``blizzard_items`` is the only source of the vendor sell price --
    ItemSparse carries no price.  That is the same split
    :func:`woweconomy.services.db2_items.load_facts` makes, over the shared
    connection rather than a SQLite path.

    A database that is not there returns ``{}`` instead of raising: losing
    the filter degrades the ranking, but it must not fail the export.
    """
    try:
        from woweconomy.dbcompat import connect
    except Exception:  # pragma: no cover - a partial install
        return {}
    try:
        con = connect()
    except Exception:
        return {}

    wanted = None if item_ids is None else {int(i) for i in item_ids}
    if wanted is not None and not wanted:
        return {}

    db2: Dict[int, object] = {}
    try:
        rows = con.execute(
            "SELECT itemid, name, binding, quality, era, itemlevel"
            " FROM db2_items").fetchall()
    except Exception:
        rows = []
    for row in rows:
        try:
            item_id = int(row[0])
        except (TypeError, ValueError):
            continue
        if wanted is not None and item_id not in wanted:
            continue
        db2[item_id] = (str(row[1] or ""),
                        None if row[2] is None else int(row[2]),
                        None if row[3] is None else int(row[3]),
                        int(row[4] or 0), int(row[5] or 0))

    api: Dict[int, tuple] = {}
    try:
        rows = con.execute(
            "SELECT itemid, name, quality, binding, level, sellprice"
            " FROM blizzard_items").fetchall()
    except Exception:
        rows = []
    for row in rows:
        try:
            item_id = int(row[0])
        except (TypeError, ValueError):
            continue
        if wanted is not None and item_id not in wanted:
            continue
        api[item_id] = (str(row[1] or ""), str(row[2] or ""),
                        str(row[3] or ""), int(row[4] or 0), int(row[5] or 0))

    from woweconomy.services.db2_items import BINDING_NAMES, QUALITY_NAMES

    facts: Dict[int, fr.BlizzardItem] = {}
    for item_id in set(db2) | set(api):
        d = db2.get(item_id)
        a = api.get(item_id)
        name = (d[0] if d and d[0] else (a[0] if a else ""))
        quality = (QUALITY_NAMES.get(d[2], "") if d and d[2] is not None
                   else (a[1] if a else ""))
        binding = (BINDING_NAMES.get(d[1], "") if d and d[1] is not None
                   else (a[2] if a else ""))
        facts[item_id] = fr.BlizzardItem(
            item_id, name, quality, binding,
            level=int(d[4] or 0) if d else (a[3] if a else 0),
            sell_price=int(a[4]) if a else 0,
            era=int(d[3] or 0) if d else 0)
    return facts


def _stop(farm: fr.NpcFarm, items_per_stop: int) -> Dict[str, object]:
    """One farm stop, with the items that pay for it named."""
    items = []
    for drop in farm.top_drops[:items_per_stop]:
        if drop.realizable <= 0:
            continue
        # id *and* name, always: a name alone is ambiguous in a game with
        # two "Worn Leather Boots", and an id alone is unreadable.
        items.append({"id": int(drop.item_id),
                      "name": drop.item_name,
                      "rate": round(float(drop.drop_rate), 4),
                      "net": int(round(drop.net_value))})
    return {
        "npc": int(farm.npc_id),
        "name": farm.npc_name,
        "zone": farm.zone_name,
        "ev": int(round(farm.realizable_ev)),
        "gross": int(round(farm.ev_per_kill)),
        "vendor": int(round(farm.vendor_per_kill)),
        "spawns": int(farm.spawns or 0),
        "items": items,
    }


def _price_age_days(engine) -> Optional[int]:
    """How old the regional state the model reads is, in days.

    Shipped once, at the top of the block, rather than as a confidence
    column on every row.  Measured on the live model, the per-row
    confidence is identical across every stop -- it is ``evidence x
    freshness``, and the model prices everything with a sale rate, so the
    evidence term is 1.0 and only the age varies.  A column that repeats
    the same number ten times tells the reader nothing, and its daily
    drift would make every export look like a new dataset.

    ``None`` means the engine could not say, which is reported as unknown
    rather than as fresh.
    """
    if engine is None:
        return None
    try:
        snap_ts = engine.history.snapshot_time_at(
            "region", "region_items",
            int(datetime.now(timezone.utc).timestamp()))
    except Exception:
        return None
    if not snap_ts:
        return None
    age = (datetime.now(timezone.utc).timestamp() - snap_ts) / 86400.0
    return max(int(age), 0)


def build_rankings(*, engine=None, zone_ids: Optional[Sequence[int]] = None,
                   directory: Optional[Path] = None,
                   source: str = "auto", db_path: str = "",
                   csv_path: str = "csv/items.csv",
                   blizz: Optional[Dict[int, fr.BlizzardItem]] = None,
                   top_stops: int = TOP_STOPS, top_mogs: int = TOP_MOGS,
                   top_mats: int = TOP_MATS,
                   items_per_stop: int = ITEMS_PER_STOP) -> Dict[str, object]:
    """Build the ``farmRankings`` block as plain data.

    Raises :class:`FarmRankingUnavailable` when there is nothing to rank --
    no zone files, or a price source that prices none of them.  An empty
    ranking that says "everything is worth nothing" is worse than a ranking
    that says it could not be built.
    """
    paths = zone_paths(zone_ids, directory)
    if not paths:
        raise FarmRankingUnavailable(f"no zone files under {Path(directory or ZONES_DIR)}")
    drops = fr.load_zone_drops(paths)
    if not drops:
        raise FarmRankingUnavailable("the zone files carry no drops")

    names = {d.item_id: d.item_name for d in drops if d.item_name}
    try:
        market_source = fr.resolve_market(
            {d.item_id for d in drops}, names=names, source=source,
            db_path=db_path, csv_path=csv_path, engine=engine)
    except SystemExit as exc:  # resolve_market reports a missing path this way
        raise FarmRankingUnavailable(str(exc) or "no usable price source") from exc
    market = market_source.market
    if not market:
        raise FarmRankingUnavailable(
            f"no prices for these zones ({market_source.label})")

    facts = (blizz if blizz is not None
             else blizzard_facts({d.item_id for d in drops}))
    cfg = fr.WowEconomyConfig.default()
    r_cfg = fr.FarmRankingConfig(price_age_days=_price_age_days(engine))
    farms = fr.score_npcs(drops=drops, market=market, cfg=cfg, blizz=facts,
                          r_cfg=r_cfg)
    # ``top_n`` is advisory here: top_mogs documents that it returns the full
    # ranked list and leaves slicing to the caller.  Slicing is not optional
    # for this caller -- unsliced, 1023 transmog rows went into the dataset.
    mogs = fr.top_mogs(drops=drops, market=market, blizz=facts,
                       r_cfg=r_cfg)[:top_mogs]
    mats = fr.top_mats(drops=drops, market=market, top_n=top_mats,
                       blizz=facts, r_cfg=r_cfg)

    return {
        "generatedAt": int(datetime.now(timezone.utc).timestamp()),
        "source": market_source.label,
        "reason": "",
        "dataAgeDays": _price_age_days(engine),
        "zones": len({d.zone_name for d in drops}),
        "items": len({d.item_id for d in drops}),
        "npcs": len({d.npc_id for d in drops}),
        "stops": [_stop(f, items_per_stop) for f in farms[:top_stops]],
        "mogs": [{"id": int(c.item_id), "name": c.item_name,
                  "price": int(round(c.unit_price)),
                  "drop": round(float(c.drop_rate), 5),
                  # per-listing sell-through: the evidence that the price is
                  # reachable at all.  A 26k listing that never sells is not
                  # a farm, and this is the number that says so.
                  "rate": round(float(c.sale_rate), 4)}
                 for c in mogs],
        "mats": [{"mat": m.mat, "npc": m.npc_name, "zone": m.zone_name,
                  "units": round(float(m.units_per_kill), 3),
                  "ev": int(round(m.ev_per_kill))}
                 for m in mats],
    }


def empty_block(reason: str) -> Dict[str, object]:
    """The block to ship when nothing could be built, carrying the reason."""
    return {
        "generatedAt": int(datetime.now(timezone.utc).timestamp()),
        "source": "", "reason": str(reason)[:300], "dataAgeDays": None,
        "zones": 0, "items": 0, "npcs": 0,
        "stops": [], "mogs": [], "mats": [],
    }


def _q(value: object) -> str:
    """A Lua string literal, escaped the way the dataset escapes them."""
    text = str(value if value is not None else "")
    text = text.replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ")
    return f'"{text}"'


def render(block: Dict[str, object], indent: str = "\t") -> List[str]:
    """The ``farmRankings`` table as dataset lines, one row per line.

    One row per line because this file is committed and diffed: a farm that
    moved up the ranking should be one changed line, not a rewritten
    paragraph.
    """
    lines: List[str] = []
    lines.append(f"{indent}-- what is worth farming right now, from the zone drop tables")
    lines.append(f"{indent}-- and the same model the tooltips use.  Rebuilt with every")
    lines.append(f"{indent}-- export, so it is never older than the prices it rests on.")
    lines.append(f"{indent}farmRankings = {{")
    lines.append(f"{indent}\tgeneratedAt = {int(block.get('generatedAt') or 0)},")
    lines.append(f"{indent}\tsource = {_q(block.get('source'))},")
    reason = str(block.get("reason") or "")
    if reason:
        lines.append(f"{indent}\treason = {_q(reason)},")
    age = block.get("dataAgeDays")
    lines.append(f"{indent}\tdataAgeDays = {int(age) if age is not None else 'nil'},")
    lines.append(f"{indent}\tzones = {int(block.get('zones') or 0)},")
    lines.append(f"{indent}\titems = {int(block.get('items') or 0)},")
    lines.append(f"{indent}\tnpcs = {int(block.get('npcs') or 0)},")

    lines.append(f"{indent}\t-- the stops: realizable copper per kill, and what pays for it")
    lines.append(f"{indent}\tstops = {{")
    for stop in block.get("stops") or []:  # type: ignore[union-attr]
        items = ", ".join(
            f"{{ id = {int(item['id'])}, name = {_q(item['name'])},"
            f" rate = {float(item['rate'])}, net = {int(item['net'])} }}"
            for item in stop.get("items") or [])
        lines.append(
            f"{indent}\t\t{{ npc = {int(stop['npc'])}, name = {_q(stop['name'])},"
            f" zone = {_q(stop['zone'])}, ev = {int(stop['ev'])},"
            f" gross = {int(stop['gross'])}, vendor = {int(stop['vendor'])},"
            f" spawns = {int(stop['spawns'])},"
            f" items = {{ {items} }} }},")
    lines.append(f"{indent}\t}},")

    lines.append(f"{indent}\t-- transmog items that are rare AND sell")
    lines.append(f"{indent}\tmogs = {{")
    for mog in block.get("mogs") or []:  # type: ignore[union-attr]
        lines.append(
            f"{indent}\t\t{{ id = {int(mog['id'])}, name = {_q(mog['name'])},"
            f" price = {int(mog['price'])}, drop = {float(mog['drop'])},"
            f" rate = {float(mog['rate'])} }},")
    lines.append(f"{indent}\t}},")

    lines.append(f"{indent}\t-- material classes by expected value per kill")
    lines.append(f"{indent}\tmats = {{")
    for mat in block.get("mats") or []:  # type: ignore[union-attr]
        lines.append(
            f"{indent}\t\t{{ mat = {_q(mat['mat'])}, npc = {_q(mat['npc'])},"
            f" zone = {_q(mat['zone'])}, units = {float(mat['units'])},"
            f" ev = {int(mat['ev'])} }},")
    lines.append(f"{indent}\t}},")
    lines.append(f"{indent}}},")
    return lines


def summary(block: Dict[str, object]) -> str:
    """One line for the export log."""
    if block.get("reason"):
        return f"farm ranking: not built ({block['reason']})"
    age = block.get("dataAgeDays")
    age_text = "unknown age" if age is None else f"{int(age)} day(s) old"
    return (f"farm ranking: {len(block.get('stops') or [])} stop(s), "
            f"{len(block.get('mogs') or [])} mog(s), "
            f"{len(block.get('mats') or [])} mat class(es) from "
            f"{block.get('npcs')} NPC(s) in {block.get('zones')} zone(s) "
            f"-- {block.get('source')}, data {age_text}")
