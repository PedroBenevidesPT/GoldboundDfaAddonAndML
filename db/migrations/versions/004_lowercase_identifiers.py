"""Lower-case the mixed-case column names the SQLite import carried over.

Revision ID: 004_lowercase_identifiers
Revises: 003_mog_strategy_plan

Why this is a migration and not a rename in the import script
-------------------------------------------------------------
The source is SQLite, which matches identifiers case-insensitively, so the
original schema was free to call a column ``itemId``.  Postgres folds every
*unquoted* identifier to lower case, so ``SELECT itemId`` looks for ``itemid``
and fails against a column actually named ``"itemId"``.  Every reader written
against SQLite -- which is all of them -- writes the name unquoted, so keeping
the camelCase spelling would mean quoting 32 column names at every call site,
forever, in both backends.

Lower-casing the Postgres side instead makes the two backends agree on the one
form the code already uses: SQLite still answers to ``itemId`` because it is
case-insensitive, Postgres answers to it because the column *is* ``itemid``.
``Row`` was made case-insensitive at the same time so name-based access works
whichever spelling a caller passes.

The renames are listed explicitly rather than discovered from the catalog: a
migration that rewrites whatever it happens to find is not reproducible, and
this list is the complete set (verified: 32 columns, no lower-case collision).
"""
from __future__ import annotations

from alembic import op

revision = "004_lowercase_identifiers"
down_revision = "003_mog_strategy_plan"
branch_labels = None
depends_on = None


#: (table, column) for every identifier that is not already lower case.
_RENAMES = (
    ("blizzard_items", "fetchedAt"),
    ("blizzard_items", "itemId"),
    ("blizzard_items", "sellPrice"),
    ("blizzard_missing", "failedAt"),
    ("blizzard_missing", "itemId"),
    ("db2_item_appearance", "appearanceId"),
    ("db2_item_appearance", "itemId"),
    ("db2_items", "itemId"),
    ("db2_items", "itemLevel"),
    ("db2_items", "requiredLevel"),
    ("item_analytics", "farmScore"),
    ("item_analytics", "goldThroughput"),
    ("item_analytics", "itemId"),
    ("item_analytics", "priceTrend"),
    ("item_analytics", "snapshotId"),
    ("items", "createdAt"),
    ("items", "itemId"),
    ("journal", "itemId"),
    ("journal_stats", "itemId"),
    ("market_snapshots", "avgSalePrice"),
    ("market_snapshots", "itemId"),
    ("market_snapshots", "marketValue"),
    ("market_snapshots", "saleRate"),
    ("market_snapshots", "snapshotId"),
    ("market_snapshots", "soldPerDay"),
    ("snapshots", "importFileName"),
    ("snapshots", "importedAt"),
    ("snapshots", "rowCount"),
    ("snapshots", "snapshotId"),
    ("snapshots", "snapshotTimestamp"),
    ("tsm_market", "itemId"),
    ("tsm_region", "itemId"),
)


def upgrade() -> None:
    for table, column in _RENAMES:
        # RENAME COLUMN keeps indexes, constraints and defaults attached, so
        # nothing downstream has to be rebuilt.
        op.execute(
            f'ALTER TABLE "{table}" RENAME COLUMN "{column}" TO {column.lower()}'
        )


def downgrade() -> None:
    """Restore the camelCase spelling.

    Provided because a rename is reversible and it costs one statement per
    column.  Nothing else in this project downgrades; this one can, so it does.
    """
    for table, column in _RENAMES:
        op.execute(
            f'ALTER TABLE "{table}" RENAME COLUMN {column.lower()} TO "{column}"'
        )
