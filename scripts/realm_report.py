"""Per-realm scorecard: is this realm worth posting on, and why.

"The market is dead" is not answerable from prices alone.  A realm can be
expensive and hopeless (nothing sells) or cheap and fine (everything moves).
This puts four structural facts side by side for each realm, and is explicit
about which of them are realm-specific and which are not:

  churn        share of items whose cheapest listing *moved* between the first
               and last Blizzard scan of the window, and the share that left
               the shelf entirely.  This is the only sale-adjacent signal that
               is genuinely per realm, because it comes from our own scans.
  competition  median listings per item in the latest scan: how many sellers
               share the shelf.
  undercut     gap between the cheapest and the second cheapest listing for the
               same item.  A 1% gap is a knife fight; 15% means you can post
               and be seen.
  price level  median market value, and ``recent`` vs ``historical`` -- whether
               the realm is getting cheaper.

Liquidity is **not** per realm, and pretending otherwise was the first version's
mistake.  TSM publishes ``saleRate``/``soldPerDay`` only in the region-level
files (``region.commodities`` / ``region.region_items``); the per-realm files
carry ``itemId,name,marketValue,minBuyout,recent,historical,updatedAt`` and
nothing else.  Measured: 0 of 780,556 per-realm change rows carry a non-zero
``sold_per_day``, against 44,128 of 131,617 region rows.  So the regional rate is
reported once, as a baseline, and never attributed to a realm.

Nothing here writes: every query is read-only.

    python scripts/realm_report.py
    python scripts/realm_report.py --realms silvermoon,draenor,kazzak
    python scripts/realm_report.py --realms kazzak --hours 48
    python scripts/realm_report.py --csv reports/realm_report.csv

The score is a ranking aid, not a verdict: a weighted percentile of the columns
printed beside it, all of which are shown so the ranking can be argued with.
Weights are 0.35 churn, 0.25 undercut, 0.25 competition, 0.15 price level.
"""

from __future__ import annotations

import argparse
import csv
import statistics
import sys
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from woweconomy.dbcompat import connect, load_env          # noqa: E402

GOLD = 10_000


def slug(name: str) -> str:
    """The TSM scope for a realm name: lowercase, alphanumerics only."""
    return "".join(ch for ch in name.lower() if ch.isalnum())


def median(values: List[float]) -> Optional[float]:
    values = [v for v in values if v is not None]
    return statistics.median(values) if values else None


# ---------------------------------------------------------------------------
# the region baseline (shared, never attributed to a realm)
# ---------------------------------------------------------------------------

def region_liquidity(con) -> dict:
    """The regional sale rate: the only liquidity the feed actually carries."""
    rows = con.execute(
        "SELECT key_id, sale_rate, sold_per_day FROM tsm_price_change"
        " WHERE scope='region' ORDER BY key_id, snapshot_id DESC").fetchall()
    seen: Dict[int, Tuple[Optional[float], Optional[float]]] = {}
    for key_id, sale_rate, sold_per_day in rows:
        if key_id not in seen:
            seen[key_id] = (sale_rate, sold_per_day)
    sold = [float(v) for _r, v in seen.values() if v is not None]
    rates = [float(v) for v, _s in seen.values() if v is not None]
    moving = [v for v in sold if v > 0]
    return {
        "region_items": len(seen),
        # The median over *everything* is 0: half the catalogue never sells.
        # That is true and useless as a benchmark, so the baseline reported is
        # the median among the items that do sell, next to the share that do.
        "region_sold_per_day": median(moving),
        "region_sale_rate": median([v for v in rates if v > 0]),
        "region_moving": (len(moving) / len(sold)) if sold else None,
    }


# ---------------------------------------------------------------------------
# the realm's own shelf
# ---------------------------------------------------------------------------

def scans(con, realm: str, scope: str, hours: int) -> List[Tuple[str, int]]:
    """The realm's snapshots inside the window, oldest first."""
    cutoff = int(time.time()) - hours * 3600
    rows = con.execute(
        "SELECT snapshot_id, captured_at FROM ah_snapshot"
        " WHERE (lower(realm)=? OR lower(realm_slug)=?) AND captured_at>=?"
        " ORDER BY captured_at", (realm.lower(), scope, cutoff)).fetchall()
    return [(str(r[0]), int(r[1])) for r in rows]


def cheapest_by_item(con, snapshot: str) -> Dict[int, int]:
    rows = con.execute(
        "SELECT item_id, MIN(unit_price_copper) FROM ah_auction"
        " WHERE snapshot_id=? AND unit_price_copper IS NOT NULL"
        " GROUP BY item_id", (snapshot,)).fetchall()
    return {int(r[0]): int(r[1]) for r in rows}


