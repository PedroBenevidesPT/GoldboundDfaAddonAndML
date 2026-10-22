"""One pricing posture per realm, read from the measured market profile.

Why this module exists
    The band coefficients in ``mog_operations`` say how far below a price band a
    listing should sit.  They were estimated on the **regional** market value,
    and that reference is not the local level: in the snapshot used here
    ``mediana(mercado do reino / mercado regional)`` runs from 0.24 (Argent Dawn)
    to 1.29 (Pozzo dell'Eternita).  Posting at the regional level on Argent Dawn
    puts the ask four times above the local market and it never sells; posting at
    the regional level on a quiet realm leaves money on the table.

    So pricing has **two independent dimensions**:

    * the band (``BAND_MULTIPLIERS``) -- statistical, per price band;
    * the realm (this module) -- one scalar ``factor`` (the measured local level,
      median of realm market / region market over the whole catalogue) plus a
      posture derived from the measured competition depth.

    A realm is not allowed to reshape the bands per item group: with ~14k priced
    items per realm we can estimate one scalar per realm, not a realm x band x
    group coefficient.  The single scalar is applied uniformly to all three
    price fields of every band, which keeps the ordering
    ``min <= normal <= max`` and the bands' own ranking intact.

What is measured and what is a choice
    Measured (``reports/mog_groups/realm_profiles.csv``):
        ``price_vs_region``  median local ask / regional ask, per realm
        ``depth_med``        median live auctions per listed catalogue item
        ``wall_share``       share of listed items with >= 3 auctions
        ``solo_share``       share with exactly one auction
        ``listed_share``     share of priced items with a live auction

    A choice (``CLASS_POLICY``):
        the posture multiplier, the posting duration and the holding window of
        each class.  These are a policy, not an estimate: there is no per-realm
        sales history to calibrate them against.

    ``depth_med`` exists only for the 12 realms the TSM app downloaded.  On the
    other realms the class is **inferred** from ``price_vs_region`` (a realm
    asking 95 %+ of the region is treated as thin, below 45 % as deep) and the
    row is marked ``inferido``: on the eight realms where both are known the
    price rule agrees with the measured depth on seven.

Confidence
    ``alta``    -- measured depth and >= 2000 priced items
    ``inferida``-- no auction counts, class inferred from the price ratio
    ``partilhada`` -- no data at all: the regional reference (factor 1.00) is
    used, which is the conservative choice, never an invented local number.
"""
from __future__ import annotations

import csv
import re
import unicodedata
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Dict

ROOT = Path(__file__).resolve().parents[2]
PROFILES_CSV = ROOT / "reports" / "mog_groups" / "realm_profiles.csv"
ALIASES_CSV = ROOT / "reports" / "mog_groups" / "realm_aliases.csv"

# --- the realm dimension: a policy, not an estimate ---------------------
# posture multiplies normal/min/max on top of the measured factor.
#   thin:     a solo listing can ask more and must not chase a lowball, and the
#             catalogue turns over for months -> 48h, 60 days, +20 %.
#   balanced: the regional reference corrected to the local level, 24h.
#   deep:     no price concession (the wall branch already sets the price when
#             there is competition); the lever is speed -> 12h, 14 days.
CLASS_POLICY: Dict[str, Dict[str, float]] = {
    "thin": {"posture": 1.20, "duration": 48, "hold_days": 60},
    "balanced": {"posture": 1.00, "duration": 24, "hold_days": 30},
    "deep": {"posture": 1.00, "duration": 12, "hold_days": 14},
}

CLASS_LABEL = {
    "thin": "pouco competitivo",
    "balanced": "intermédio",
    "deep": "competitivo",
}

MIN_FACTOR = 0.20
MAX_FACTOR = 1.30


