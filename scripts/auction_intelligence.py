"""DFA Auction Intelligence — CLI Ingestion, Validation and Reporting.

Usage:
    python scripts/auction_intelligence.py import
    python scripts/auction_intelligence.py import-jsonl data/events.jsonl
    python scripts/auction_intelligence.py export --out reports/events_export.jsonl
    python scripts/auction_intelligence.py validate reports/events_export.jsonl
    python scripts/auction_intelligence.py stats
    python scripts/auction_intelligence.py report --out reports/auction_intelligence.md
"""
from __future__ import annotations

import argparse
import glob
import json
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from woweconomy.analytics.auction_intelligence import (  # noqa: E402
    generate_auction_intelligence_report, load_overall_auction_summary,
)
from woweconomy.services.auction_intelligence import (  # noqa: E402
    export_events_jsonl, ingest_journalator_archive, ingest_jsonl,
    init_auction_events_db, validate_event,
)

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except (AttributeError, OSError):
    pass

DB_PATH = ROOT / "tsm_markets.db"

WTF_GLOBS_JOURNALATOR = [
    r"C:/Program Files (x86)/World of Warcraft/*/WTF/Account/*/SavedVariables/Journalator.lua",
    r"C:/Program Files/World of Warcraft/*/WTF/Account/*/SavedVariables/Journalator.lua",
    r"D:/*/World of Warcraft/*/WTF/Account/*/SavedVariables/Journalator.lua",
]

WTF_GLOBS_DFA_AI = [
    r"C:/Program Files (x86)/World of Warcraft/*/WTF/Account/*/SavedVariables/DFA_AuctionIntelligence.lua",
    r"C:/Program Files/World of Warcraft/*/WTF/Account/*/SavedVariables/DFA_AuctionIntelligence.lua",
    r"D:/*/World of Warcraft/*/WTF/Account/*/SavedVariables/DFA_AuctionIntelligence.lua",
]


def find_latest_file(globs: list[str]) -> Path | None:
    hits = [Path(p) for g in globs for p in glob.glob(g)]
    return max(hits, key=lambda p: p.stat().st_mtime) if hits else None


def cmd_import(args) -> int:
    con = sqlite3.connect(args.db)
    init_auction_events_db(con)

    total_inserted = 0
    total_duplicates = 0

    # 1. Check for DFA_AuctionIntelligence.lua export
    dfa_path = Path(args.dfa_addon) if args.dfa_addon else find_latest_file(WTF_GLOBS_DFA_AI)
    if dfa_path and dfa_path.exists():
        print(f"Reading DFA Addon SavedVariables: {dfa_path}")
        text = dfa_path.read_text(encoding="utf-8", errors="replace")
        # Check if jsonl string is in export
        m = text.find('["jsonl"] = "')
        if m != -1:
            # We can extract the json lines
            print("  Extracting embedded JSONL export from addon...")
            # For simplicity, extract lines that parse as valid JSON
            lines = []
            for line in text.splitlines():
                line = line.strip().rstrip(",")
                if line.startswith("{") and line.endswith("}"):
                    try:
                        json.loads(line)
                        lines.append(line)
                    except Exception:
                        pass
            if lines:
                temp_file = ROOT / "reports" / "_temp_addon_export.jsonl"
                temp_file.parent.mkdir(parents=True, exist_ok=True)
                temp_file.write_text("\n".join(lines), encoding="utf-8")
                res = ingest_jsonl(con, temp_file, source_tag="dfa_addon_export")
                print(f"  Addon Export Ingested: {res.summary()}")
                total_inserted += res.inserted
                total_duplicates += res.duplicates

    # 2. Check for Journalator.lua archive
    j_path = Path(args.journalator) if args.journalator else find_latest_file(WTF_GLOBS_JOURNALATOR)
    if j_path and j_path.exists():
        print(f"Reading Journalator archive: {j_path}")
        res_j = ingest_journalator_archive(con, j_path)
        print(f"  Journalator Ingested: {res_j.summary()}")
        total_inserted += res_j.inserted
        total_duplicates += res_j.duplicates
        if res_j.errors:
            for err in res_j.errors[:5]:
                print(f"    Warning: {err}")
    else:
        print("  Journalator.lua not found.")

    print(f"\nImport finished: {total_inserted} new events inserted, {total_duplicates} duplicates skipped.")
    con.close()
    return 0


def cmd_import_jsonl(args) -> int:
    con = sqlite3.connect(args.db)
    res = ingest_jsonl(con, args.file, source_tag="cli_jsonl")
    print(res.summary())
    if res.errors:
        print("\nErrors encountered:")
        for err in res.errors[:10]:
            print(f"  {err}")
    con.close()
    return 0 if res.invalid == 0 else 1


def cmd_export(args) -> int:
    con = sqlite3.connect(args.db)
    count = export_events_jsonl(con, args.out, since_unix=args.since, realm=args.realm)
    print(f"Exported {count} events to {args.out}")
    con.close()
    return 0