def shelf(con, realm: str, scope: str, hours: int) -> dict:
    """Depth, undercut, value and churn, from the realm's own scans."""
    window = scans(con, realm, scope, hours)
    if not window:
        return {"snapshot": None, "scans": 0}
    latest = window[-1][0]
    first = window[0][0]

    rows = con.execute(
        "SELECT item_id, unit_price_copper, total_price_copper, is_commodity"
        " FROM ah_auction WHERE snapshot_id=? AND unit_price_copper IS NOT NULL"
        " ORDER BY item_id, unit_price_copper", (latest,)).fetchall()

    per_item: Dict[int, List[int]] = {}
    gear: Dict[int, List[int]] = {}
    value = 0
    commodities = 0
    for item_id, unit, total, is_commodity in rows:
        per_item.setdefault(int(item_id), []).append(int(unit))
        if not is_commodity:
            # Undercut is measured on gear only.  Commodities are posted as
            # stacks at a single unit price by convention, so their cheapest
            # two listings are usually identical and a 0% gap there says
            # nothing about the shelf.
            gear.setdefault(int(item_id), []).append(int(unit))
        value += int(total or 0)
        if is_commodity:
            commodities += 1

    depths = [len(v) for v in per_item.values()]
    gaps = sorted((p[1] - p[0]) / p[0]
                  for p in gear.values() if len(p) >= 2 and p[0] > 0)
    p75 = gaps[int(len(gaps) * 0.75)] if gaps else None
    # The share of items whose two cheapest listings are at the *identical*
    # price.  On a healthy shelf the second listing is a little above the
    # first; when this is high the book is a wall of copies and the only way
    # to win is to be first, not to be cheaper.
    paired = [p for p in gear.values() if len(p) >= 2]
    identical = (sum(1 for p in paired if p[0] == p[1]) / len(paired)
                 if paired else None)

    result = {
        "snapshot": latest,
        "scans": len(window),
        "window_h": round((window[-1][1] - window[0][1]) / 3600, 1),
        "listings": len(rows),
        "items": len(per_item),
        "depth": median(depths),
        "single_share": (sum(1 for d in depths if d == 1) / len(depths)) if depths else None,
        "undercut": median(gaps),
        "undercut_p75": p75,
        "identical": identical,
        "value": value,
        "commodity_share": (commodities / len(rows)) if rows else None,
    }

    if len(window) >= 2:
        before = cheapest_by_item(con, first)
        after = cheapest_by_item(con, latest)
        both = [k for k in before if k in after]
        result["churn"] = (sum(1 for k in both if before[k] != after[k]) / len(both)
                           if both else None)
        result["gone"] = (sum(1 for k in before if k not in after) / len(before)
                          if before else None)
        result["new"] = (sum(1 for k in after if k not in before) / len(after)
                         if after else None)
    return result


def price_level(con, scope: str) -> dict:
    """Current state per item: market value, and how recent compares to 60d."""
    rows = con.execute(
        "SELECT market_value, recent, historical FROM tsm_realm_price"
        " WHERE scope=?", (scope,)).fetchall()
    mv = [float(r[0]) for r in rows if r[0] is not None]
    ratios = [float(recent) / float(historical)
              for _mv, recent, historical in rows
              if recent and historical and historical > 0]
    return {"market_value": median(mv), "trend": median(ratios)}


# ---------------------------------------------------------------------------
# ranking
# ---------------------------------------------------------------------------

WEIGHTS = {"churn": 0.35, "undercut": 0.25,
           "competition": 0.25, "price": 0.15}


def percentile(values: List[Optional[float]], value: Optional[float],
               higher_is_better: bool = True) -> float:
    """Where ``value`` sits in ``values``, 0..1.  None ranks worst."""
    known = [v for v in values if v is not None]
    if value is None or len(known) < 2:
        return 0.0
    share = sum(1 for v in known if v < value) / (len(known) - 1)
    return share if higher_is_better else 1.0 - share


def build(realms: List[str], hours: int = 24) -> Tuple[List[dict], dict]:
    con = connect(None)
    try:
        baseline = region_liquidity(con)
        rows = []
        for realm in realms:
            scope = slug(realm)
            entry = {"realm": realm, "scope": scope}
            entry.update(price_level(con, scope))
            entry.update(shelf(con, realm, scope, hours))
            rows.append(entry)
    finally:
        try:
            con.close()
        except Exception:
            pass

    pool_churn = [r.get("churn") for r in rows]
    pool_undercut = [r.get("undercut") for r in rows]
    pool_depth = [r.get("depth") for r in rows]
    pool_price = [r.get("market_value") for r in rows]

    for row in rows:
        row["p_churn"] = percentile(pool_churn, row.get("churn"))
        row["p_undercut"] = percentile(pool_undercut, row.get("undercut"))
        row["p_competition"] = percentile(pool_depth, row.get("depth"),
                                          higher_is_better=False)
        row["p_price"] = percentile(pool_price, row.get("market_value"))
        row["score"] = (WEIGHTS["churn"] * row["p_churn"]
                        + WEIGHTS["undercut"] * row["p_undercut"]
                        + WEIGHTS["competition"] * row["p_competition"]
                        + WEIGHTS["price"] * row["p_price"])
    rows.sort(key=lambda r: r["score"], reverse=True)
    return rows, baseline


