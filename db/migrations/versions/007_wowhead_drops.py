"""Wowhead zone drops in the shared database.

Revision ID: 007_wowhead_drops
Revises: 006_relist_decision
Create Date: 2026-10-23

The Wowhead scraper used to stop at ``woweconomy/wowhead_zones/zone_<id>.json``.
Those files were a second source of truth next to Postgres: the farm ranking
and the importer read them, while the database every other consumer uses knew
nothing about the drops.  These tables are the canonical rows; the JSON stays
as the raw artefact a killed scrape leaves behind.

``wowhead_zone`` is one row per scraped zone (when, how many NPCs, from where);
``wowhead_drop`` is one row per (zone, NPC, item).  The primary key is what
lets a rescan replace a zone without inventing duplicate drops.
"""
from typing import Sequence, Union

from alembic import op

revision: str = "007_wowhead_drops"
down_revision: Union[str, None] = "006_relist_decision"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE IF NOT EXISTS wowhead_zone (
            zone_id     BIGINT NOT NULL,
            zone_name   TEXT   NOT NULL DEFAULT '',
            npc_total   BIGINT NOT NULL DEFAULT 0,
            scraped_at  BIGINT NOT NULL DEFAULT 0,
            source      TEXT   NOT NULL DEFAULT 'wowhead',
            PRIMARY KEY (zone_id)
        )
    """)
    op.execute("""
        CREATE TABLE IF NOT EXISTS wowhead_drop (
            zone_id     BIGINT NOT NULL,
            npc_id      BIGINT NOT NULL,
            item_id     BIGINT NOT NULL,
            zone_name   TEXT   NOT NULL DEFAULT '',
            npc_name    TEXT   NOT NULL DEFAULT '',
            category    TEXT   NOT NULL DEFAULT '',
            item_name   TEXT   NOT NULL DEFAULT '',
            item_type   TEXT   NOT NULL DEFAULT '',
            drop_rate   DOUBLE PRECISION NOT NULL DEFAULT 0,
            qty         DOUBLE PRECISION NOT NULL DEFAULT 1,
            spawns      BIGINT NOT NULL DEFAULT 0,
            source_url  TEXT   NOT NULL DEFAULT '',
            scraped_at  BIGINT NOT NULL DEFAULT 0,
            PRIMARY KEY (zone_id, npc_id, item_id)
        )
    """)
    op.execute("""
        CREATE INDEX IF NOT EXISTS idx_wowhead_drop_item
            ON wowhead_drop (item_id)
    """)
    op.execute("""
        CREATE INDEX IF NOT EXISTS idx_wowhead_drop_npc
            ON wowhead_drop (npc_id)
    """)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS wowhead_drop")
    op.execute("DROP TABLE IF EXISTS wowhead_zone")
