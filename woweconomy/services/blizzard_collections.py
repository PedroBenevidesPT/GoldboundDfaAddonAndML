"""Fetch collections from Blizzard's own API (the DfA replacement, fetch side).

Only what is needed to *get* the data lives here; storage/import stays in the
caller. Three routes, all public once the OAuth token is in hand:

* ``/profile/wow/character/{realm}/{name}`` -- existence, real
  ``last_login_timestamp`` and ``achievement_points``. One request, and it is
  the cheapest way to tell a dead character from a hidden one.
* ``.../collections/{mounts,pets,toys,transmogs,heirlooms}`` -- what the
  character owns, as flat lists of ids. Transmogs come as
  ``slots[].appearances[]`` (item-appearance ids) plus ``appearance_sets[]``.
* ``/data/wow/{mount,pet,toy}/index`` (+ ``/{id}``) -- the universe of
  collectibles, with the names and sources the catalogue needs.

Namespaces differ per route and getting one wrong fails loudly (404), so they
are spelled out: ``profile-<region>`` for a character, ``static-<region>`` for
game data.

Pacing: the shared :class:`ApiPacer` owns the rate (documented 100/s ceiling,
10/s sustained = the 36k/hour quota) and the cooldown every worker waits out
together; the client owns OAuth, retries and error typing. Nothing here sleeps
on its own.

Credentials are looked up in this order, and never echoed:

    1. --client-id / --client-secret
    2. $BLIZZARD_CLIENT_ID / $BLIZZARD_CLIENT_SECRET
    3. woweconomy/blizzard_client.txt   (line 1 = id, line 2 = secret)
    4. <COLLECTOR_PROJECT>/.env         (default: C:/collectorproject)

Usage:
    python -m woweconomy.services.blizzard_collections auth
    python -m woweconomy.services.blizzard_collections character --realm silvermoon --name vulppu
    python -m woweconomy.services.blizzard_collections universe --out woweconomy/dfa/cache/blizzard
    python -m woweconomy.services.blizzard_collections scan --file chars.txt --out out.jsonl
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
import time
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import quote

from woweconomy.services.api_pacer import ApiPacer
from woweconomy.services.dfa_store import DEFAULT_OUT_DIR
from woweconomy.services.blizzard_client import (
    REGION_CONFIGS,
    AsyncBlizzardClient,
    BlizzardApiError,
)

logger = logging.getLogger(__name__)

REGION = "eu"
LOCALE = "en_GB"

CREDENTIALS_FILE = Path("woweconomy/blizzard_client.txt")
COLLECTOR_PROJECT = Path(
    os.environ.get("COLLECTOR_PROJECT", r"C:/collectorproject")
)

CHARACTER_ROOT = "/profile/wow/character/{realm_slug}/{name}"

#: kind -> route. The kind names are the ones the database already uses, so a
#: fetched collection lands in `catalogue`/`missing`/`owned` without renaming.
#: `appearances` is the transmog route, and `recipes` is not a collection route
#: at all: `/professions` carries `known_recipes` per tier, one request for all
#: of them (its ids are the same recipe ids the DfA catalogue is keyed on --
#: 2677 of one character's 2680 were already there).
ROUTES: dict[str, str] = {
    "mounts": "collections/mounts",
    "pets": "collections/pets",
    "toys": "collections/toys",
    "appearances": "collections/transmogs",
    "heirlooms": "collections/heirlooms",
    "recipes": "professions",
}

#: The three kinds the DfA sample is built on; pets and toys are opt-in because
#: `catalogue` has no rows for them yet (their index has to be loaded first).
DEFAULT_KINDS = "mounts,recipes,appearances"

#: Requests one character costs for the default kinds: summary + 3 routes.
REQUESTS_PER_CHARACTER = 4

#: Where the raw answers are kept between runs (the discovery CLI uses the same
#: path, so both commands land in one file instead of two).
DEFAULT_CACHE_DIR = Path("woweconomy/dfa/cache")

#: kind -> (index route, the key its payload nests the list under)
UNIVERSE_ROUTES: dict[str, tuple[str, str]] = {
    "mounts": ("/data/wow/mount/index", "mounts"),
    "pets": ("/data/wow/pet/index", "pets"),
    "toys": ("/data/wow/toy/index", "toys"),
}
UNIVERSE_DETAIL = {
    "mounts": "/data/wow/mount/{id}",
    "pets": "/data/wow/pet/{id}",
    "toys": "/data/wow/toy/{id}",
}


# --------------------------------------------------------------------------- #
# Credentials
# --------------------------------------------------------------------------- #

def _read_env_file(path: Path) -> dict[str, str]:
    """The KEY=VALUE lines of a dotenv file; comments and blanks dropped.

    Deliberately dumb: no interpolation, no ``export`` handling beyond the
    prefix. The value is never logged -- only the fact that a key was found.
    """
    out: dict[str, str] = {}
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return out
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if key.startswith("export "):
            key = key[len("export "):].strip()
        out[key] = value.strip().strip('"').strip("'")
    return out


def load_credentials(
    client_id: str | None = None,
    client_secret: str | None = None,
) -> tuple[str, str]:
    """(id, secret), from the first source that has both. Never prints them."""
    for source, cid, secret in (
        ("--client-id", client_id, client_secret),
        ("$BLIZZARD_CLIENT_ID", os.environ.get("BLIZZARD_CLIENT_ID"),
         os.environ.get("BLIZZARD_CLIENT_SECRET")),
    ):
        if cid and secret:
            logger.debug("Blizzard credentials from %s", source)
            return cid.strip(), secret.strip()

    if CREDENTIALS_FILE.exists():
        lines = [ln.strip() for ln in
                 CREDENTIALS_FILE.read_text(encoding="utf-8").splitlines()
                 if ln.strip() and not ln.strip().startswith("#")]
        if len(lines) >= 2:
            logger.debug("Blizzard credentials from %s", CREDENTIALS_FILE)
            return lines[0], lines[1]

    env = _read_env_file(COLLECTOR_PROJECT / ".env")
    cid = env.get("BLIZZARD_CLIENT_ID", "")
    secret = env.get("BLIZZARD_CLIENT_SECRET", "")
    if cid and secret:
        logger.debug("Blizzard credentials from %s", COLLECTOR_PROJECT / ".env")
        return cid, secret

    raise SystemExit(
        "error: no Blizzard OAuth client.\n"
        "       Create one at https://develop.battle.net/access/clients and either:\n"
        f"         * write id and secret (one per line) to {CREDENTIALS_FILE}\n"
        "         * export BLIZZARD_CLIENT_ID=... BLIZZARD_CLIENT_SECRET=...\n"
        "         * pass --client-id ... --client-secret ...\n"
        f"       ({COLLECTOR_PROJECT / '.env'} is read too, if it has them.)\n"
    )


# --------------------------------------------------------------------------- #
# Names -> routes
# --------------------------------------------------------------------------- #

def realm_slug(name: str) -> str:
    """A realm as Blizzard spells it in a URL: ``Quel'Thalas`` -> ``quelthalas``.

    Apostrophes are *removed*, not turned into dashes (Blizzard's own slugs do
    that: ``quelthalas``, ``area-52``, ``twisting-nether``). A DfA board name
    (``connected-dragonmaw``) is normalised to the realm it pools, because the
    profile route is per-realm, never per-connected-group.
    """
    slug = (name or "").strip().split("/")[-1].strip().lower()
    if slug.startswith("connected-"):
        slug = slug[len("connected-"):]
    slug = slug.replace("'", "").replace("’", "")
    out = []
    for ch in slug:
        if ch.isalnum():
            out.append(ch)
        elif out and out[-1] != "-":
            out.append("-")
    return "".join(out).strip("-")


def character_path(realm: str, name: str) -> str:
    """The profile summary route for one character (name lower-cased)."""
    return CHARACTER_ROOT.format(
        realm_slug=quote(realm_slug(realm), safe=""),
        name=quote((name or "").strip().lower(), safe=""),
    )


def _namespaces(region: str) -> tuple[str, str]:
    return f"profile-{region}", f"static-{region}"


# --------------------------------------------------------------------------- #
# Parsing
# --------------------------------------------------------------------------- #

def parse_collections(payloads: Mapping[str, Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Owned-entity rows from the collection payloads.

    Every list entry is reduced to its stable id; the rest of the payload
    (quality, level, is_useable) is kept as a small detail string. An entry
    without an id is skipped rather than guessed: a partial id is not an
    identity.
    """
    out: list[dict[str, Any]] = []

    def add(collection_type: str, entity_id: Any, detail: Any = None) -> None:
        if entity_id in (None, ""):
            return
        try:
            entity_id = int(entity_id)
        except (TypeError, ValueError):
            return
        out.append(
            {
                "collection_type": collection_type,
                "entity_id": entity_id,
                "detail": detail,
            }
        )

    mounts = payloads.get("mounts", {}).get("mounts")
    if isinstance(mounts, list):
        for entry in mounts:
            if isinstance(entry, Mapping):
                mount = entry.get("mount")
                if isinstance(mount, Mapping):
                    add(
                        "mount",
                        mount.get("id"),
                        str(entry.get("is_useable")) if "is_useable" in entry else None,
                    )

    pets = payloads.get("pets", {}).get("pets")
    if isinstance(pets, list):
        for entry in pets:
            if isinstance(entry, Mapping):
                species = entry.get("species")
                if isinstance(species, Mapping):
                    detail = {
                        "level": entry.get("level"),
                        "quality": (entry.get("quality") or {}).get("type")
                        if isinstance(entry.get("quality"), Mapping)
                        else None,
                    }
                    add("pet", species.get("id"), str(detail))

    toys = payloads.get("toys", {}).get("toys")
    if isinstance(toys, list):
        for entry in toys:
            if isinstance(entry, Mapping):
                toy = entry.get("toy")
                if isinstance(toy, Mapping):
                    add("toy", toy.get("id"))

    transmogs = payloads.get("transmogs", {})
    sets = transmogs.get("appearance_sets")
    if isinstance(sets, list):
        for entry in sets:
            if isinstance(entry, Mapping):
                add("appearance_set", entry.get("id"))
    slots = transmogs.get("slots")
    if isinstance(slots, list):
        for slot in slots:
            if not isinstance(slot, Mapping):
                continue
            slot_type = (
                (slot.get("slot") or {}).get("type")
                if isinstance(slot.get("slot"), Mapping)
                else None
            )
            for appearance in slot.get("appearances") or []:
                if isinstance(appearance, Mapping):
                    add("appearance", appearance.get("id"), slot_type)

    # `/professions` is one payload holding every profession; the recipes are
    # nested three levels down, and a tier without `known_recipes` is a skill
    # that was never trained, not a character without recipes.
    for group in ("primaries", "secondaries"):
        for profession in payloads.get("professions", {}).get(group) or []:
            if not isinstance(profession, Mapping):
                continue
            for tier in profession.get("tiers") or []:
                if not isinstance(tier, Mapping):
                    continue
                for recipe in tier.get("known_recipes") or []:
                    if isinstance(recipe, Mapping):
                        add("recipe", recipe.get("id"))

    heirlooms = payloads.get("heirlooms", {}).get("heirlooms")
    if isinstance(heirlooms, list):
        for entry in heirlooms:
            if isinstance(entry, Mapping):
                heirloom = entry.get("heirloom")
                if isinstance(heirloom, Mapping):
                    add(
                        "heirloom",
                        heirloom.get("id"),
                        str((entry.get("upgrade") or {}).get("level"))
                        if isinstance(entry.get("upgrade"), Mapping)
                        else None,
                    )
    return out


