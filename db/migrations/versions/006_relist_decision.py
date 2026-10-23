"""The relist decision ledger.

Revision ID: 006_relist_decision
Revises: 005_identity_keys
Create Date: 2026-10-23

``relist.advise`` ranks open listings today and forgets the ranking on the next
scan, so there is no way to ask later whether the advice worked.  This table
records each recommendation with its inputs (append-only in spirit: a later
scan supersedes the previous row, and an outcome is written once) and attaches
the confirmed journal event afterwards.

The table was first added to ``woweconomy/intel/store.py``'s SQLite schema,
which also ran on Postgres and quietly created it outside Alembic.  This
revision makes Alembic the owner: the SQL is ``IF NOT EXISTS`` so an existing
database is upgraded without touching its rows, while a fresh database built
from ``002_baseline`` gets the table it was missing.
"""
from typing import Sequence, Union

from alembic import op

revision: str = "006_relist_decision"
down_revision: Union[str, None] = "005_identity_keys"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE IF NOT EXISTS relist_decision (
            decision_id            TEXT    NOT NULL,
            decided_at             BIGINT  NOT NULL,
            observed_at            BIGINT  NOT NULL DEFAULT 0,
            realm                  TEXT    NOT NULL DEFAULT '',
            character              TEXT    NOT NULL DEFAULT '',
            scan_id                TEXT    NOT NULL DEFAULT '',
            own_auction_id         BIGINT  NOT NULL DEFAULT 0,
            item_id                BIGINT  NOT NULL DEFAULT 0,
            item_name              TEXT    NOT NULL DEFAULT '',
            advice                 TEXT    NOT NULL,
            reason                 TEXT    NOT NULL DEFAULT '',
            reference_status       TEXT    NOT NULL DEFAULT '',
            relation               TEXT    NOT NULL DEFAULT '',
            own_unit_price_copper  BIGINT  NOT NULL DEFAULT 0,
            lowest_unit_price_copper BIGINT NOT NULL DEFAULT 0,
            own_quantity           BIGINT  NOT NULL DEFAULT 1,
            gap_pct                DOUBLE PRECISION NOT NULL DEFAULT 0,
            items_ahead            BIGINT  NOT NULL DEFAULT 0,
            hours_left             DOUBLE PRECISION,
            suggested_ask_copper   BIGINT,
            market_json            TEXT    NOT NULL DEFAULT '',
            review_required        BIGINT  NOT NULL DEFAULT 0,
            superseded             BIGINT  NOT NULL DEFAULT 0,
            outcome                TEXT    NOT NULL DEFAULT 'open',
            outcome_at             BIGINT,
            outcome_source         TEXT    NOT NULL DEFAULT '',
            outcome_price_copper   BIGINT,
            outcome_qty            BIGINT,
            realised_copper        BIGINT,
            evidence               TEXT    NOT NULL DEFAULT '',
            PRIMARY KEY (decision_id)
        )
    """)
    op.execute("""
        CREATE INDEX IF NOT EXISTS idx_relist_decision_realm
            ON relist_decision (realm, decided_at)
    """)
    op.execute("""
        CREATE INDEX IF NOT EXISTS idx_relist_decision_outcome
            ON relist_decision (outcome, decided_at)
    """)
    op.execute("""
        CREATE INDEX IF NOT EXISTS idx_relist_decision_auction
            ON relist_decision (realm, own_auction_id)
    """)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS relist_decision")