def cmd_validate(args) -> int:
    path = Path(args.file)
    if not path.exists():
        print(f"File not found: {path}")
        return 1

    total = 0
    valid_count = 0
    invalid_count = 0
    seen_signatures = set()
    dup_count = 0

    print(f"Validating {path}...")
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        for idx, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            total += 1
            try:
                data = json.loads(line)
            except Exception as e:
                invalid_count += 1
                if invalid_count <= 5:
                    print(f"  Line {idx}: JSON Error: {e}")
                continue

            ok, errs = validate_event(data)
            if not ok:
                invalid_count += 1
                if invalid_count <= 5:
                    print(f"  Line {idx}: Schema Error: {'; '.join(errs)}")
                continue

            ev_type = data.get('event_type') or data.get('eventType')
            item_id = data.get('item_id') if data.get('item_id') is not None else data.get('itemId')
            ts = data.get('timestamp_unix') or data.get('timestamp')
            qty = data.get('qty') or data.get('quantity') or 1
            price = data.get('total_price_copper') if data.get('total_price_copper') is not None else data.get('totalPriceCopper', 0)
            realm = data.get('realm') or ''
            counter = data.get('counterparty') or data.get('playerName') or ''
            sig = f"{ev_type}:{item_id}:{ts}:{qty}:{price}:{realm}:{counter}"
            if sig in seen_signatures:
                dup_count += 1
            else:
                seen_signatures.add(sig)

            valid_count += 1

    print("\nValidation Summary:")
    print(f"  Total Lines: {total}")
    print(f"  Valid Events: {valid_count}")
    print(f"  Invalid Lines: {invalid_count}")
    print(f"  Duplicate Signatures in File: {dup_count}")

    if invalid_count == 0:
        print("  Status: PASSED (Schema compliant)")
        return 0
    else:
        print("  Status: FAILED")
        return 1


def cmd_stats(args) -> int:
    con = sqlite3.connect(args.db)
    init_auction_events_db(con)
    summary = load_overall_auction_summary(con, realm=args.realm)
    print("--- DFA Auction Intelligence Statistics ---")
    print(f"Realm: {summary['realm']}")
    print(f"Total Events: {summary['total_events']:,}")
    print(f"Event Counts: {summary['counts']}")
    print(f"Units Sold: {summary['units_sold']:,}")
    print(f"Gross Revenue: {summary['gross_sales_copper'] / 10000:,.1f} g")
    print(f"Consignment Fees (AH Cut): {summary['total_fees_copper'] / 10000:,.1f} g")
    print(f"Net Realized Gold: {summary['net_proceeds_copper'] / 10000:,.1f} g")
    print(f"Listing Deposits Paid: {summary['listing_deposits_copper'] / 10000:,.1f} g")
    str_val = f"{summary['sell_through_rate']:.1%}" if summary["sell_through_rate"] is not None else "N/A"
    print(f"Observed Sell-Through Rate: {str_val}")
    con.close()
    return 0


def cmd_report(args) -> int:
    con = sqlite3.connect(args.db)
    init_auction_events_db(con)
    report_text = generate_auction_intelligence_report(con, realm=args.realm)
    if args.out:
        out_path = Path(args.out)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(report_text, encoding="utf-8")
        print(f"Report written to: {out_path}")
    else:
        print(report_text)
    con.close()
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--db", default=str(DB_PATH), help="Path to tsm_markets.db")
    subparsers = parser.add_subparsers(dest="subcommand", required=True)

    # import
    p_imp = subparsers.add_parser("import", help="Import events from SavedVariables")
    p_imp.add_argument("--journalator", default=None, help="Explicit path to Journalator.lua")
    p_imp.add_argument("--dfa-addon", default=None, help="Explicit path to DFA_AuctionIntelligence.lua")

    # import-jsonl
    p_jl = subparsers.add_parser("import-jsonl", help="Import events from a JSONL file")
    p_jl.add_argument("file", help="Path to JSONL file")

    # export
    p_exp = subparsers.add_parser("export", help="Export events to a JSONL file")
    p_exp.add_argument("--out", required=True, help="Path to output JSONL file")
    p_exp.add_argument("--since", type=int, default=None, help="Filter timestamp >= unix seconds")
    p_exp.add_argument("--realm", default=None, help="Filter by realm")

    # validate
    p_val = subparsers.add_parser("validate", help="Validate a JSONL file against schema")
    p_val.add_argument("file", help="Path to JSONL file")

    # stats
    p_stat = subparsers.add_parser("stats", help="Display summary statistics")
    p_stat.add_argument("--realm", default=None, help="Filter by realm")

    # report
    p_rep = subparsers.add_parser("report", help="Generate full markdown report")
    p_rep.add_argument("--realm", default=None, help="Filter by realm")
    p_rep.add_argument("--out", default=None, help="Optional output path")

    args = parser.parse_args(argv)

    if args.subcommand == "import":
        return cmd_import(args)
    elif args.subcommand == "import-jsonl":
        return cmd_import_jsonl(args)
    elif args.subcommand == "export":
        return cmd_export(args)
    elif args.subcommand == "validate":
        return cmd_validate(args)
    elif args.subcommand == "stats":
        return cmd_stats(args)
    elif args.subcommand == "report":
        return cmd_report(args)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
