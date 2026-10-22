-- ============================
-- WoW TSM Market Snapshot DB
-- SQLite Schema v1
-- ============================

-- Items lookup table (normalized)
CREATE TABLE IF NOT EXISTS items (
    itemId      INTEGER NOT NULL PRIMARY KEY,
    name        TEXT    NOT NULL,
    created_at  TEXT    NOT NULL DEFAULT (datetime('now'))
);

-- Snapshots: one per CSV import
CREATE TABLE IF NOT EXISTS snapshots (
    snapshotId        INTEGER NOT NULL PRIMARY KEY AUTOINCREMENT,
    snapshotTimestamp TEXT    NOT NULL,              -- taken from CSV updatedAt
    importFileName    TEXT,                          -- original filename for auditing
    rowCount          INTEGER NOT NULL DEFAULT 0,    -- sanity check
    checksum          TEXT,                          -- SHA256 of raw CSV content
    importedAt        TEXT    NOT NULL DEFAULT (datetime('now')),
    UNIQUE(checksum)                                -- idempotency guard
);

-- Market data: one row per item per snapshot
CREATE TABLE IF NOT EXISTS market_snapshots (
    snapshotId    INTEGER NOT NULL REFERENCES snapshots(snapshotId) ON DELETE CASCADE,
    itemId        INTEGER NOT NULL REFERENCES items(itemId),
    marketValue   REAL    NOT NULL DEFAULT 0,
    historical    REAL    NOT NULL DEFAULT 0,
    avgSalePrice  REAL    NOT NULL DEFAULT 0,
    saleRate      REAL    NOT NULL DEFAULT 0,
    soldPerDay    REAL    NOT NULL DEFAULT 0,
    PRIMARY KEY (snapshotId, itemId)
) WITHOUT ROWID;  -- saves space, uses PK as clustered index

-- ── Derived analytics table (materialised for speed) ──
-- Populated by the import script so we don't recompute every time.
CREATE TABLE IF NOT EXISTS item_analytics (
    snapshotId    INTEGER NOT NULL REFERENCES snapshots(snapshotId) ON DELETE CASCADE,
    itemId        INTEGER NOT NULL REFERENCES items(itemId),
    farmScore     REAL    NOT NULL DEFAULT 0,
    goldThroughput REAL   NOT NULL DEFAULT 0,   -- soldPerDay * avgSalePrice
    priceTrend    REAL    NOT NULL DEFAULT 1.0, -- marketValue / historical
    liquidity     REAL    NOT NULL DEFAULT 0,
    PRIMARY KEY (snapshotId, itemId)
) WITHOUT ROWID;

-- ── Category lookup (optional, kept simple) ──
CREATE TABLE IF NOT EXISTS item_categories (
    itemId     INTEGER NOT NULL PRIMARY KEY REFERENCES items(itemId),
    category   TEXT    NOT NULL DEFAULT 'Other',
    subcategory TEXT
);

-- ── Indexes ──
-- Speed up item lookups in market_snapshots
CREATE INDEX IF NOT EXISTS idx_market_snapshots_itemId
    ON market_snapshots(itemId);

-- Speed up snapshot-range queries (time series)
CREATE INDEX IF NOT EXISTS idx_snapshots_timestamp
    ON snapshots(snapshotTimestamp);

-- Composite index for trend queries: price history of one item over time
CREATE INDEX IF NOT EXISTS idx_market_item_time
    ON market_snapshots(itemId, snapshotId);

-- Analytics lookups
CREATE INDEX IF NOT EXISTS idx_analytics_snapshot
    ON item_analytics(snapshotId, farmScore DESC);

CREATE INDEX IF NOT EXISTS idx_analytics_item
    ON item_analytics(itemId, snapshotId);

-- Fast "latest snapshot" queries
CREATE INDEX IF NOT EXISTS idx_snapshots_imported
    ON snapshots(importedAt DESC);
