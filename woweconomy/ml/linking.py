"""Linking listings to their outcomes, with honest censoring.

The Journalator archive records three kinds of event but none of them
carries an auction id:

* a **posting** (the account put an item on the market),
* an **invoice** (the account sold or bought something),
* a **failure** (an auction expired or was cancelled).

A posting therefore has to be matched to its outcome by content: the same
realm, the same character, the same item and the same stack size, with the
outcome happening *after* the posting.

Matching on content alone is not enough.  The same character lists the
same item in the same quantity more than once -- 'Lean Wolf Flank' was
posted at 5500 and at 1800 a unit within hours -- and a matcher that
simply takes the first posting it finds pairs the sale with the wrong
ask.  The account posts **buyout-only** auctions, so the total that
changes hands identifies the listing exactly:

    value == buyout * count

That is used as the primary key of the match, and it is what makes the
link trustworthy.  A failure carries no price, so a failure can only be
matched by position, and the ambiguity of that match is measured.

Two independent checks say the result is sound:

* **the great majority of the matched sales closed at exactly the posted
  buyout** -- a wrong pairing would scatter the prices;
* the **expiry delays cluster on 12 h, 24 h and 48 h**, which are the
  three auction durations WoW offers.  A wrong pairing would smear the
  delays.

Censoring
---------
A listing whose outcome was never observed is **not** an unsold listing: it
is either still on the market or its outcome happened after the archive
ends.  Those rows are marked ``censored`` and never counted as failures.

A **cancellation** is the seller withdrawing the listing.  It is not
evidence of a lack of demand and it is not counted as one either; it is
kept as its own outcome class and excluded from the demand labels.
"""
from __future__ import annotations

import logging
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

from .config import AUCTION_DURATIONS_H
from .journalator import Failure, Invoice, JournalArchive, Posting

logger = logging.getLogger(__name__)

#: outcome classes
SALE = "sale"
EXPIRED = "expired"
CANCELLED = "cancelled"
CENSORED = "censored"


@dataclass
class ListingOutcome:
    """A posting and what became of it."""

    posting: Posting
    outcome: str = CENSORED
    outcome_time: int = 0
    #: seconds from the posting to the outcome
    elapsed: int = 0
    #: realised unit price (gross), 0 when there was no sale
    unit_price: int = 0
    #: house cut actually charged
    consignment: int = 0
    #: deposit actually refunded
    deposit: int = 0
    #: the auction duration inferred from the expiry delay, in hours
    inferred_duration_h: Optional[int] = None
    #: how many postings of the same content were in play
    ambiguous: int = 1
    #: the sale total matched the posting's buyout exactly
    price_exact: bool = False
    #: more than one outstanding posting had the same total, so the
    #: *specific* listing that sold is not identified even though the
    #: price is.  The price label survives; the elapsed time does not.
    price_ambiguous: bool = False

    @property
    def item_id(self) -> Optional[int]:
        return self.posting.item_id

    @property
    def net_unit(self) -> int:
        """What the seller actually pocketed, per unit."""
        if self.outcome != SALE:
            return 0
        total_net = self.unit_price * self.posting.qty - self.consignment + self.deposit
        return total_net // max(1, self.posting.qty)

    @property
    def gross_unit(self) -> int:
        return self.unit_price if self.outcome == SALE else 0


@dataclass
class LinkReport:
    postings: int = 0
    matched_sale: int = 0
    matched_expired: int = 0
    matched_cancelled: int = 0
    censored: int = 0
    unresolved_item: int = 0
    ambiguous_matches: int = 0
    ambiguous_price_matches: int = 0
    sold_at_ask: int = 0
    price_exact_matches: int = 0
    orphan_outcomes: int = 0
    observation_end: int = 0
    notes: List[str] = field(default_factory=list)

    def summary(self) -> Dict[str, object]:
        matched = self.matched_sale + self.matched_expired + self.matched_cancelled
        return {
            "postings": self.postings,
            "matched_sale": self.matched_sale,
            "matched_expired": self.matched_expired,
            "matched_cancelled": self.matched_cancelled,
            "censored": self.censored,
            "unresolved_item": self.unresolved_item,
            "ambiguous_matches": self.ambiguous_matches,
            "sold_at_ask": self.sold_at_ask,
            "sold_at_ask_rate": round(self.sold_at_ask / self.matched_sale, 4)
            if self.matched_sale else None,
            "price_exact_matches": self.price_exact_matches,
            "price_exact_rate": round(self.price_exact_matches / self.matched_sale, 4)
            if self.matched_sale else None,
            "ambiguous_price_matches": self.ambiguous_price_matches,
            "ambiguous_price_rate": round(
                self.ambiguous_price_matches / self.matched_sale, 4)
            if self.matched_sale else None,
            "orphan_outcomes": self.orphan_outcomes,
            "observation_end": self.observation_end,
            "match_rate": round(matched / self.postings, 4) if self.postings else None,
        }


def _key(realm: str, character: str, item_id: Optional[int], qty: int):
    return (realm, character, item_id, int(qty))