def owned_by_kind(rows: Iterable[Mapping[str, Any]]) -> dict[str, list[int]]:
    """The ownership rows grouped into the kind names the catalogue uses.

    ``appearance``/``appearance_set`` both become ``appearances`` -- the same
    set the DfA catalogue is keyed on (item-appearance ids) -- and the two
    remain distinguishable by the ``collection_type`` on the row itself.
    """
    out: dict[str, set[int]] = {}
    for row in rows:
        kind = str(row.get("collection_type") or "")
        if kind.startswith("appearance"):
            kind = "appearances"
        elif kind == "recipe":
            kind = "recipes"
        else:
            kind += "s"
        out.setdefault(kind, set()).add(int(row["entity_id"]))
    return {kind: sorted(ids) for kind, ids in out.items()}


# --------------------------------------------------------------------------- #
# The fetcher
# --------------------------------------------------------------------------- #

@dataclass
class FetchStats:
    characters: int = 0
    requests: int = 0
    missing: int = 0
    failed: int = 0
    started: float = field(default_factory=time.monotonic)

    def report(self, pacer: ApiPacer) -> dict[str, Any]:
        elapsed = max(0.0, time.monotonic() - self.started)
        return {
            "characters": self.characters,
            "requests": self.requests,
            "missing": self.missing,
            "failed": self.failed,
            "seconds": round(elapsed, 1),
            "chars_per_second": round(self.characters / elapsed, 3) if elapsed else 0.0,
            "throttled_429": pacer.throttled,
            "final_rate_per_second": round(pacer.current, 2),
        }


