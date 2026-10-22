"""
SQLAlchemy ORM models for the TSM Market Snapshot database.
"""

import math
from datetime import datetime, timezone

from sqlalchemy import (
    Column, Integer, Float, Text, ForeignKey, UniqueConstraint, Index,
    create_engine, event, PrimaryKeyConstraint,
)
from sqlalchemy.orm import declarative_base, relationship, sessionmaker

Base = declarative_base()


class Item(Base):
    __tablename__ = "items"

    itemId    = Column(Integer, primary_key=True, autoincrement=False)
    name      = Column(Text, nullable=False)
    createdAt = Column(Text, nullable=False, default=lambda: datetime.now(timezone.utc).isoformat())

    snapshots = relationship("MarketSnapshot", back_populates="item")
    analytics = relationship("ItemAnalytic", back_populates="item")


class Snapshot(Base):
    __tablename__ = "snapshots"

    snapshotId        = Column(Integer, primary_key=True)
    snapshotTimestamp = Column(Text, nullable=False, index=True)
    importFileName    = Column(Text)
    rowCount          = Column(Integer, nullable=False, default=0)
    checksum          = Column(Text, unique=True)  # idempotency
    importedAt        = Column(Text, nullable=False,
                               default=lambda: datetime.now(timezone.utc).isoformat())

    market_data = relationship("MarketSnapshot", back_populates="snapshot",
                               cascade="all, delete-orphan")
    analytics   = relationship("ItemAnalytic", back_populates="snapshot",
                               cascade="all, delete-orphan")

    __table_args__ = (
        Index("idx_snapshots_imported", importedAt.desc()),
    )


class MarketSnapshot(Base):
    __tablename__ = "market_snapshots"

    snapshotId   = Column(Integer, ForeignKey("snapshots.snapshotId", ondelete="CASCADE"),
                          primary_key=True)
    itemId       = Column(Integer, ForeignKey("items.itemId"), primary_key=True)
    marketValue  = Column(Float, nullable=False, default=0)
    historical   = Column(Float, nullable=False, default=0)
    avgSalePrice = Column(Float, nullable=False, default=0)
    saleRate     = Column(Float, nullable=False, default=0)
    soldPerDay   = Column(Float, nullable=False, default=0)

    snapshot = relationship("Snapshot", back_populates="market_data")
    item     = relationship("Item", back_populates="snapshots")

    __table_args__ = (
        Index("idx_market_snapshots_itemId", itemId),
        Index("idx_market_item_time", itemId, snapshotId),
    )


class ItemAnalytic(Base):
    """Pre-computed per-item metrics for fast querying."""
    __tablename__ = "item_analytics"

    snapshotId     = Column(Integer, ForeignKey("snapshots.snapshotId", ondelete="CASCADE"),
                            primary_key=True)
    itemId         = Column(Integer, ForeignKey("items.itemId"), primary_key=True)
    farmScore      = Column(Float, nullable=False, default=0)
    goldThroughput = Column(Float, nullable=False, default=0)
    priceTrend     = Column(Float, nullable=False, default=1.0)
    liquidity      = Column(Float, nullable=False, default=0)

    snapshot = relationship("Snapshot", back_populates="analytics")
    item     = relationship("Item", back_populates="analytics")

    __table_args__ = (
        Index("idx_analytics_snapshot", snapshotId, farmScore.desc()),
        Index("idx_analytics_item", itemId, snapshotId),
    )


# ── Helpers ──

def compute_analytics(market: MarketSnapshot) -> dict:
    """Compute FarmScore and derived metrics for a market snapshot row."""
    mv   = market.marketValue or 0
    hist = market.historical or 0
    asp  = market.avgSalePrice or 0
    sr   = market.saleRate or 0
    spd  = market.soldPerDay or 0

    # Log-based liquidity
    liq = math.log(1 + spd) * math.log(2 + sr * 100) if spd > 0 and sr > 0 else 0

    # Gold throughput
    throughput = spd * asp

    # Price trend
    trend = mv / hist if hist > 0 else 1.0
    trend = max(0.5, min(5.0, trend))

    # FarmScore (simplified – real scoring uses all items for normalisation)
    # Stored as raw score for later percentile ranking
    score = liq  # placeholder; full normalisation happens at query time

    return {
        "farmScore": score,
        "goldThroughput": throughput,
        "priceTrend": trend,
        "liquidity": liq,
    }


def init_db(db_url: str = "sqlite:///tsm_markets.db"):
    """Create engine, apply schema, return Session class."""
    engine = create_engine(db_url, echo=False)

    # Enable WAL mode for better concurrent reads
    @event.listens_for(engine, "connect")
    def set_sqlite_pragma(dbapi_connection, connection_record):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA cache_size=-80000")  # 80 MB
        cursor.close()

    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    return engine, Session