def infer_duration_h(elapsed_seconds: int) -> Optional[int]:
    """The auction duration a given expiry delay implies.

    The delay is the duration plus the time the character needed to open
    the mail, so the duration is the largest standard duration that is not
    larger than the delay.  ``None`` when the delay is below the shortest
    auction, which means the mail arrived essentially at once.
    """
    hours = elapsed_seconds / 3600.0
    best: Optional[int] = None
    for dur in sorted(AUCTION_DURATIONS_H):
        if hours >= dur - 0.5:
            best = dur
    return best


@dataclass
class _Group:
    """Everything that happened to one item, in one stack size, for one account."""

    postings: List[Posting] = field(default_factory=list)
    sales: List[Invoice] = field(default_factory=list)
    failures: List[Failure] = field(default_factory=list)


def link_postings(archive: JournalArchive,
                  observation_end: Optional[int] = None) -> Tuple[List[ListingOutcome], LinkReport]:
    """Match every posting to at most one outcome.

    Outcomes are consumed in time order.  A sale is matched to a posting
    whose total equals the amount that changed hands, when one exists;
    that is an exact identification.  Only when no such posting exists
    does the matcher fall back on the most recent listing outstanding,
    and the match is then marked as inexact.
    """
    report = LinkReport()
    if observation_end is None:
        observation_end = archive.last_ts
    report.observation_end = observation_end

    groups: Dict[tuple, _Group] = defaultdict(_Group)
    for post in archive.postings:
        if post.item_id is None:
            report.unresolved_item += 1
            continue
        groups[_key(post.realm, post.character, post.item_id, post.qty)].postings.append(post)
    for inv in archive.invoices:
        if inv.invoice_type != "seller" or inv.item_id is None:
            continue
        groups[_key(inv.realm, inv.character, inv.item_id, inv.qty)].sales.append(inv)
    for fail in archive.failures:
        if fail.item_id is None:
            continue
        groups[_key(fail.realm, fail.character, fail.item_id, fail.qty)].failures.append(fail)

    outcomes: Dict[int, ListingOutcome] = {}
    report.postings = sum(len(g.postings) for g in groups.values()) + report.unresolved_item
    used_outcomes = 0
    total_outcomes = 0

    for key, group in groups.items():
        group.postings.sort(key=lambda p: p.time)
        # the postings a sale can still claim
        available = list(group.postings)
        # outcomes in time order
        events: List[Tuple[int, int, str, object]] = []
        for inv in group.sales:
            events.append((inv.time, 0, "sale", inv))
        for fail in group.failures:
            events.append((fail.time, 1, "failure", fail))
        events.sort(key=lambda e: (e[0], e[1]))
        total_outcomes += len(events)

        for when, _, kind, payload in events:
            # postings that existed at that moment
            candidates = [p for p in available if p.time <= when]
            if not candidates:
                continue
            chosen: Optional[Posting] = None
            exact = False
            price_ambiguous = False
            if kind == "sale":
                total = int(payload.value)
                matches = [p for p in candidates
                           if p.unit_buyout and p.unit_buyout * p.qty == total]
                if matches:
                    chosen = matches[-1]      # the most recent exact listing
                    # Two outstanding postings with the same total are not
                    # the same evidence: the price is identified, the
                    # *listing* is not.  Saying "exact" here would claim a
                    # precision the data does not have, and the elapsed
                    # time would then be attached to an arbitrary posting.
                    exact = len(matches) == 1
                    price_ambiguous = len(matches) > 1
            if chosen is None:
                chosen = candidates[-1]      # the most recent listing outstanding
            available.remove(chosen)
            used_outcomes += 1

            if kind == "sale":
                out = ListingOutcome(posting=chosen, outcome=SALE,
                                     outcome_time=when, elapsed=when - chosen.time,
                                     price_exact=exact,
                                     price_ambiguous=price_ambiguous)
                out.unit_price = int(payload.value) // max(1, chosen.qty)
                out.consignment = int(payload.consignment)
                out.deposit = int(payload.deposit)
                report.matched_sale += 1
                if exact:
                    report.price_exact_matches += 1
                if price_ambiguous:
                    report.ambiguous_price_matches += 1
                if chosen.unit_buyout > 0 and abs(out.unit_price - chosen.unit_buyout) <= 1:
                    report.sold_at_ask += 1
            else:
                failed_type = str(payload.failed_type)
                out = ListingOutcome(posting=chosen,
                                     outcome=CANCELLED if failed_type == "cancelled" else EXPIRED,
                                     outcome_time=when, elapsed=when - chosen.time)
                out.inferred_duration_h = infer_duration_h(out.elapsed)
                if out.outcome == EXPIRED:
                    report.matched_expired += 1
                else:
                    report.matched_cancelled += 1

            out.ambiguous = len(candidates)
            if out.ambiguous > 1:
                report.ambiguous_matches += 1
            outcomes[id(chosen)] = out

    # what is left of every posting had no observed outcome
    for group in groups.values():
        for post in group.postings:
            if id(post) in outcomes:
                continue
            out = ListingOutcome(posting=post, outcome=CENSORED)
            outcomes[id(post)] = out
            report.censored += 1

    report.orphan_outcomes = total_outcomes - used_outcomes
    ordered = sorted(outcomes.values(), key=lambda o: (o.posting.time, o.posting.item_id or 0))
    return ordered, report