class CollectionFetcher:
    """Fetch one character's collections through a shared pacer."""

    def __init__(
        self,
        client: AsyncBlizzardClient,
        pacer: ApiPacer,
        region: str = REGION,
        kinds: Sequence[str] = ("mounts", "recipes", "appearances"),
    ) -> None:
        unknown = [k for k in kinds if k not in ROUTES]
        if unknown:
            raise ValueError(f"unknown kind(s): {', '.join(unknown)}")
        self.client = client
        self.pacer = pacer
        self.region = region
        self.kinds = tuple(kinds)
        self.profile_ns, self.static_ns = _namespaces(region)
        self.stats = FetchStats()
        self._result_lock = asyncio.Lock()
        self._request_lock = asyncio.Lock()

    async def get(self, path: str, params: Mapping[str, Any]) -> Mapping[str, Any]:
        """One request: a slot from the pacer, then the client (which retries)."""
        # Hold one request through its retries. Otherwise concurrent responses
        # can each pass a previously taken slot and burst past the pacer's rate.
        async with self._request_lock:
            await self.pacer.slot()
            start = time.monotonic()
            try:
                payload = await self.client.get_json(path, params)
            except BlizzardApiError as exc:
                latency = time.monotonic() - start
                if exc.http_status == 429:
                    self.pacer.on_throttled(exc.headers, latency=latency)
                    raise
                self.pacer.on_success(latency)
                raise
            self.pacer.on_success(time.monotonic() - start)
            return payload

    async def character(self, realm: str, name: str) -> dict[str, Any]:
        """Summary + collections for one character; never raises for a 404."""
        path = character_path(realm, name)
        summary: Mapping[str, Any] | None = None
        try:
            summary = await self.get(path, {"namespace": self.profile_ns,
                                           "locale": LOCALE})
            self.stats.requests += 1
        except BlizzardApiError as exc:
            if exc.http_status in (403, 404):
                async with self._result_lock:
                    self.stats.characters += 1
                    self.stats.missing += 1
                return self._result(realm, name, "NOT_FOUND", status=exc.http_status,
                                    summary=None, payloads={})
            async with self._result_lock:
                self.stats.characters += 1
                self.stats.failed += 1
            return self._result(realm, name, "ERROR", status=exc.http_status,
                                summary=None, payloads={}, error=str(exc))

        async with self._result_lock:
            self.stats.characters += 1
        payloads: dict[str, Mapping[str, Any]] = {}
        statuses: dict[str, int | None] = {}
        for kind in self.kinds:
            route = ROUTES[kind]
            # The payload key is the route's last segment: transmogs -> the
            # `transmogs` parser, professions -> the `professions` one.
            try:
                payloads[route.rsplit("/", 1)[-1]] = await self.get(
                    f"{path}/{route}",
                    {"namespace": self.profile_ns, "locale": LOCALE},
                )
                statuses[kind] = 200
                self.stats.requests += 1
            except BlizzardApiError as exc:
                statuses[kind] = exc.http_status
                self.stats.requests += 1
                # Blizzard returns 404 for characters without professions;
                # that's a genuine empty recipe set, unlike a hidden collection.
                if kind == "recipes" and exc.http_status == 404:
                    # Empty is a complete fact; mark the route as successfully
                    # interpreted so it can be committed to `scanned`.
                    statuses[kind] = 200
                    payloads["professions"] = {}
                    continue
                if exc.http_status in (403, 404):
                    # A hidden collection is not an empty one: it is recorded as
                    # absent so the caller can tell "owns nothing" from "we were
                    # not shown it".
                    continue
                self.stats.failed += 1
                return self._result(realm, name, "ERROR", status=exc.http_status,
                                    summary=summary, payloads=payloads,
                                    statuses=statuses, error=str(exc))
        rows = parse_collections(payloads)
        return self._result(realm, name, "OK", status=200, summary=summary,
                            payloads=payloads, rows=rows, statuses=statuses)

    def _result(
        self,
        realm: str,
        name: str,
        outcome: str,
        *,
        status: int | None,
        summary: Mapping[str, Any] | None,
        payloads: Mapping[str, Mapping[str, Any]],
        rows: Sequence[Mapping[str, Any]] | None = None,
        statuses: Mapping[str, int | None] | None = None,
        error: str | None = None,
    ) -> dict[str, Any]:
        rows = rows if rows is not None else parse_collections(payloads)
        return {
            "region": self.region,
            "realm": realm,
            "realm_slug": realm_slug(realm),
            "name": name,
            "outcome": outcome,
            "status": status,
            "error": error,
            "statuses": dict(statuses or {}),
            "last_login_timestamp": (summary or {}).get("last_login_timestamp"),
            "account_id": (summary or {}).get("id"),
            "realm_id": (((summary or {}).get("realm") or {}).get("id")
                         if isinstance((summary or {}).get("realm"), Mapping) else None),
            "achievement_points": (summary or {}).get("achievement_points"),
            "level": (summary or {}).get("level"),
            "class": ((summary or {}).get("character_class") or {}).get("name"),
            "faction": ((summary or {}).get("faction") or {}).get("name"),
            "guild": ((summary or {}).get("guild") or {}).get("name"),
            "ownership": [dict(r) for r in rows],
            "owned": owned_by_kind(rows),
            "fetched_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        }

    async def many(
        self,
        characters: Iterable[tuple[str, str]],
        concurrency: int = 8,
    ) -> list[dict[str, Any]]:
        """Every character, at most `concurrency` in flight (the pacer holds rate)."""
        gate = asyncio.Semaphore(concurrency)

        async def one(realm: str, name: str) -> dict[str, Any]:
            async with gate:
                return await self.character(realm, name)

        return list(await asyncio.gather(
            *(one(r, n) for r, n in characters)
        ))


# --------------------------------------------------------------------------- #
# Universe (mounts / pets / toys)
# --------------------------------------------------------------------------- #

def _index_entries(payload: Mapping[str, Any], container: str) -> list[dict[str, Any]]:
    entries = payload.get(container)
    if not isinstance(entries, list):
        return []
    out = []
    for entry in entries:
        if not isinstance(entry, Mapping) or not entry.get("id"):
            continue
        out.append({"id": int(entry["id"]), "name": entry.get("name")})
    return out


async def fetch_universe(
    fetcher: CollectionFetcher,
    kinds: Sequence[str],
    detail: bool = False,
) -> dict[str, list[dict[str, Any]]]:
    """The collectible universe per kind, from the official indices.

    The index already carries the name; ``detail=True`` adds what only the
    detail route publishes (source type, requirements, icon) at one request
    per id -- ~1.4k mounts, ~1.6k pets, ~800 toys.
    """
    out: dict[str, list[dict[str, Any]]] = {}
    for kind in kinds:
        path, container = UNIVERSE_ROUTES[kind]
        payload = await fetcher.get(path, {"namespace": fetcher.static_ns,
                                          "locale": LOCALE})
        entries = _index_entries(payload, container)
        logger.info("%s index: %d ids", kind, len(entries))
        if detail:
            template = UNIVERSE_DETAIL[kind]

            async def one(entry: dict[str, Any]) -> dict[str, Any]:
                try:
                    body = await fetcher.get(
                        template.format(id=entry["id"]),
                        {"namespace": fetcher.static_ns, "locale": LOCALE},
                    )
                except BlizzardApiError as exc:
                    if exc.http_status in (403, 404):
                        return entry
                    raise
                row = dict(entry)
                row.update({
                    "name": body.get("name") or entry.get("name"),
                    "description": body.get("description"),
                    "source_type": (body.get("source") or {}).get("type")
                    if isinstance(body.get("source"), Mapping) else None,
                    "requirements": body.get("requirements"),
                    "item_id": (body.get("item") or {}).get("id")
                    if isinstance(body.get("item"), Mapping) else None,
                    "is_tradable": body.get("is_tradable"),
                    "is_capturable": body.get("is_capturable"),
                })
                return row

            gate = asyncio.Semaphore(fetcher.client.concurrency)

            async def bounded(entry: dict[str, Any]) -> dict[str, Any]:
                async with gate:
                    return await one(entry)

            entries = list(await asyncio.gather(*(bounded(e) for e in entries)))
        out[kind] = sorted(entries, key=lambda e: e["id"])
    return out


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #

def _client(args) -> AsyncBlizzardClient:
    client_id, client_secret = load_credentials(args.client_id, args.client_secret)
    cache = None
    cache_dir = getattr(args, "cache_dir", None)
    if cache_dir:
        from .http_cache import ResponseCache
        cache = ResponseCache(Path(cache_dir) / "blizzard.sqlite")
    return AsyncBlizzardClient(
        region=args.region,
        locale=args.locale,
        client_id=client_id,
        client_secret=client_secret,
        concurrency=args.concurrency,
        cache=cache,
        cache_ttl=float(getattr(args, "cache_ttl", 0.0)) * 3600.0,
        offline=bool(getattr(args, "offline", False)),
    )


def _pacer(args) -> ApiPacer:
    return ApiPacer(
        rate=args.rate,
        sustained_rate=args.sustained_rate,
        min_rate=1.0,
    )


def cmd_auth(args) -> int:
    async def run() -> int:
        client = _client(args)
        async with client:
            token = await client._get_access_token()  # noqa: SLF001 - a probe
            print(f"ok  {args.region}  token len={len(token)}  "
                  f"ns={client.namespace}")
        return 0

    return asyncio.run(run())


def cmd_character(args) -> int:
    async def run() -> int:
        client = _client(args)
        async with client:
            fetcher = CollectionFetcher(
                client, _pacer(args), args.region,
                args.kinds.split(",") if hasattr(args, "kinds") else ("mounts", "recipes", "appearances"),
            )
            result = await fetcher.character(args.realm, args.name)
        print(json.dumps({
            "realm": result["realm_slug"],
            "name": result["name"],
            "outcome": result["outcome"],
            "status": result["status"],
            "statuses": result["statuses"],
            "last_login_timestamp": result["last_login_timestamp"],
            "achievement_points": result["achievement_points"],
            "owned": {k: len(v) for k, v in result["owned"].items()},
        }, indent=2))
        if result["outcome"] == "ERROR":
            # Four empty collections and a failed request look identical in the
            # counts above, so say why.
            print(f"error: {result.get('error') or 'pedido falhou'}", file=sys.stderr)
            if args.offline:
                print(f"       offline: esta resposta não está em {args.cache_dir}; "
                      f"corre uma vez sem --offline.", file=sys.stderr)
        if args.json:
            args.json.parent.mkdir(parents=True, exist_ok=True)
            args.json.write_text(json.dumps(result, indent=1), encoding="utf-8")
            print(f"-> {args.json}")
        return 0 if result["outcome"] == "OK" else 1

    return asyncio.run(run())


def cmd_universe(args) -> int:
    kinds = [k.strip() for k in args.universe_kinds.split(",") if k.strip()]
    unknown = [k for k in kinds if k not in UNIVERSE_ROUTES]
    if unknown:
        print(f"error: unknown kind(s): {', '.join(unknown)}", file=sys.stderr)
        return 2

    async def run() -> dict[str, list[dict[str, Any]]]:
        client = _client(args)
        async with client:
            fetcher = CollectionFetcher(client, _pacer(args), args.region)
            data = await fetch_universe(fetcher, kinds, detail=args.detail)
            print(json.dumps(fetcher.stats.report(fetcher.pacer), indent=2))
            return data

    data = asyncio.run(run())
    for kind, entries in data.items():
        print(f"{kind:<10} {len(entries):>6} entries")
    if args.out:
        args.out.mkdir(parents=True, exist_ok=True)
        for kind, entries in data.items():
            path = args.out / f"{kind}.json"
            path.write_text(json.dumps(entries, ensure_ascii=False),
                            encoding="utf-8")
            print(f"  {path}")
    return 0


def read_characters(path: Path) -> list[tuple[str, str]]:
    """``realm/name`` or ``realm<TAB>name`` per line; ``#`` comments skipped."""
    out: list[tuple[str, str]] = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "/" in line:
            realm, _, name = line.partition("/")
        elif "\t" in line:
            realm, _, name = line.partition("\t")
        else:
            realm, _, name = line.partition(" ")
        if realm.strip() and name.strip():
            out.append((realm.strip(), name.strip()))
    return out


def cmd_scan(args) -> int:
    characters = read_characters(args.file)
    if not characters:
        print(f"error: {args.file} has no `realm/name` lines", file=sys.stderr)
        return 2
    if args.limit:
        characters = characters[: args.limit]
    # Resume skips only characters that answered: a NOT_FOUND is one cheap
    # request and is re-probed, because a character that did not exist when the
    # run happened may be a typo or a rename and cannot be told apart here.
    done: set[tuple[str, str]] = set()
    attempts = 0
    if args.out.exists() and not args.force:
        for line in args.out.read_text(encoding="utf-8").splitlines():
            try:
                row = json.loads(line)
            except ValueError:
                continue
            attempts += 1
            if row.get("outcome") == "OK":
                done.add((str(row.get("realm_slug")),
                          str(row.get("name")).lower()))
        characters = [c for c in characters
                      if (realm_slug(c[0]), c[1].lower()) not in done]
        print(f"resume: {attempts} linhas em {args.out} ({len(done)} fetched), "
              f"{len(characters)} por buscar")

    async def run() -> FetchStats:
        client = _client(args)
        async with client:
            fetcher = CollectionFetcher(
                client, _pacer(args), args.region,
                args.kinds.split(",") if hasattr(args, "kinds") else ("mounts", "recipes", "appearances"),
            )
            results = await fetcher.many(characters, concurrency=args.concurrency)
            args.out.parent.mkdir(parents=True, exist_ok=True)
            with args.out.open("a", encoding="utf-8") as handle:
                for row in results:
                    handle.write(json.dumps(row, ensure_ascii=False) + "\n")
            print(json.dumps(fetcher.stats.report(fetcher.pacer), indent=2))
            return fetcher.stats

    asyncio.run(run())
    print(f"-> {args.out}")
    return 0


def _parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--region", default=REGION, choices=sorted(REGION_CONFIGS))
    common.add_argument("--locale", default=LOCALE)
    common.add_argument("--client-id")
    common.add_argument("--client-secret")
    common.add_argument("--concurrency", type=int, default=8,
                        help="requests in flight (the pacer, not this, sets the rate)")
    common.add_argument("--rate", type=float, default=100.0,
                        help="per-second ceiling (Blizzard's documented 100/s)")
    common.add_argument("--sustained-rate", dest="sustained_rate", type=float,
                        default=10.0,
                        help="hourly budget in requests/second (36000/h = 10); "
                             "0 disables the bucket")
    common.add_argument("-v", "--verbose", action="count", default=0)
    # Answers are kept on disk by default: the same request served twice costs
    # nothing the second time, and a 304 renews what is already there.
    common.add_argument("--cache-dir", type=Path, default=DEFAULT_CACHE_DIR,
                        help="one SQLite file holding every raw API answer")
    common.add_argument("--cache-ttl", dest="cache_ttl", type=float, default=0.0,
                        help="hours to serve from cache without asking Blizzard; "
                             "0 revalidates with If-None-Match")
    common.add_argument("--offline", action="store_true",
                        help="answer only from the cache, never the network")
    ap = argparse.ArgumentParser(
        prog="blizzard_collections",
        description="Fetch WoW collections from Blizzard's official API.",
        parents=[common],
    )
    sub = ap.add_subparsers(dest="command", required=True)
    sub.add_parser("auth", parents=[common], help="test the OAuth token").set_defaults(func=cmd_auth)

    ch = sub.add_parser("character", parents=[common],
                        help="one character's collections")
    ch.add_argument("--realm", required=True)
    ch.add_argument("--name", required=True)
    ch.add_argument("--json", type=Path)
    ch.add_argument("--kinds", default=DEFAULT_KINDS,
                    help="comma-separated: mounts,recipes,appearances,pets,toys,heirlooms")
    ch.set_defaults(func=cmd_character)

    un = sub.add_parser("universe", parents=[common],
                        help="mount/pet/toy index (the catalogue)")
    un.add_argument("--kinds", dest="universe_kinds", default="mounts,pets,toys")
    un.add_argument("--detail", action="store_true",
                    help="one request per id for source/requirements")
    un.add_argument("--out", type=Path)
    un.set_defaults(func=cmd_universe)

    sc = sub.add_parser("scan", parents=[common],
                        help="many characters from a realm/name file, resumable "
                             "(for discovered characters use "
                             "`python -m woweconomy.services.blizzard_discovery scan`)")
    sc.add_argument("--file", type=Path, required=True,
                    help="JSON list of characters; the discovered ones are already "
                         "in dfa.db, so this tool is for a hand-made list")
    sc.add_argument("--out", type=Path, required=True)
    sc.add_argument("--limit", type=int, default=0)
    sc.add_argument("--force", action="store_true", help="ignore what --out has")
    sc.add_argument("--kinds", default=DEFAULT_KINDS,
                    help="comma-separated: mounts,recipes,appearances,pets,toys,heirlooms")
    sc.set_defaults(func=cmd_scan)
    return ap


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose > 1 else
        logging.INFO if args.verbose else logging.WARNING,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    try:
        return args.func(args)
    except BlizzardApiError as exc:
        # `--offline` on a request that was never stored is an expected stop,
        # not a crash: say which request and how to get it.
        if args.offline:
            print(f"error: {exc}\n"
                  f"       offline: esta resposta não está em {args.cache_dir}; "
                  f"corre uma vez sem --offline.", file=sys.stderr)
        else:
            print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