def verdict(row: dict) -> str:
    """One sentence an operator can act on, from the numbers above it."""
    if not row.get("items"):
        return "no data -- the scan never reached this realm"
    bits = []
    churn = row.get("churn")
    if churn is not None:
        if churn < 0.10:
            bits.append(f"prices barely move ({churn:.0%} of items repriced)")
        elif churn > 0.35:
            bits.append(f"prices moving hard ({churn:.0%} repriced)")
    gone = row.get("gone")
    if gone is not None and gone > 0.15:
        bits.append(f"{gone:.0%} of the shelf left the book")
    same = row.get("identical")
    under = row.get("undercut")
    if same is not None and same > 0.5:
        bits.append(f"price wall: {same:.0%} of items have the two cheapest"
                    " listings at the same copper")
    elif under is not None:
        if under < 0.02:
            bits.append(f"undercut knife-fight ({under:.1%} gap)")
        elif under > 0.10:
            bits.append(f"room to post ({under:.0%} gap)")
    depth = row.get("depth")
    if depth is not None and depth >= 6:
        bits.append(f"{depth:.0f} listings per item on average")
    trend = row.get("trend")
    if trend is not None and trend < 0.9:
        bits.append(f"prices down {(1 - trend):.0%} vs the 60-day line")
    return "; ".join(bits) or "nothing stands out"


def fmt_gold(copper: Optional[float]) -> str:
    return "-" if copper is None else f"{copper / GOLD:,.0f}g"


def fmt(value: Optional[float], pattern: str) -> str:
    return "-" if value is None else format(value, pattern)


def report(rows: List[dict], baseline: dict) -> str:
    head = (f"{'realm':<14}{'score':>6} {'items':>7} {'churn':>7} {'gone':>6}"
            f" {'depth':>6} {'same$':>6} {'undercut':>9} {'mv':>8} {'trend':>6}"
            f" {'scans':>6}")
    lines = [head, "-" * len(head)]
    for row in rows:
        lines.append(
            f"{row['realm'][:13]:<14}{row['score']:>6.2f}"
            f" {row.get('items') or 0:>7,}"
            f" {fmt(row.get('churn'), '.0%'):>7}"
            f" {fmt(row.get('gone'), '.0%'):>6}"
            f" {fmt(row.get('depth'), '.0f'):>6}"
            f" {fmt(row.get('identical'), '.0%'):>6}"
            f" {fmt(row.get('undercut'), '.1%'):>9}"
            f" {fmt_gold(row.get('market_value')):>8}"
            f" {fmt(row.get('trend'), '.2f'):>6}"
            f" {row.get('scans') or 0:>6}")
    lines.append("")
    for row in rows:
        lines.append(f"{row['realm']}: {verdict(row)}")
    lines.append("")
    lines.append(
        f"region baseline (not per realm): {baseline['region_items']:,} items"
        f" tracked; {fmt(baseline['region_moving'], '.0%')} of them sold at all,"
        f" and those sell {fmt(baseline['region_sold_per_day'], '.2f')}/day"
        f" (median).")
    return "\n".join(lines)


def write_csv(rows: List[dict], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fields = ["realm", "scope", "score", "items", "listings", "depth",
              "single_share", "undercut", "undercut_p75", "identical",
              "churn", "gone", "new",
              "market_value", "trend", "value", "commodity_share",
              "scans", "window_h", "snapshot",
              "p_churn", "p_undercut", "p_competition", "p_price"]
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


def all_realms(con) -> List[str]:
    """Every realm the TSM snapshot knows, so the ranking has a universe."""
    rows = con.execute(
        "SELECT DISTINCT scope FROM tsm_realm_price ORDER BY scope").fetchall()
    return [r[0] for r in rows if r[0]]


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--realms", default=None,
                        help="comma separated realm names; default: every realm"
                             " with data, best first")
    parser.add_argument("--hours", type=int, default=24,
                        help="how far back to look for the churn comparison"
                             " (default 24)")
    parser.add_argument("--top", type=int, default=0,
                        help="print only the best N realms")
    parser.add_argument("--csv", default=None, help="write the full table here")
    args = parser.parse_args(argv)

    load_env()
    con = connect(None)
    try:
        realms = ([r.strip() for r in args.realms.split(",") if r.strip()]
                  if args.realms else all_realms(con))
    finally:
        try:
            con.close()
        except Exception:
            pass

    rows, baseline = build(realms, args.hours)
    print(report(rows[:args.top] if args.top else rows, baseline))
    if args.csv:
        write_csv(rows, Path(args.csv))
        print(f"\nwrote {args.csv} ({len(rows)} realms)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
