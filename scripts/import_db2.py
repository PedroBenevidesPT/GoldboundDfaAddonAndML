"""Import the client's item tables (DB2) so we know what is BoE and what grants what.

Why this exists
---------------
``items`` (TSM) only holds items that were seen on an auction house, and
``blizzard_items`` had binding/quality for ~6% of them -- the Blizzard API can
*enrich* an id, it cannot *enumerate* the game's items, so it can never tell us
"these are all the BoE items".  The client's own ItemSparse table can, and it
carries everything we need at once:

    ItemSparse.ID            the item id (== our ItemID everywhere else)
    ItemSparse.Display_lang  its English name
    ItemSparse.Bonding       bind type; 2 == Bind on Equip, i.e. sellable
    ItemSparse.OverallQualityID  quality (0 poor .. 5 legendary)
    ItemSparse.ItemSquishEraID   != 0 means the row belongs to a CLASSIC era
                             (see below), not to retail
    ItemSparse.ExpansionID, ItemLevel, RequiredLevel   for context

    ItemModifiedAppearance.ItemID            -> item
    ItemModifiedAppearance.ItemAppearanceID  -> ItemAppearance.ID

and ``ItemAppearance.ID`` is the ``visualid`` that Data for Azeroth uses as the
key of its appearance catalogue.  That is the bridge this project was missing:
the wishlist says which *appearances* collectors still lack, and this says which
*items* grant them -- and whether those items can be sold at all.

Classic eras
------------
The same client data also carries the items of the Classic *realms* (WotLK,
MoP, ...), and they are the trap this project fell into: an item can be BoE in
MoP Classic and be sold on that realm's auction house while meaning nothing on
retail.  Measured on the live build: ``ItemSquishEraID`` is 0 for 171,760 rows,
2 for 4,935 and 1 for 7 -- and "Slippers of the Hissing Cult" (id 271435,
RequiredLevel 90, ItemLevel 219, Bonding 1) exists ONLY as an era-2 row, with no
retail twin.  So era != 0 is the mechanical way to tell the two apart, and every
report here can now filter on it instead of guessing from the price.

Source
------
The tables are served as CSV by wago.tools, one file per table:

    https://wago.tools/db2/<Table>/csv?build=<build>

No key, no login.  Their robots.txt asks crawlers not to walk the site, so this
fetches the two files it needs, once, and never enumerates anything else.  The
raw CSV is kept on disk under ``db2/`` so re-runs do not touch the network.

The build is PINNED (``DEFAULT_BUILD``): without ``?build=`` the site serves
whatever is current, so the same command would silently mix two patches' item
tables (and an item squished in one of them would change binding between runs).
The build actually used is recorded in ``db2_meta``; when it differs from the
one on disk, the files are re-downloaded.

Usage
-----
    python scripts/import_db2.py                 # download (if needed) + import
    python scripts/import_db2.py --build 12.1.7.70424   # pin another build
    python scripts/import_db2.py --stats         # just report what is in the DB
    python scripts/import_db2.py --force         # re-download
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from woweconomy.dbcompat import connect_store, is_postgres, load_env

WAGO = "https://wago.tools/db2/{table}/csv?build={build}"
DEFAULT_BUILD = "12.1.7.70424"  # confirmado pelo cliente retail; pin, nao "latest"
DEFAULT_DIR = Path("db2")
DEFAULT_DB = Path("tsm_markets.db")

# Column positions are read from the header, never hardcoded: the layout is
# build dependent (the header on the live build is 104 columns wide).
WANTED_COLUMNS = {
    "ItemSparse": ("ID", "Display_lang", "Bonding", "OverallQualityID",
                   "ExpansionID", "ItemSquishEraID", "ItemLevel", "RequiredLevel"),
    "ItemModifiedAppearance": ("ItemID", "ItemAppearanceID"),
}

# Era 0 is the retail row; anything else is a Classic-era row of the same item
# space (confirmed by the data above, not by an enum table).
RETAIL_ERA = 0

# Enum.ItemBind (warcraft.wiki.gg/wiki/Enum.ItemBind), confirmed against the
# live CSV: id 25 "Worn Shortsword" -- a classic BoE -- carries Bonding=2.
BINDING_NAMES = {
    0: "NONE", 1: "ON_ACQUIRE", 2: "ON_EQUIP", 3: "ON_USE", 4: "QUEST",
    7: "TO_ACCOUNT", 8: "TO_BNET", 9: "TO_BNET_UNTIL_EQUIPPED",
}

ITEM_COLUMNS = ("itemId", "name", "binding", "quality", "expansion", "era",
                "itemLevel", "requiredLevel")

SCHEMA = """
CREATE TABLE IF NOT EXISTS db2_item_appearance(
    itemId INTEGER NOT NULL,
    appearanceId INTEGER NOT NULL,
    PRIMARY KEY(itemId, appearanceId)
);
CREATE INDEX IF NOT EXISTS idx_db2_item_appearance ON db2_item_appearance(appearanceId);
CREATE TABLE IF NOT EXISTS db2_meta(
    key TEXT PRIMARY KEY, value TEXT
);
"""


def log(msg: str) -> None:
    print(msg, flush=True)


def download(table: str, path: Path, build: str = DEFAULT_BUILD,
             force: bool = False) -> Path:
    """Stream one table's CSV to disk.  Kept, so this only hits the network once."""
    if path.exists() and path.stat().st_size > 0 and not force:
        log(f"  {table:<22} ja em {path} ({path.stat().st_size / 1e6:.1f} MB) -- sem rede")
        return path
    url = WAGO.format(table=table, build=build)
    path.parent.mkdir(parents=True, exist_ok=True)
    started = time.time()
    written = 0
    with requests.get(url, stream=True, timeout=300) as r:
        r.raise_for_status()
        tmp = path.with_suffix(path.suffix + ".part")
        with tmp.open("wb") as fh:
            for chunk in r.iter_content(1 << 20):
                fh.write(chunk)
                written += len(chunk)
                if written % (16 << 20) < (1 << 20):
                    log(f"  {table:<22} {written / 1e6:7.1f} MB ...")
        tmp.replace(path)
    log(f"  {table:<22} {written / 1e6:.1f} MB em {time.time() - started:.0f}s -> {path}")
    return path


