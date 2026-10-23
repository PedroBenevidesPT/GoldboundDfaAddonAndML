"""The TSM auctioning strings, as something the engine can evaluate.

The three strings (Minimum / Normal / Maximum) are the listing policy: what
the player posts, what the wall is, and what the floor is.  They live in TSM,
which means the training side cannot read them -- and a policy the model
never sees is a policy the model cannot be honest about.

So the strings are *transcribed* here, with TSM's documented semantics, and
evaluated against the same references the dataset ships:

    normalmarket = max(min(dbmarket, 125% dbregionmarketavg),
                       85% dbregionmarketavg)

    Minimum = max(60% normalmarket, 2 * vendorsell)

    Normal  = ifgt(normalmarket, 50000g,
                    max(70% normalmarket, (1 - numexpires/100) * normalmarket),
                    ifgt(dbregionsalerate, 0.015, normalmarket,
                      ifgt(dbregionsalerate, 0.01, 90% normalmarket,
                        ifgt(dbregionsalerate, 0.005, 80% normalmarket,
                          60% normalmarket))))

    Maximum = ifgt(normalmarket, 50000g,
                    max(70% normalmarket, (1 - numexpires/100) * normalmarket),
                    150% normalmarket)

What ``normalmarket`` means: the local ask, capped by the regional ask, and
never allowed to fall below 85 % of the regional one.  It is a *clamp*, not a
blend -- a local market that has run away is pulled back down, and a local
market with nothing in it is lifted to a defensible price.

Semantics (the ones TSM documents, and the ones that matter here):

* a source with no data is **invalid**, not zero;
* ``avg``/``max``/``min``/``first`` skip the invalid arguments, and all
  invalid means invalid;
* arithmetic with an invalid operand is invalid;
* ``ifgt``/``iflt`` with an invalid *comparison* is invalid -- it does not
  fall back to the else branch, because "I do not know" is not "no";
* ``X% expr`` is ``X/100 * expr``; ``1g`` is 10 000 copper.

``numexpires`` is the one input the archive does not have: TSM keeps it in
its own accounting, per item, and it is a count of expired auctions since the
last sale.  It defaults to 0, which is the *most expensive* reading of the
string (a fresh item), and it only matters above 50 000 g.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Dict, Mapping, Optional

#: TSM's "no data" -- distinct from zero, and it propagates.
INVALID: None = None

COPPER_PER_GOLD = 10_000

STRING_NORMALMARKET = "max(min(dbmarket, 125%dbregionmarketavg), 85%dbregionmarketavg)"

STRING_MINIMUM = "max(60% normalmarket, 2 * vendorsell)"

STRING_NORMAL = (
    "ifgt(normalmarket, 50000g, "
    "max(70% normalmarket, (1 - numexpires/100) * normalmarket), "
    "ifgt(dbregionsalerate, 0.015, normalmarket, "
    "ifgt(dbregionsalerate, 0.01, 90% normalmarket, "
    "ifgt(dbregionsalerate, 0.005, 80% normalmarket, 60% normalmarket))))"
)

STRING_MAXIMUM = (
    "ifgt(normalmarket, 50000g, "
    "max(70% normalmarket, (1 - numexpires/100) * normalmarket), "
    "150% normalmarket)"
)

#: the sale rate below which the string starts discounting, and by how much.
SALE_RATE_STEPS = ((0.015, 1.00), (0.01, 0.90), (0.005, 0.80), (0.0, 0.60))

#: above this, an item is priced off its expiry count instead of its sale rate.
EXPENSIVE_ITEM_COPPER = 50_000 * COPPER_PER_GOLD


# --------------------------------------------------------------------------- #
# the evaluator
# --------------------------------------------------------------------------- #

_TOKEN = re.compile(
    r"\s*(?:(?P<num>\d+(?:\.\d+)?)\s*(?P<unit>[gsc])?"
    r"|(?P<id>[a-zA-Z_]+)"
    r"|(?P<op>[-+*/%(),]))"
)
_UNITS = {"g": COPPER_PER_GOLD, "s": 100, "c": 1}


def _tokenize(text: str) -> list[tuple[str, object]]:
    out: list[tuple[str, object]] = []
    index = 0
    while index < len(text):
        match = _TOKEN.match(text, index)
        if not match:
            raise ValueError(f"invalid token in TSM string at {text[index:index + 20]!r}")
        index = match.end()
        if match.group("num") is not None:
            unit = match.group("unit") or "c"
            out.append(("num", float(match.group("num")) * _UNITS[unit]))
        else:
            out.append((match.lastgroup or "", match.group(match.lastgroup or "")))
    return out


def _present(*values: Optional[float]) -> list[float]:
    return [v for v in values if v is not INVALID]


def _avg(*xs: Optional[float]) -> Optional[float]:
    values = _present(*xs)
    return sum(values) / len(values) if values else INVALID


def _first(*xs: Optional[float]) -> Optional[float]:
    for value in xs:
        if value is not INVALID:
            return value
    return INVALID


def _max(*xs: Optional[float]) -> Optional[float]:
    values = _present(*xs)
    return max(values) if values else INVALID


def _min(*xs: Optional[float]) -> Optional[float]:
    values = _present(*xs)
    return min(values) if values else INVALID


def _ifgt(a, b, c, d=INVALID):
    """``ifgt(a, b, x, y)``: TSM treats a missing operand as unknown."""
    if len(_present(a, b)) < 2:
        return INVALID
    return c if a > b else d


def _iflt(a, b, c, d=INVALID):
    if len(_present(a, b)) < 2:
        return INVALID
    return c if a < b else d


_FUNCTIONS = {
    "max": _max,
    "min": _min,
    "avg": _avg,
    "first": _first,
    "ifgt": _ifgt,
    "iflt": _iflt,
}

_SCALARS = {"numexpires", "numexpires", "vendorsell"}


class _Parser:
    def __init__(self, tokens: list[tuple[str, object]], context: Mapping[str, object]):
        self.tokens = tokens
        self.index = 0
        self.context = context

    def _peek(self):
        return self.tokens[self.index] if self.index < len(self.tokens) else (None, None)

    def _take(self, expected: Optional[str] = None):
        _kind, token = self._peek()
        if token is None or (expected is not None and token != expected):
            raise ValueError(f"expected {expected!r}, found {token!r}")
        self.index += 1
        return token

    def expression(self):
        value = self.term()
        while self._peek()[1] in ("+", "-"):
            op = self._take()
            right = self.term()
            value = _unknown() if value is INVALID or right is INVALID else (
                value + right if op == "+" else value - right)
        return value

    def term(self):
        value = self.factor()
        while self._peek()[1] in ("*", "/", "%"):
            op = self._take()
            right = self.factor()
            if value is INVALID or right is INVALID:
                value = INVALID
            elif op == "*":
                value = value * right
            elif op == "/":
                value = value / right if right else INVALID
            else:
                # ``X% expr`` is X/100 * expr, so the left side is the percent
                value = value / 100.0 * right
        return value

    def factor(self):
        kind, token = self._peek()
        if token == "(":
            self._take("(")
            value = self.expression()
            self._take(")")
            return value
        if token == "-":
            self._take("-")
            value = self.factor()
            return INVALID if value is INVALID else -value
        if kind == "num":
            self._take()
            return token
        if kind == "id":
            self._take()
            if self._peek()[1] != "(":
                return self.context.get(str(token), INVALID)
            self._take("(")
            args = [self.expression()]
            while self._peek()[1] == ",":
                self._take(",")
                args.append(self.expression())
            self._take(")")
            function = _FUNCTIONS.get(str(token))
            if function is None:
                raise ValueError(f"unknown TSM function: {token}")
            return function(*args)
        raise ValueError(f"unexpected token: {token!r}")


def _unknown() -> None:
    return INVALID


def evaluate(expression: str, context: Mapping[str, object]) -> Optional[float]:
    """The value of a TSM expression, or ``None`` when TSM would say INVALID."""
    parser = _Parser(_tokenize(expression), context)
    value = parser.expression()
    if parser.index != len(parser.tokens):
        raise ValueError(f"trailing tokens: {[t for _, t in parser.tokens[parser.index:]]}")
    return value


# --------------------------------------------------------------------------- #
# the three levels
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class Levels:
    """What the three strings say for one item, in copper."""

    normalmarket: Optional[float]
    minimum: Optional[float]
    normal: Optional[float]
    maximum: Optional[float]

    def as_dict(self) -> Dict[str, Optional[float]]:
        return {"normalmarket": self.normalmarket, "minimum": self.minimum,
                "normal": self.normal, "maximum": self.maximum}


def context(*, dbmarket: Optional[float] = None,
            dbregionmarketavg: Optional[float] = None,
            dbregionsalerate: Optional[float] = None,
            vendorsell: Optional[float] = None,
            numexpires: float = 0.0,
            **extra: Optional[float]) -> Dict[str, object]:
    """The sources the strings read, with TSM's missing-value rule.

    ``numexpires`` defaults to 0 because the archive does not carry it: a
    zero is a fresh item, which is the expensive end of the discount.
    """
    ctx: Dict[str, object] = {
        "dbmarket": dbmarket,
        "dbregionmarketavg": dbregionmarketavg,
        "dbregionsalerate": dbregionsalerate,
        "vendorsell": vendorsell,
        "numexpires": float(numexpires) if numexpires is not None else INVALID,
    }
    for name, value in extra.items():
        ctx.setdefault(name, value)
    return ctx


def levels(ctx: Mapping[str, object]) -> Levels:
    """The three strings, evaluated once ``normalmarket`` is known."""
    # `normalmarket` is itself a TSM source; it has to be resolved first so
    # the three strings can refer to it by name.
    local = dict(ctx)
    local["normalmarket"] = evaluate(STRING_NORMALMARKET, local)
    return Levels(
        normalmarket=local["normalmarket"],
        minimum=evaluate(STRING_MINIMUM, local),
        normal=evaluate(STRING_NORMAL, local),
        maximum=evaluate(STRING_MAXIMUM, local),
    )


def levels_for(dbmarket: Optional[float],
               dbregionmarketavg: Optional[float],
               dbregionsalerate: Optional[float],
               vendorsell: Optional[float] = None,
               numexpires: float = 0.0) -> Levels:
    """The three levels for one item, from the references we actually have."""
    return levels(context(dbmarket=dbmarket,
                          dbregionmarketavg=dbregionmarketavg,
                          dbregionsalerate=dbregionsalerate,
                          vendorsell=vendorsell,
                          numexpires=numexpires))
