"""Storage for the intelligence layer.

One SQLite file (``data/intel.sqlite`` by default) next to -- never on top
of -- ``tsm_markets.db``.  The journal and the TSM mirror are read-only
inputs here; if this layer corrupts its own file the existing engine is
unaffected.

Every table carries its provenance:

* ``ah_snapshot`` says where a snapshot came from and which payload hash
  produced it, so re-ingesting the same file is a no-op;
* ``ah_auction`` keeps the original ``time_left`` string *and* the mapped
  band.  A category the client adds later survives ingestion unchanged;
* ``seller_observation`` stores the raw tooltip line next to the parsed
  name, because a parser that cannot show its evidence cannot be trusted;
* ``inventory_snapshot`` is a full picture at one instant, and the
  reconciliation rows say which movement was explained by a journal event
  and which was not.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

DEFAULT_DB = Path(r"C:\FarmAnalysis\data\intel.sqlite")

SCHEMA = """
CREATE TABLE IF NOT EXISTS ah_snapshot (
    snapshot_id       TEXT PRIMARY KEY,
    region            TEXT NOT NULL,
    connected_realm_id INTEGER,
    realm             TEXT NOT NULL,
    realm_slug        TEXT NOT NULL DEFAULT '',
    captured_at       INTEGER NOT NULL,
    api_last_modified TEXT NOT NULL DEFAULT '',
    source            TEXT NOT NULL,
    payload_sha256    TEXT NOT NULL,
    auction_count     INTEGER NOT NULL DEFAULT 0,
    commodity_count   INTEGER NOT NULL DEFAULT 0,
    rejected_rows     INTEGER NOT NULL DEFAULT 0,
    imported_at       TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_ah_snapshot_realm
    ON ah_snapshot(region, realm, captured_at);

CREATE TABLE IF NOT EXISTS ah_auction (
    snapshot_id      TEXT NOT NULL,
    auction_key      TEXT NOT NULL,
    auction_id       INTEGER,
    item_id          INTEGER NOT NULL,
    item_context     INTEGER,
    quantity         INTEGER NOT NULL,
    unit_price_copper INTEGER,
    buyout_copper    INTEGER,
    bid_copper       INTEGER,
    total_price_copper INTEGER,
    unit_price_source TEXT NOT NULL DEFAULT '',
    time_left_raw    TEXT,
    time_left_band   TEXT,
    is_commodity     INTEGER NOT NULL DEFAULT 0,
    pet_species_id   INTEGER,
    bonus_lists      TEXT,
    modifiers        TEXT,
    PRIMARY KEY (snapshot_id, auction_key),
    FOREIGN KEY (snapshot_id) REFERENCES ah_snapshot(snapshot_id)
);
CREATE INDEX IF NOT EXISTS idx_ah_auction_item
    ON ah_auction(snapshot_id, item_id);

CREATE TABLE IF NOT EXISTS ah_realm (
    region              TEXT NOT NULL,
    realm_slug          TEXT NOT NULL,
    realm_name          TEXT NOT NULL,
    connected_realm_id  INTEGER NOT NULL,
    refreshed_at        TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (region, realm_slug)
);

CREATE TABLE IF NOT EXISTS seller_observation (
    obs_id       TEXT PRIMARY KEY,
    observed_at  INTEGER NOT NULL,
    realm        TEXT NOT NULL DEFAULT '',
    seller       TEXT NOT NULL,
    seller_name  TEXT NOT NULL,
    seller_realm TEXT NOT NULL DEFAULT '',
    item_id      INTEGER,
    item_name    TEXT NOT NULL DEFAULT '',
    auction_id   INTEGER,
    unit_price_copper INTEGER,
    source       TEXT NOT NULL,
    confidence   TEXT NOT NULL,
    label        TEXT NOT NULL DEFAULT '',
    raw_line     TEXT NOT NULL DEFAULT '',
    character    TEXT NOT NULL DEFAULT '',
    imported_at  TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_seller_name
    ON seller_observation(seller_name, realm, observed_at);
CREATE INDEX IF NOT EXISTS idx_seller_item
    ON seller_observation(item_id, realm, observed_at);

CREATE TABLE IF NOT EXISTS inventory_snapshot (
    snapshot_id  TEXT PRIMARY KEY,
    captured_at  INTEGER NOT NULL,
    character    TEXT NOT NULL DEFAULT '',
    realm        TEXT NOT NULL DEFAULT '',
    reason       TEXT NOT NULL DEFAULT '',
    source       TEXT NOT NULL DEFAULT '',
    bag_slots    INTEGER NOT NULL DEFAULT 0,
    available_json TEXT NOT NULL DEFAULT '["bags","bank","posted"]',
    imported_at  TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS inventory_row (
    snapshot_id TEXT NOT NULL,
    location    TEXT NOT NULL,
    item_id     INTEGER NOT NULL,
    item_name   TEXT NOT NULL DEFAULT '',
    quantity    INTEGER NOT NULL,
    PRIMARY KEY (snapshot_id, location, item_id),
    FOREIGN KEY (snapshot_id) REFERENCES inventory_snapshot(snapshot_id)
);

CREATE TABLE IF NOT EXISTS inventory_posted_listing (
    snapshot_id       TEXT NOT NULL,
    auction_id        INTEGER NOT NULL DEFAULT 0,
    item_id           INTEGER NOT NULL,
    item_name         TEXT NOT NULL DEFAULT '',
    quantity          INTEGER NOT NULL DEFAULT 1,
    unit_price_copper INTEGER NOT NULL DEFAULT 0,
    buyout_copper     INTEGER NOT NULL DEFAULT 0,
    time_left         INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (snapshot_id, auction_id, item_id)
);

CREATE TABLE IF NOT EXISTS reconciliation (
    snapshot_id   TEXT NOT NULL,
    location      TEXT NOT NULL,
    item_id       INTEGER NOT NULL,
    item_name     TEXT NOT NULL DEFAULT '',
    prev_qty      INTEGER NOT NULL DEFAULT 0,
    cur_qty       INTEGER NOT NULL DEFAULT 0,
    actual_delta  INTEGER NOT NULL DEFAULT 0,
    expected_delta INTEGER NOT NULL DEFAULT 0,
    residual      INTEGER NOT NULL DEFAULT 0,
    status        TEXT NOT NULL,
    evidence      TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (snapshot_id, location, item_id)
);

CREATE TABLE IF NOT EXISTS realm_model_run (
    run_id       TEXT PRIMARY KEY,
    created_at   TEXT NOT NULL,
    region       TEXT NOT NULL,
    realm        TEXT NOT NULL,
    model_kind   TEXT NOT NULL,
    status       TEXT NOT NULL,
    n_rows       INTEGER NOT NULL DEFAULT 0,
    metrics_json TEXT NOT NULL DEFAULT '{}',
    model_path   TEXT NOT NULL DEFAULT '',
    note         TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS mail_invoice (
    mail_id     TEXT PRIMARY KEY,
    seen_at     INTEGER NOT NULL,
    estimated_sale_at INTEGER NOT NULL DEFAULT 0,
    realm       TEXT NOT NULL DEFAULT '',
    character   TEXT NOT NULL DEFAULT '',
    invoice_type TEXT NOT NULL,
    item_name   TEXT NOT NULL,
    player_name TEXT NOT NULL,
    quantity    INTEGER NOT NULL DEFAULT 1,
    value_copper INTEGER NOT NULL DEFAULT 0,
    buyout_copper INTEGER NOT NULL DEFAULT 0,
    deposit_copper INTEGER NOT NULL DEFAULT 0,
    consignment_copper INTEGER NOT NULL DEFAULT 0,
    days_left   REAL NOT NULL DEFAULT 0,
    subject     TEXT NOT NULL DEFAULT '',
    sender      TEXT NOT NULL DEFAULT '',
    item_link   TEXT NOT NULL DEFAULT '',
    source      TEXT NOT NULL DEFAULT 'mail_invoice',
    confidence  TEXT NOT NULL DEFAULT 'verified',
    imported_at TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_mail_invoice_lookup
    ON mail_invoice(realm, invoice_type, estimated_sale_at);
CREATE INDEX IF NOT EXISTS idx_mail_invoice_facts
    ON mail_invoice(item_name, quantity, value_copper);

CREATE TABLE IF NOT EXISTS undercut_observation (
    observation_id TEXT PRIMARY KEY,
    scan_id        TEXT NOT NULL DEFAULT '',
    observed_at    INTEGER NOT NULL,
    realm          TEXT NOT NULL DEFAULT '',
    character      TEXT NOT NULL DEFAULT '',
    item_id        INTEGER NOT NULL DEFAULT 0,
    item_name      TEXT NOT NULL DEFAULT '',
    item_key       TEXT NOT NULL DEFAULT '',
    item_link      TEXT NOT NULL DEFAULT '',
    own_auction_id INTEGER NOT NULL DEFAULT 0,
    own_quantity   INTEGER NOT NULL DEFAULT 1,
    own_buyout_copper INTEGER NOT NULL DEFAULT 0,
    own_unit_price_copper INTEGER NOT NULL DEFAULT 0,
    lowest_unit_price_copper INTEGER NOT NULL DEFAULT 0,
    lowest_seller  TEXT NOT NULL DEFAULT '',
    lowest_seller_realm TEXT NOT NULL DEFAULT '',
    lowest_auction_id INTEGER NOT NULL DEFAULT 0,
    undercut_copper INTEGER NOT NULL DEFAULT 0,
    undercut_pct   REAL NOT NULL DEFAULT 0,
    items_ahead    INTEGER NOT NULL DEFAULT 0,
    sellers_below  INTEGER NOT NULL DEFAULT 0,
    time_left      INTEGER NOT NULL DEFAULT 0,
    sellers_json   TEXT NOT NULL DEFAULT '[]',
    source         TEXT NOT NULL DEFAULT '',
    confidence     TEXT NOT NULL DEFAULT '',
    reference_status TEXT NOT NULL DEFAULT '',
    imported_at    TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_undercut_realm_time
    ON undercut_observation(realm, observed_at);
CREATE INDEX IF NOT EXISTS idx_undercut_seller
    ON undercut_observation(lowest_seller);

CREATE TABLE IF NOT EXISTS sale_buyer_link (
    sale_event_id TEXT PRIMARY KEY,
    mail_id     TEXT NOT NULL,
    realm       TEXT NOT NULL DEFAULT '',
    buyer       TEXT NOT NULL,
    method      TEXT NOT NULL,
    confidence  TEXT NOT NULL,
    linked_at   TEXT NOT NULL,
    note        TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS mob_observation (
    observation_id TEXT PRIMARY KEY,
    batch_id     TEXT NOT NULL,
    realm        TEXT NOT NULL DEFAULT '',
    character    TEXT NOT NULL DEFAULT '',
    npc_id       INTEGER NOT NULL,
    npc_name     TEXT NOT NULL DEFAULT '',
    kills        INTEGER NOT NULL DEFAULT 0,
    loot_events  INTEGER NOT NULL DEFAULT 0,
    empty_loot_events INTEGER NOT NULL DEFAULT 0,
    money_copper INTEGER NOT NULL DEFAULT 0,
    first_at     INTEGER NOT NULL DEFAULT 0,
    last_at      INTEGER NOT NULL DEFAULT 0,
    source       TEXT NOT NULL DEFAULT 'game_addon',
    imported_at  TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_mob_observation_npc
    ON mob_observation(realm, npc_id, last_at);
CREATE INDEX IF NOT EXISTS idx_mob_observation_batch
    ON mob_observation(batch_id);

CREATE TABLE IF NOT EXISTS mob_loot_observation (
    observation_id TEXT NOT NULL,
    item_id      INTEGER NOT NULL,
    quantity     INTEGER NOT NULL,
    PRIMARY KEY (observation_id, item_id),
    FOREIGN KEY (observation_id) REFERENCES mob_observation(observation_id)
);
CREATE INDEX IF NOT EXISTS idx_mob_loot_item
    ON mob_loot_observation(item_id);

CREATE TABLE IF NOT EXISTS mob_valuation (
    valuation_id TEXT PRIMARY KEY,
    realm        TEXT NOT NULL DEFAULT '',
    npc_id       INTEGER NOT NULL,
    npc_name     TEXT NOT NULL DEFAULT '',
    valued_at    INTEGER NOT NULL,
    price_source TEXT NOT NULL DEFAULT '',
    kills        INTEGER NOT NULL DEFAULT 0,
    money_copper INTEGER NOT NULL DEFAULT 0,
    vendor_copper   INTEGER NOT NULL DEFAULT 0,
    market_copper   INTEGER,
    realized_copper INTEGER,
    gold_per_mob_vendor   REAL,
    gold_per_mob_market   REAL,
    gold_per_mob_realized REAL,
    priced_units INTEGER NOT NULL DEFAULT 0,
    total_units  INTEGER NOT NULL DEFAULT 0,
    stale        INTEGER NOT NULL DEFAULT 0,
    price_age_h  REAL,
    detail_json  TEXT NOT NULL DEFAULT '[]'
);
CREATE INDEX IF NOT EXISTS idx_mob_valuation_realm
    ON mob_valuation(realm, npc_id, valued_at);

CREATE TABLE IF NOT EXISTS import_log (
    kind        TEXT NOT NULL,
    ref         TEXT NOT NULL,
    imported_at TEXT NOT NULL,
    detail      TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (kind, ref)
);
"""


def _ensure_column(con: sqlite3.Connection, table: str, column: str,
                   ddl: str) -> None:
    """Additive migration: an older file gets the column, data is kept."""
    try:
        names = {row[1] for row in con.execute(f"PRAGMA table_info({table})")}
    except sqlite3.OperationalError:
        return
    if names and column not in names:
        con.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")


def connect(db_path: Path | str = DEFAULT_DB) -> sqlite3.Connection:
    """Open (and initialise) the intelligence database."""
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(path))
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.executescript(SCHEMA)
    _ensure_column(con, "inventory_snapshot", "available_json",
                   "TEXT NOT NULL DEFAULT '[\"bags\",\"bank\",\"posted\"]'")
    _ensure_column(con, "undercut_observation", "reference_status",
                   "TEXT NOT NULL DEFAULT ''")
    return con


def memory_connection() -> sqlite3.Connection:
    """A connection for tests: same schema, no file."""
    con = sqlite3.connect(":memory:")
    con.row_factory = sqlite3.Row
    con.executescript(SCHEMA)
    return con