def rows_of(path: Path, table: str):
    """Yield the wanted fields of each CSV row, by header name (csv, not split)."""
    want = WANTED_COLUMNS[table]
    with path.open(encoding="utf-8", newline="") as fh:
        reader = csv.reader(fh)
        header = next(reader)
        try:
            idx = [header.index(c) for c in want]
        except ValueError as exc:
            raise SystemExit(f"{table}: coluna em falta ({exc}); cabecalho: {header[:8]}...")
        for row in reader:
            if len(row) <= max(idx):
                continue
            yield tuple(row[i] for i in idx)


def number(value: str) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def import_table(con, table: str, path: Path) -> int:
    """Load one CSV into its table.  Returns the number of rows stored."""
    if table == "ItemSparse":
        # Derived table: the CSV is the source of truth, so replace it
        # wholesale (an older, narrower layout would otherwise be kept).
        # On Postgres the table belongs to Alembic: drop-and-create would
        # replace a migrated table with a SQLite-shaped one, so the rows are
        # cleared instead and the schema stays where it is documented.
        if is_postgres():
            con.execute("DELETE FROM db2_items")
        else:
            con.execute("DROP TABLE IF EXISTS db2_items")
            con.execute("""CREATE TABLE db2_items(
                itemId INTEGER PRIMARY KEY, name TEXT NOT NULL DEFAULT '',
                binding INTEGER, quality INTEGER, expansion INTEGER,
                era INTEGER, itemLevel INTEGER, requiredLevel INTEGER)""")
        sql = ("INSERT INTO db2_items(" + ", ".join(ITEM_COLUMNS) +
               ") VALUES(" + ",".join("?" * len(ITEM_COLUMNS)) + ")"
               " ON CONFLICT(itemId) DO UPDATE SET "
               + ", ".join(f"{c}=excluded.{c}" for c in ITEM_COLUMNS[1:]))
        n = 0
        batch = []
        for iid, name, bonding, quality, exp, era, ilvl, req in rows_of(path, table):
            key = number(iid)
            if key is None:
                continue
            batch.append((key, (name or "").strip(), number(bonding), number(quality),
                          number(exp), number(era), number(ilvl), number(req)))
            if len(batch) >= 20000:
                con.executemany(sql, batch)
                n += len(batch)
                batch.clear()
                con.commit()
        con.executemany(sql, batch)
        n += len(batch)
    else:
        con.execute("DELETE FROM db2_item_appearance")
        sql = ("INSERT INTO db2_item_appearance(itemId, appearanceId)"
               " VALUES(?,?) ON CONFLICT(itemId, appearanceId) DO NOTHING")
        n = 0
        batch = []
        for iid, aid in rows_of(path, table):
            a, b = number(iid), number(aid)
            if a is None or b is None:
                continue
            batch.append((a, b))
            if len(batch) >= 20000:
                con.executemany(sql, batch)
                n += len(batch)
                batch.clear()
                con.commit()
        con.executemany(sql, batch)
        n += len(batch)
    con.commit()
    return n


