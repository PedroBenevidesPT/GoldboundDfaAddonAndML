"""TOP ITENS (most expensive) regression tests."""
import pytest

from woweconomy.analytics.farm_ranking import (
    ItemMarket, NpcDrop, top_expensive,
)


def _drop(npc=1, item=1, rate=0.5, name="X"):
    return NpcDrop(npc, "Mob", 331, "ash", "drops", item, name, rate, 1.0, "")


def test_top_expensive_price_rank():
    mkt = {1: ItemMarket(1, "Cheap", 1000.0, 0.5, 50.0),
           2: ItemMarket(2, "Pricey", 999999.0, 0.01, 1.0),
           3: ItemMarket(3, "NoPrice", 0.0, 0.5, 50.0)}
    drops = [_drop(item=1, rate=0.9), _drop(item=2, rate=0.001),
             _drop(item=3, rate=0.9)]
    top = top_expensive(drops, mkt)
    assert [t.item_id for t in top] == [2, 1]  # price desc, unpriced out
    assert top[0].npc_id == 1 and top[0].sale_rate == 0.01


def test_top_expensive_best_source():
    mkt = {9: ItemMarket(9, "Same", 5000.0, 0.1, 5.0)}
    drops = [_drop(npc=1, item=9, rate=0.01), _drop(npc=2, item=9, rate=0.09)]
    top = top_expensive(drops, mkt)
    assert len(top) == 1 and top[0].npc_id == 2  # best rate wins the row