@dataclass(frozen=True)
class RealmProfile:
    """Measured market state of one connected realm plus its pricing posture."""

    realm: str
    group_id: int = 0
    klass: str = "balanced"
    factor: float = 1.0
    items: int = 0
    listed_share: float | None = None
    depth_med: float | None = None
    wall_share: float | None = None
    solo_share: float | None = None
    price_vs_region: float | None = None
    p25: float | None = None
    p75: float | None = None
    sample: int = 0
    confidence: str = "partilhada"

    @property
    def posture(self) -> float:
        return CLASS_POLICY[self.klass]["posture"]

    @property
    def duration(self) -> int:
        return int(CLASS_POLICY[self.klass]["duration"])

    @property
    def hold_days(self) -> int:
        return int(CLASS_POLICY[self.klass]["hold_days"])

    @property
    def scale(self) -> float:
        """Multiplier applied to every band coefficient (factor x posture)."""
        return self.factor * self.posture

    @property
    def measured(self) -> bool:
        return self.depth_med is not None

    def describe(self) -> str:
        bits = [f"{self.realm} [{self.klass}]",
                f"factor {self.factor:.2f}"]
        if self.depth_med is not None:
            bits.append(f"{self.depth_med:g} anúncios/item")
        if self.price_vs_region is not None:
            bits.append(f"{self.price_vs_region:.2f}x região")
        bits.append(f"confiança {self.confidence}")
        return ", ".join(bits)


NEUTRAL = RealmProfile(realm="referência regional")


def _fold(text: str) -> str:
    decomposed = unicodedata.normalize("NFKD", text)
    stripped = "".join(c for c in decomposed if not unicodedata.combining(c))
    return "".join(c for c in stripped if c.isascii())


def _key(text: str) -> str:
    return "".join(c for c in _fold(text).lower() if c.isalnum())


def _first_token(text: str) -> str:
    tokens = [t for t in re.split(r"[^A-Za-z0-9]+", _fold(text)) if t]
    return tokens[0].lower() if tokens else ""


def _number(value: str) -> float | None:
    value = (value or "").strip()
    if not value:
        return None
    try:
        return float(value)
    except ValueError:
        return None


@lru_cache(maxsize=1)
def load() -> tuple[Dict[str, RealmProfile], Dict[str, str], Dict[str, str]]:
    """Return ``(profiles by key, alias key -> canonical key, alias token -> key)``."""
    profiles: Dict[str, RealmProfile] = {}
    if PROFILES_CSV.exists():
        with PROFILES_CSV.open(encoding="utf-8") as fh:
            for row in csv.DictReader(fh):
                profile = RealmProfile(
                    realm=row["realm"].strip(),
                    group_id=int(row.get("group_id") or 0),
                    klass=(row.get("class") or "balanced").strip(),
                    factor=float(row.get("factor") or 1.0),
                    items=int(row.get("items") or 0),
                    listed_share=_number(row.get("listed_share")),
                    depth_med=_number(row.get("depth_med")),
                    wall_share=_number(row.get("wall_share")),
                    solo_share=_number(row.get("solo_share")),
                    price_vs_region=_number(row.get("price_vs_region")),
                    p25=_number(row.get("p25")),
                    p75=_number(row.get("p75")),
                    sample=int(row.get("sample") or 0),
                    confidence=(row.get("confidence") or "partilhada").strip(),
                )
                profiles[_key(profile.realm)] = profile

    aliases: Dict[str, str] = {}
    tokens: Dict[str, str] = {}
    if ALIASES_CSV.exists():
        with ALIASES_CSV.open(encoding="utf-8") as fh:
            for row in csv.DictReader(fh):
                canonical = _key(row["canonical"])
                if canonical not in profiles:
                    continue
                for alias in (row.get("realm") or "").split("|"):
                    alias = alias.strip()
                    if not alias:
                        continue
                    key = _key(alias)
                    aliases[key] = canonical
                    token = _first_token(alias)
                    if token:
                        tokens.setdefault(token, canonical)
    return profiles, aliases, tokens


def resolve(realm: "str | RealmProfile | None") -> RealmProfile:
    """Find the profile of a realm name, slug or connected-realm alias."""
    if isinstance(realm, RealmProfile):
        return realm
    if not realm:
        return NEUTRAL
    profiles, aliases, tokens = load()
    key = _key(realm)
    for candidate in (key, aliases.get(key, "")):
        if candidate and candidate in profiles:
            return profiles[candidate]
    token = _first_token(realm)
    canonical = tokens.get(token) or (token if token in profiles else "")
    if canonical:
        return profiles.get(canonical, NEUTRAL)
    return NEUTRAL


def known_realms() -> list[RealmProfile]:
    """Every realm with a profile row, most competitive last."""
    profiles, _, _ = load()
    unique = {p.realm: p for p in profiles.values()}
    return sorted(unique.values(), key=lambda p: (p.depth_med or 0, p.realm))