def report(con) -> None:
    q = con.execute
    items = q("SELECT COUNT(*) FROM db2_items").fetchone()[0]
    links = q("SELECT COUNT(*) FROM db2_item_appearance").fetchone()[0]
    print(f"\n== db2_items              {items:>8} itens")
    if not items:
        print("   (vazio -- corre o import primeiro)")
        return
    print("   binding:")
    for b, n in q("SELECT COALESCE(binding, -1), COUNT(*) FROM db2_items GROUP BY 1 ORDER BY 2 DESC"):
        label = BINDING_NAMES.get(b, f"({b})")
        print(f"     {label:<24} {n:>8}  {100.0 * n / items:5.1f}%")
    print("   era (0 = retail; != 0 = linha de era-Classic):")
    for e, n in q("SELECT COALESCE(era, -1), COUNT(*) FROM db2_items GROUP BY 1 ORDER BY 2 DESC"):
        print(f"     era {e:<20} {n:>8}")
    retail_boe = q("SELECT COUNT(*) FROM db2_items WHERE binding=2 AND COALESCE(era,0)=0").fetchone()[0]
    print(f"   BoE retail (binding=2, era=0) {retail_boe:>8}")
    print(f"== db2_item_appearance    {links:>8} pares item->aparicao")
    print(f"   itens distintos com aparicao: "
          f"{q('SELECT COUNT(DISTINCT itemId) FROM db2_item_appearance').fetchone()[0]}")
    print(f"   aparicoes distintas         : "
          f"{q('SELECT COUNT(DISTINCT appearanceId) FROM db2_item_appearance').fetchone()[0]}")
    meta = dict(q("SELECT key, value FROM db2_meta"))
    for k in ("build", "source", "downloaded_at", "ItemSparse",
              "ItemModifiedAppearance"):
        if k in meta:
            print(f"   {k:<24} {meta[k]}")


def wishlist_join(con, demand_dir: Path) -> None:
    """The whole point: how many wanted APPEARANCES become sellable ITEMS now."""
    path = demand_dir / "demand_appearances.csv"
    if not path.exists():
        print(f"\n(sem {path} -- salto o cruzamento com a wishlist)")
        return
    wanted = [r for r in csv.DictReader(path.open(encoding="utf-8")) if r.get("name")]
    con.execute("DROP TABLE IF EXISTS tmp_wanted")
    con.execute("CREATE TEMP TABLE tmp_wanted(appearanceId INTEGER PRIMARY KEY, name TEXT)")
    con.executemany(
        "INSERT INTO tmp_wanted(appearanceId, name) VALUES(?,?)"
        " ON CONFLICT(appearanceId) DO NOTHING",
        [(int(r["item_id"]), r["name"]) for r in wanted])
    con.commit()
    total = con.execute("SELECT COUNT(*) FROM tmp_wanted").fetchone()[0]
    print(f"\n== wishlist: {total} aparicoes em falta")
    for label, extra in (("tem item (qualquer bind)", ""),
                         ("item BoE (Bonding=2)", "AND i.binding = 2"),
                         ("item BoE com qualidade >=2", "AND i.binding = 2 AND i.quality >= 2")):
        n = con.execute(f"""
            SELECT COUNT(DISTINCT a.appearanceId)
            FROM tmp_wanted a
            JOIN db2_item_appearance ia ON ia.appearanceId = a.appearanceId
            JOIN db2_items i ON i.itemId = ia.itemId
            WHERE 1=1 {extra}""").fetchone()[0]
        print(f"   {label:<28} {n:>7}  ({100.0 * n / total:5.1f}%)")


