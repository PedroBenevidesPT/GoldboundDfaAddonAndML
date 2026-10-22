"""Alembic migration script: create initial schema.

Revision ID: 001_initial
Revises:
Create Date: 2026-07-28
"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa


revision: str = "001_initial"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Items
    op.create_table(
        "items",
        sa.Column("itemId", sa.Integer(), autoincrement=False, nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("createdAt", sa.Text(), nullable=False,
                  server_default=sa.text("(datetime('now'))")),
        sa.PrimaryKeyConstraint("itemId"),
    )

    # Snapshots
    op.create_table(
        "snapshots",
        sa.Column("snapshotId", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("snapshotTimestamp", sa.Text(), nullable=False),
        sa.Column("importFileName", sa.Text(), nullable=True),
        sa.Column("rowCount", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("checksum", sa.Text(), nullable=True, unique=True),
        sa.Column("importedAt", sa.Text(), nullable=False,
                  server_default=sa.text("(datetime('now'))")),
        sa.PrimaryKeyConstraint("snapshotId"),
    )

    # Market snapshots
    op.create_table(
        "market_snapshots",
        sa.Column("snapshotId", sa.Integer(), nullable=False),
        sa.Column("itemId", sa.Integer(), nullable=False),
        sa.Column("marketValue", sa.Float(), nullable=False, server_default="0"),
        sa.Column("historical", sa.Float(), nullable=False, server_default="0"),
        sa.Column("avgSalePrice", sa.Float(), nullable=False, server_default="0"),
        sa.Column("saleRate", sa.Float(), nullable=False, server_default="0"),
        sa.Column("soldPerDay", sa.Float(), nullable=False, server_default="0"),
        sa.ForeignKeyConstraint(["snapshotId"], ["snapshots.snapshotId"],
                                ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["itemId"], ["items.itemId"]),
        sa.PrimaryKeyConstraint("snapshotId", "itemId"),
    )

    # Item analytics
    op.create_table(
        "item_analytics",
        sa.Column("snapshotId", sa.Integer(), nullable=False),
        sa.Column("itemId", sa.Integer(), nullable=False),
        sa.Column("farmScore", sa.Float(), nullable=False, server_default="0"),
        sa.Column("goldThroughput", sa.Float(), nullable=False, server_default="0"),
        sa.Column("priceTrend", sa.Float(), nullable=False, server_default="1.0"),
        sa.Column("liquidity", sa.Float(), nullable=False, server_default="0"),
        sa.ForeignKeyConstraint(["snapshotId"], ["snapshots.snapshotId"],
                                ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["itemId"], ["items.itemId"]),
        sa.PrimaryKeyConstraint("snapshotId", "itemId"),
    )

    # Category lookup
    op.create_table(
        "item_categories",
        sa.Column("itemId", sa.Integer(), nullable=False),
        sa.Column("category", sa.Text(), nullable=False, server_default="Other"),
        sa.Column("subcategory", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(["itemId"], ["items.itemId"]),
        sa.PrimaryKeyConstraint("itemId"),
    )

    # Indexes
    op.create_index("idx_market_snapshots_itemId", "market_snapshots", ["itemId"])
    op.create_index("idx_snapshots_timestamp", "snapshots", ["snapshotTimestamp"])
    op.create_index("idx_market_item_time", "market_snapshots", ["itemId", "snapshotId"])
    op.create_index("idx_analytics_snapshot", "item_analytics",
                    ["snapshotId", sa.text("farmScore DESC")])
    op.create_index("idx_analytics_item", "item_analytics", ["itemId", "snapshotId"])
    op.create_index("idx_snapshots_imported", "snapshots", [sa.text("importedAt DESC")])


def downgrade() -> None:
    op.drop_table("item_categories")
    op.drop_table("item_analytics")
    op.drop_table("market_snapshots")
    op.drop_table("snapshots")
    op.drop_table("items")
