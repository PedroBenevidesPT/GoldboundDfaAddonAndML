"""Auction-house intelligence built on top of the existing engine.

This package is deliberately additive: it reads the journal and the TSM
mirror that :mod:`woweconomy.ml` already uses, and it never rewrites a
table that the existing pipeline owns.

The five jobs, and the honesty rule each one follows:

``ah``
    Blizzard Auction House snapshots.  Every row keeps the raw
    ``time_left`` string the API sent; an unknown category is kept and
    flagged, never mapped to a guess.  A listing that disappeared between
    two snapshots is a departure, **not** a sale.

``inventory``
    Reconciles bags, bank and posted stock against the confirmed journal
    events.  A quantity that moved without an event is reported as
    ``unexplained`` -- never promoted to a sale.

``sellers``
    Observed seller names (addon tooltip / auction-info capture) and their
    correlation with our own confirmed sales.  Confirmed facts and
    probabilistic inferences are returned in separate lists.

``realm_ml`` / ``dynamics``
    One model and one metric set per (region, realm).  Nothing is pooled;
    a shared prior is only reported after it wins on the realm's own
    out-of-sample window.

``farming``
    A farm recommendation that starts from the price a buyer actually
    paid, then discounts by measured liquidity and realm competition --
    never from an asking-price reference sold as a realizable return.
"""
from __future__ import annotations

__all__ = ["store", "ah", "inventory", "sellers", "realm_ml", "dynamics",
           "farming", "ledger"]