def local_check(con, inventory: Path, zones_glob: str) -> None:
    """What the DB2 says about the data this project already had.

    This is the correction: the same item can be BoE on a Classic realm and
    meaningless on retail, so every existing dataset gets split by era and by
    bind type instead of being trusted as one pile.
    """
    q = con.execute
    print("\n== o que ja tinhamos, agora com era e binding do cliente")

    def split(label: str, source_sql: str) -> None:
        """Count the ids by (era, binding) -- the two axes that decide sellability."""
        rows = q(f"""SELECT COALESCE(d.era,0), COALESCE(d.binding,-1), COUNT(*)
                    FROM ({source_sql}) t
                    LEFT JOIN db2_items d ON d.itemId = t.itemId
                    GROUP BY 1, 2""").fetchall()
        total = sum(r[2] for r in rows) or 1
        classic = sum(r[2] for r in rows if r[0])
        boe = sum(r[2] for r in rows if r[1] == 2 and not r[0])
        bop = sum(r[2] for r in rows if r[1] == 1)
        missing = sum(r[2] for r in rows if not r[0] and r[1] == -1)
        print(f"   {label:<34} {total:>7} itens"
              f" | era-Classic {classic:>5} ({100.0 * classic / total:4.1f}%)"
              f" | BoE retail {boe:>5} ({100.0 * boe / total:4.1f}%)"
              f" | BoP {bop:>5} | fora do DB2 {missing:>4}")

    split("items do TSM (vistos num AH)", "SELECT itemId FROM items")
    split("... e com avgSalePrice > 0",
          "SELECT ms.itemId FROM market_snapshots ms JOIN snapshots s "
          "ON s.snapshotId = ms.snapshotId "
          "WHERE s.snapshotId = (SELECT MAX(snapshotId) FROM snapshots) "
          "AND ms.avgSalePrice > 0")

    if inventory.exists():
        ids = []
        with inventory.open(encoding="utf-8-sig", newline="") as fh:
            head = fh.readline()
            fh.seek(0)
            delim = ";" if head.count(";") >= head.count(",") else ","
            for row in csv.DictReader(fh, delimiter=delim):
                for key in ("ItemID", "itemId", "item_id", "ID"):
                    if key in row:
                        v = number(str(row[key]).strip())
                        if v is not None:
                            ids.append((v,))
                        break
        con.execute("DROP TABLE IF EXISTS tmp_bag")
        con.execute("CREATE TEMP TABLE tmp_bag(itemId INTEGER)")
        con.executemany("INSERT INTO tmp_bag VALUES(?)", ids)
        split(f"bolsa ({inventory.name})", "SELECT itemId FROM tmp_bag")

    paths = sorted(Path().glob(zones_glob))
    if paths:
        con.execute("DROP TABLE IF EXISTS tmp_zone")
        con.execute("CREATE TEMP TABLE tmp_zone(itemId INTEGER)")
        ids = set()
        for path in paths:
            data = json.loads(path.read_text(encoding="utf-8"))
            entries = data.values() if isinstance(data, dict) else data
            for entry in entries:
                for it in entry.get("items", []):
                    v = number(str(it.get("item_id") or ""))
                    if v is not None:
                        ids.add(v)
        con.executemany("INSERT INTO tmp_zone VALUES(?)", [(i,) for i in ids])
        split(f"drops das zonas ({len(paths)} ficheiros)", "SELECT itemId FROM tmp_zone")
    con.commit()


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--dir", default=str(DEFAULT_DIR), help="onde guardar os CSV crus")
    ap.add_argument("--db", default=str(DEFAULT_DB))
    ap.add_argument("--demand-dir", default=r"C:\economics\woweconomy\dfa",
                    help="pasta com demand_appearances.csv")
    ap.add_argument("--build", default=DEFAULT_BUILD,
                    help="build do cliente a usar no ?build= (default %(default)s)")
    ap.add_argument("--force", action="store_true", help="re-descarregar os CSV")
    ap.add_argument("--stats", action="store_true", help="so reportar o que ja esta")
    ap.add_argument("--inventory", default="scripts/inventario.csv")
    ap.add_argument("--zones-glob", default="woweconomy/wowhead_zones/zone_*.json")
    args = ap.parse_args(argv)

    # Read the project's .env: a manual import must write to the same database
    # the pipeline reads, not to a local file next to it.
    load_env()
    con = connect_store(args.db)
    if not is_postgres():
        con.executescript(SCHEMA)

    if not args.stats:
        out = Path(args.dir)
        stored = dict(con.execute("SELECT key, value FROM db2_meta").fetchall())
        stale = stored.get("build") != args.build
        if stale:
            log(f"  build em cache: {stored.get('build') or 'desconhecido'} -> "
                f"{args.build} (re-descarregar)")
        for table in WANTED_COLUMNS:
            path = out / f"{table}.csv"
            download(table, path, args.build, force=args.force or stale)
            n = import_table(con, table, path)
            con.execute("INSERT OR REPLACE INTO db2_meta(key, value) VALUES(?,?)",
                        (table, f"{n} linhas"))
            log(f"  {table:<22} {n} linhas importadas")
        con.execute("INSERT OR REPLACE INTO db2_meta(key, value) VALUES(?,?)",
                    ("build", args.build))
        con.execute("INSERT OR REPLACE INTO db2_meta(key, value) VALUES(?,?)",
                    ("source", WAGO.format(table="<Table>", build=args.build)))
        con.execute("INSERT OR REPLACE INTO db2_meta(key, value) VALUES(?,?)",
                    ("downloaded_at", datetime.now(timezone.utc).isoformat(timespec="seconds")))
        con.commit()

    report(con)
    wishlist_join(con, Path(args.demand_dir))
    local_check(con, Path(args.inventory), args.zones_glob)
    con.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
