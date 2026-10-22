"""CLI for the auction-house intelligence layer.

    python scripts/intel.py ah-ingest snapshot.json --region eu --realm Kazzak
    python scripts/intel.py addon-import
    python scripts/intel.py sellers --realm Dragonmaw
    python scripts/intel.py diagnose --realm Dragonmaw
    python scripts/intel.py realm-ml
    python scripts/intel.py farms --realm Dragonmaw woweconomy/wowhead_zones/zone_16.json
    python scripts/intel.py mobs --realm Dragonmaw --persist
    python scripts/intel.py ledger-verify data/experiments/ledger.jsonl

The commands read the existing ``tsm_markets.db`` and write to
``data/intel.sqlite``.  Nothing here rewrites an existing table.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from woweconomy.analytics import auction_intel as ai  # noqa: E402
from woweconomy.intel import actions, ah, ah_undercut, dynamics, farming, inventory, ledger, mail, mobs, realm_ml, relist, sellers, store, undercut  # noqa: E402


def _print(payload) -> None:
    print(json.dumps(payload, indent=2, ensure_ascii=False, default=str))


def _ts(value: str) -> int:
    if value.isdigit():
        return int(value)
    from datetime import datetime, timezone
    text = value[:-1] + "+00:00" if value.endswith("Z") else value
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return int(parsed.timestamp())


def cmd_ah_ingest(args) -> int:
    con = store.connect(args.db)
    captured = _ts(args.captured_at) if args.captured_at else None
    result = ah.ingest_file(
        con, args.file, region=args.region, realm=args.realm,
        realm_slug=args.realm_slug or "", connected_realm_id=args.connected_realm_id,
        captured_at=captured, source=args.source,
        commodity_endpoint=args.commodity_endpoint)
    _print(result.__dict__)
    return 0


def cmd_ah_fetch(args) -> int:
    con = store.connect(args.db)
    client = ah.AuctionHouseClient(region=args.region)
    if args.sync_realms:
        count = ah.sync_realm_index(con, client)
        print(f"realm index: {count} realms", file=sys.stderr)
    realm_id = args.connected_realm_id or ah.connected_realm_id(
        con, args.realm, args.region)
    if not realm_id:
        print("unknown connected realm; run with --sync-realms first",
              file=sys.stderr)
        return 2
    payload, modified = client.fetch_auctions(realm_id, args.modified_since or "")
    if payload is None:
        print("304: unchanged", file=sys.stderr)
        return 0
    parsed = ah.parse_payload(payload, region=args.region, realm=args.realm,
                              realm_slug=args.realm_slug or "",
                              connected_realm_id=int(realm_id),
                              source="blizzard_api",
                              api_last_modified=modified)
    _print(ah.ingest_bundle(con, parsed).__dict__)
    return 0


def cmd_addon_import(args) -> int:
    path = Path(args.path) if args.path else ai.find_saved_variables()
    if not path:
        print("no addon SavedVariables found", file=sys.stderr)
        return 2
    con = store.connect(args.db)
    result = {"saved_variables": str(path)}

    events = ai.parse_saved_variables(path)
    names = ai.build_name_index(args.tsm_db)
    filled = ai.fill_item_ids(events, names)
    tsm = sqlite3.connect(args.tsm_db)
    try:
        inserted, present = ai.import_events(tsm, events)
    finally:
        tsm.close()
    result["events"] = {"read": len(events), "inserted": inserted,
                        "already_present": present, "item_ids_filled": filled}

    realm = ""
    try:
        snapshot = inventory.parse_export_file(path)
        realm = snapshot.realm
        new = inventory.import_snapshot(con, snapshot)
        previous = inventory.previous_snapshot(con, snapshot)
        report = None
        if previous is not None:
            journal = inventory.load_journal_events(
                args.tsm_db, previous.captured_at, snapshot.captured_at,
                snapshot.character)
            report = inventory.reconcile(previous, snapshot, journal)
            inventory.persist_report(con, report)
        result["inventory"] = {
            "snapshot_id": snapshot.snapshot_id, "new": new,
            "captured_at": snapshot.captured_at,
            "available": sorted(snapshot.available),
            "rows": len(snapshot.rows),
            "reconciliation": report.summary() if report else None,
        }
    except (ValueError, OSError) as exc:
        result["inventory"] = {"error": str(exc)}

    try:
        observations = sellers.parse_export_file(path)
        result["sellers"] = {"parsed": len(observations),
                             "inserted": sellers.import_observations(con, observations)}
    except (ValueError, OSError) as exc:
        result["sellers"] = {"error": str(exc)}

    try:
        invoices = mail.parse_export_file(path)
        inserted = mail.import_invoices(con, invoices)
        link = mail.link_buyers(con, args.tsm_db, realm=realm,
                                window_days=35, dry_run=args.no_link)
        result["mail"] = {"parsed": len(invoices), "inserted": inserted,
                          "link": link}
    except (ValueError, OSError) as exc:
        result["mail"] = {"error": str(exc)}

    try:
        rows = undercut.parse_export_file(path)
        inserted = undercut.import_observations(con, rows)
        result["undercut"] = {"parsed": len(rows), "inserted": inserted}
    except (ValueError, OSError) as exc:
        result["undercut"] = {"error": str(exc)}

    # Kills, loot and copper.  An export from an addon older than 1.5.0 has
    # no such block; that is reported, not fatal to the rest of the import.
    try:
        rows = mobs.parse_export_file(path)
        inserted = mobs.import_observations(con, rows)
        result["kills"] = {"parsed": len(rows), "inserted": inserted}
    except (ValueError, OSError) as exc:
        result["kills"] = {"error": str(exc)}
    _print(result)
    return 0


def cmd_sellers(args) -> int:
    con = store.connect(args.db)
    since = int(__import__("time").time()) - args.days * 86400
    observations = sellers.load_observations(con, args.realm, since)
    sales = sellers.load_confirmed_sales(args.tsm_db, since, realm=args.realm)
    correlation = sellers.correlate_sales(sales, observations, args.window_h)
    profiles = sellers.competitor_profiles(observations, correlation["inferred"])
    episodes = sellers.co_listing_episodes(observations, args.gap_h)
    _print({
        "realm": args.realm or "all",
        "observations": len(observations),
        "confirmed_sales_examined": len(sales),
        "correlation": {k: v for k, v in correlation.items() if k != "confirmed"},
        "confirmed": correlation["confirmed"][: args.limit],
        "inferred": correlation["inferred"][: args.limit],
        "profiles": profiles[: args.limit],
        "episodes": {key: value[: args.limit] for key, value in episodes.items()
                     if isinstance(value, list)},
    })
    return 0


def cmd_diagnose(args) -> int:
    con = store.connect(args.db)
    since = int(__import__("time").time()) - args.days * 86400
    events = inventory.load_journal_events(args.tsm_db, since,
                                           int(__import__("time").time()))
    seller_rows = [{"item_id": o.item_id, "seller": o.seller}
                   for o in sellers.load_observations(con, args.realm, since)]
    report = dynamics.realm_dynamics(con, args.region, args.realm, events,
                                     seller_rows)
    report["commodities"] = sellers.commodity_market_summary(con, args.region,
                                                             args.realm)
    _print(report)
    return 0


def cmd_realm_ml(args) -> int:
    from woweconomy.ml.config import MLConfig
    from woweconomy.ml.pipeline import DfaLearningEngine
    config = MLConfig()
    engine = DfaLearningEngine(config)
    engine.load()
    frame, dataset_report, link_report = engine.build_rows()
    cfg = realm_ml.RealmMLConfig(region=args.region, min_price_rows=args.min_rows,
                                 iterations=args.iterations,
                                 models_dir=Path(args.models_dir))
    con = store.connect(args.db)
    results = realm_ml.train_all(frame, cfg, con)
    _print({
        "dataset": dataset_report.summary(),
        "linkage": link_report.summary(),
        "realms": {key: result.__dict__ for key, result in results.items()},
        "note": ("each model was fitted and scored on its own realm; the"
                 " shared-prior validator is available per realm and is not"
                 " used for a served prediction without a measured gain"),
    })
    return 0


def cmd_farms(args) -> int:
    con = store.connect(args.db)
    stock = inventory.current_units(con, args.realm)
    kill_rates = (farming.load_kill_rates(args.kill_rates)
                  if args.kill_rates else {})
    # What the game measured, when it measured anything.  A missing
    # measurement leaves the Wowhead rate and the default kills/hour in
    # place, labelled as such.
    observed = (mobs.observed_rates(con, args.realm, args.days)
                if not args.no_observed else None)
    result = farming.recommend_for_realm(
        args.tsm_db, con, args.region, args.realm, args.zones,
        farming.FarmConfig(kills_per_hour=args.kpm, kill_rates=kill_rates),
        inventory_units=stock, observed=observed)
    result["inventory_items"] = len(stock)
    result["recommendations"] = [
        {**rec.__dict__, "items": [item.__dict__ for item in rec.items]}
        for rec in result["recommendations"][: args.top]
    ]
    _print(result)
    return 0


def cmd_mobs(args) -> int:
    """What the mobs this account killed were worth."""
    con = store.connect(args.db)
    _print(mobs.summary(con, args.tsm_db, args.realm, args.region,
                        days=args.days, limit=args.top,
                        persist=args.persist))
    return 0


def cmd_mail_import(args) -> int:
    path = Path(args.path) if args.path else ai.find_saved_variables()
    if not path:
        print("no addon SavedVariables found", file=sys.stderr)
        return 2
    con = store.connect(args.db)
    try:
        invoices = mail.parse_export_file(path)
    except (ValueError, OSError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    inserted = mail.import_invoices(con, invoices)
    _print({"saved_variables": str(path), "parsed": len(invoices),
            "inserted": inserted})
    return 0


def cmd_undercut_import(args) -> int:
    path = Path(args.path) if args.path else ai.find_saved_variables()
    if not path:
        print("no addon SavedVariables found", file=sys.stderr)
        return 2
    con = store.connect(args.db)
    try:
        rows = undercut.parse_export_file(path)
    except (ValueError, OSError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    inserted = undercut.import_observations(con, rows)
    _print({"saved_variables": str(path), "parsed": len(rows),
            "inserted": inserted})
    return 0


def cmd_undercut(args) -> int:
    con = store.connect(args.db)
    _print(undercut.summarise(con, args.realm, args.days, args.limit))
    return 0


def cmd_blizzard_undercut(args) -> int:
    """Match my posted auctions against the last Blizzard snapshot."""
    con = store.connect(args.db)
    result = ah_undercut.scan_realm(con, args.realm, character=args.character)
    if not result.get("error"):
        result["relist"] = relist.advise(con, args.realm, days=args.days)
    _print(result)
    return 0 if not result.get("error") else 2


def cmd_actions(args) -> int:
    """The presses the HUD recorded in the addon export."""
    path = Path(args.path) if args.path else ai.find_saved_variables()
    if not path:
        print("no addon SavedVariables found", file=sys.stderr)
        return 2
    try:
        rows = actions.parse_export_file(path)
    except (ValueError, OSError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    _print({"saved_variables": str(path), "actions": rows[-args.limit:],
            "total": len(rows)})
    return 0


def cmd_relist(args) -> int:
    con = store.connect(args.db)
    _print(relist.advise(
        con, args.realm, args.days,
        min_gap=args.min_gap, heavy_gap=args.heavy_gap,
        queue_factor=args.queue_factor, short_time_h=args.short_time_h,
        long_time_h=args.long_time_h))
    return 0


def cmd_mail_link(args) -> int:
    con = store.connect(args.db)
    _print(mail.link_buyers(con, args.tsm_db, args.realm, args.window_days,
                            args.dry_run))
    return 0


def cmd_ledger_verify(args) -> int:
    _print(ledger.ExperimentLedger(args.path).verify())
    return 0


def cmd_ledger_append(args) -> int:
    log = ledger.ExperimentLedger(args.path)
    payload = json.loads(args.json)
    record = log.append(args.kind, payload)
    _print(record.as_dict())
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default=str(store.DEFAULT_DB))
    parser.add_argument("--tsm-db", default=str(ROOT / "tsm_markets.db"))
    parser.add_argument("--region", default="eu")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("ah-ingest", help="ingest one captured AH JSON payload")
    p.add_argument("file")
    p.add_argument("--realm", required=True)
    p.add_argument("--realm-slug", default="")
    p.add_argument("--connected-realm-id", type=int)
    p.add_argument("--captured-at", default="")
    p.add_argument("--source", default="file")
    p.add_argument("--commodity-endpoint", action="store_true")
    p.set_defaults(func=cmd_ah_ingest)

    p = sub.add_parser("ah-fetch", help="fetch a connected realm's auctions")
    p.add_argument("--realm", required=True)
    p.add_argument("--realm-slug", default="")
    p.add_argument("--connected-realm-id", type=int)
    p.add_argument("--modified-since", default="")
    p.add_argument("--sync-realms", action="store_true")
    p.set_defaults(func=cmd_ah_fetch)

    p = sub.add_parser("addon-import", help="import the addon export")
    p.add_argument("--path")
    p.add_argument("--no-link", action="store_true",
                   help="import mail invoices but do not update the journal")
    p.set_defaults(func=cmd_addon_import)

    p = sub.add_parser("mail-import", help="import mailbox invoices only")
    p.add_argument("--path")
    p.set_defaults(func=cmd_mail_import)

    p = sub.add_parser("undercut-import",
                       help="import undercut observations (seller per listing)")
    p.add_argument("--path")
    p.set_defaults(func=cmd_undercut_import)

    p = sub.add_parser("undercut",
                       help="who has been posting below my price, by item")
    p.add_argument("--realm", default="")
    p.add_argument("--days", type=int, default=30)
    p.add_argument("--limit", type=int, default=20)
    p.set_defaults(func=cmd_undercut)

    p = sub.add_parser("blizzard-undercut",
                       help="derived undercuts from the last Blizzard snapshot")
    p.add_argument("--realm", required=True)
    p.add_argument("--character", default="")
    p.add_argument("--days", type=int, default=14)
    p.set_defaults(func=cmd_blizzard_undercut)

    p = sub.add_parser("actions", help="the HUD actions in the addon export")
    p.add_argument("--path")
    p.add_argument("--limit", type=int, default=20)
    p.set_defaults(func=cmd_actions)

    p = sub.add_parser("relist",
                       help="cancel and reprice, hold, or let it expire?")
    p.add_argument("--realm", default="")
    p.add_argument("--days", type=int, default=14)
    p.add_argument("--min-gap", type=float, default=relist.DEFAULT_MIN_GAP)
    p.add_argument("--heavy-gap", type=float, default=relist.DEFAULT_HEAVY_GAP)
    p.add_argument("--queue-factor", type=float,
                   default=relist.DEFAULT_QUEUE_FACTOR)
    p.add_argument("--short-time-h", type=float,
                   default=relist.DEFAULT_SHORT_TIME_H)
    p.add_argument("--long-time-h", type=float,
                   default=relist.DEFAULT_LONG_TIME_H)
    p.set_defaults(func=cmd_relist)

    p = sub.add_parser("mail-link", help="attach mail buyers to unlinked sales")
    p.add_argument("--realm", default="")
    p.add_argument("--window-days", type=int, default=35)
    p.add_argument("--dry-run", action="store_true")
    p.set_defaults(func=cmd_mail_link)

    p = sub.add_parser("sellers", help="correlate seller observations")
    p.add_argument("--realm", default="")
    p.add_argument("--days", type=int, default=30)
    p.add_argument("--window-h", type=int, default=336)
    p.add_argument("--gap-h", type=int, default=12)
    p.add_argument("--limit", type=int, default=25)
    p.set_defaults(func=cmd_sellers)

    p = sub.add_parser("diagnose", help="one realm's market dynamics")
    p.add_argument("--realm", required=True)
    p.add_argument("--days", type=int, default=14)
    p.set_defaults(func=cmd_diagnose)

    p = sub.add_parser("realm-ml", help="fit one model per realm")
    p.add_argument("--min-rows", type=int, default=40)
    p.add_argument("--iterations", type=int, default=300)
    p.add_argument("--models-dir", default=str(realm_ml.DEFAULT_MODELS_DIR))
    p.set_defaults(func=cmd_realm_ml)

    p = sub.add_parser("farms", help="recommend farms on realizable returns")
    p.add_argument("--realm", required=True)
    p.add_argument("--kpm", type=float, default=20.0,
                   help="default kills/hour for NPCs without a measured rate")
    p.add_argument("--kill-rates", default="",
                   help="JSON map npc_id -> measured kills/hour")
    p.add_argument("--days", type=int, default=30,
                   help="how far back to read the in-game observations")
    p.add_argument("--no-observed", action="store_true",
                   help="ignore the in-game kill and drop measurements")
    p.add_argument("--top", type=int, default=10)
    p.add_argument("zones", nargs="+")
    p.set_defaults(func=cmd_farms)

    p = sub.add_parser("mobs",
                       help="kills, loot and gold per mob from in-game data")
    p.add_argument("--realm", required=True)
    p.add_argument("--days", type=int, default=30)
    p.add_argument("--top", type=int, default=20)
    p.add_argument("--persist", action="store_true",
                   help="store this dated valuation alongside the observations")
    p.set_defaults(func=cmd_mobs)

    p = sub.add_parser("ledger-verify", help="verify an experiment ledger")
    p.add_argument("path")
    p.set_defaults(func=cmd_ledger_verify)

    p = sub.add_parser("ledger-append", help="append to an experiment ledger")
    p.add_argument("path")
    p.add_argument("--kind", required=True, choices=ledger.KINDS)
    p.add_argument("--json", required=True)
    p.set_defaults(func=cmd_ledger_append)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
