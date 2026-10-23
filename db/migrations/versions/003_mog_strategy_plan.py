"""The mog strategy plan table.

Revision ID: 003_mog_strategy_plan
Revises: 002_baseline
Create Date: 2026-10-23

``mog_strategy`` could compute a posting plan but nothing stored one, so the
answer to "should this be posted, and at what price" existed only for code that
imported the library.  This table is where the pipeline keeps the current plan
so the addon can read it at the auction house.

The table holds the *current* plan, not a history: a row that is no longer
planned is removed when the plan is rebuilt, because a stale recommendation is
worse than none.  History belongs in the evaluation tables.
"""
from typing import Sequence, Union

from alembic import op


revision: str = "003_mog_strategy_plan"
down_revision: Union[str, None] = "002_baseline"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE IF NOT EXISTS mog_strategy_plan (
            item_id             BIGINT  NOT NULL,
            built_at            TEXT    NOT NULL,
            as_of_snapshot      TEXT,
            name                TEXT    NOT NULL DEFAULT '',
            category            TEXT    NOT NULL DEFAULT 'unknown',
            action              TEXT    NOT NULL,
            price_unit_copper   DOUBLE PRECISION,
            estimate_copper     DOUBLE PRECISION,
            expected_net_copper DOUBLE PRECISION,
            p_sale              DOUBLE PRECISION,
            p_sale_source       TEXT    NOT NULL DEFAULT 'unavailable',
            horizon_days        DOUBLE PRECISION NOT NULL DEFAULT 30.0,
            confidence          TEXT    NOT NULL DEFAULT 'none',
            observations        BIGINT  NOT NULL DEFAULT 0,
            competition_copper  DOUBLE PRECISION,
            competition_count   BIGINT  NOT NULL DEFAULT 0,
            vendor_copper       DOUBLE PRECISION,
            reason              TEXT    NOT NULL DEFAULT '',
            flags               TEXT    NOT NULL DEFAULT '',
            PRIMARY KEY (item_id)
        )
    """)
    op.execute("""
        CREATE INDEX IF NOT EXISTS ix_mog_strategy_action
            ON mog_strategy_plan (action)
    """)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS mog_strategy_plan")
