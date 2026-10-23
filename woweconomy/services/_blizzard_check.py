r"""Self-check for the Blizzard collection fetcher (offline, no network).

Run it after touching `blizzard_collections.py`, `blizzard_client.py` or
`api_pacer.py`:

    cd C:\\economics
    python -m woweconomy.services._blizzard_check

Everything here fails silently in production if it is wrong: a slug Blizzard
does not know is a 404 (a dead character), an appearance id mapped to the
wrong kind never counts as missing, a 403 on one collection read as "owns
nothing" invents demand out of a hidden profile. So the checks drive the real
`CollectionFetcher` against a fake client and a real pacer.

Covers:
  1. realm slug: apostrophes removed (quelthalas), spaces dashed (area-52), a
     DfA connected board normalised to its realm, accents kept as letters
  2. character route: lower-cased, URL-quoted, one segment per part
  3. `parse_collections` on all five payloads, and an entry without an id is
     skipped rather than landing in bit 0
  4. `owned_by_kind` folds appearance_set into appearances (the DfA id space)
  5. credentials: flags > env > file > collector project, and "none" is an error
  6. fetcher: a 404 summary is NOT_FOUND after one request; a 403 on transmogs
     keeps the other kinds and records the 403 instead of calling it empty
  7. the pacer spaces slots: 4 requests at 20/s take >= 0.15s, not 0.00s
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import time
from pathlib import Path

from woweconomy.services import blizzard_collections as B
from woweconomy.services.api_pacer import ApiPacer
from woweconomy.services.blizzard_client import BlizzardApiError

FAILS: list[str] = []


def check(label: str, got, want) -> None:
    ok = got == want
    print(f"  {'ok  ' if ok else 'FAIL'} {label}: {got!r}"
          + ("" if ok else f" != {want!r}"))
    if not ok:
        FAILS.append(label)


class FakeClient:
    """Serves a fixed `path -> payload | BlizzardApiError` map; counts calls."""

    concurrency = 2

    def __init__(self, responses: dict) -> None:
        self.responses = responses
        self.calls: list[str] = []

    async def get_json(self, path, params=None):
        self.calls.append(path)
        value = self.responses.get(path)
        if isinstance(value, BlizzardApiError):
            raise value
        if value is None:
            raise BlizzardApiError("nope", http_status=404)
        return value


SUMMARY = {
    "name": "Testchar",
    "level": 80,
    "last_login_timestamp": 1_700_000_000_000,
    "achievement_points": 12345,
    "character_class": {"name": "Mage"},
    "faction": {"name": "Alliance"},
    "guild": {"name": "G"},
}
MOUNTS = {"mounts": [{"mount": {"id": 6}, "is_useable": True},
                     {"mount": {"id": 7}}]}
PETS = {"pets": [{"species": {"id": 39}, "level": 25,
                  "quality": {"type": "RARE"}}]}
TOYS = {"toys": [{"toy": {"id": 123}}]}
TRANSMOGS = {
    "appearance_sets": [{"id": 900}],
    "slots": [{"slot": {"type": "HEAD"}, "appearances": [{"id": 11}, {"id": 12}]}],
}
HEIRLOOMS = {"heirlooms": [{"heirloom": {"id": 5}, "upgrade": {"level": 4}}]}

ROOT = B.character_path("Kazzak", "TestChar")
TRANSFORMS_403 = BlizzardApiError("forbidden", http_status=403)


def check_slugs() -> None:
    print("1-2. slugs and routes")
    check("Quel'Thalas", B.realm_slug("Quel'Thalas"), "quelthalas")
    check("Area 52", B.realm_slug("Area 52"), "area-52")
    check("connected-dragonmaw", B.realm_slug("connected-dragonmaw"), "dragonmaw")
    check("Twisting Nether", B.realm_slug("Twisting Nether"), "twisting-nether")
    check("Kul Tiras", B.realm_slug("Kul Tiras"), "kul-tiras")
    check("route", B.character_path("Kazzak", "TestChar"),
          "/profile/wow/character/kazzak/testchar")
    check("route apostrophe realm", B.character_path("Zul'jin", "A"),
          "/profile/wow/character/zuljin/a")


def check_parse() -> None:
    print("3-4. payload -> ownership rows")
    rows = B.parse_collections({
        "mounts": MOUNTS, "pets": PETS, "toys": TOYS,
        "transmogs": TRANSMOGS, "heirlooms": HEIRLOOMS,
    })
    kinds = sorted({r["collection_type"] for r in rows})
    check("kinds", kinds,
          ["appearance", "appearance_set", "heirloom", "mount", "pet", "toy"])
    check("rows", len(rows), 8)
    check("no-id skipped", B.parse_collections({"toys": {"toys": [{"toy": {}}]}}), [])
    check("bad id skipped", B.parse_collections({"toys": {"toys": [{"toy": {"id": "x"}}]}}), [])
    check("owned_by_kind", B.owned_by_kind(rows), {
        "appearances": [11, 12, 900], "heirlooms": [5], "mounts": [6, 7],
        "pets": [39], "toys": [123],
    })


def check_credentials() -> None:
    print("5. credential lookup order")
    check("flags win", B.load_credentials("id1", "sec1"), ("id1", "sec1"))
    os.environ["BLIZZARD_CLIENT_ID"] = "id2"
    os.environ["BLIZZARD_CLIENT_SECRET"] = "sec2"
    try:
        check("env second", B.load_credentials(), ("id2", "sec2"))
    finally:
        del os.environ["BLIZZARD_CLIENT_ID"]
        del os.environ["BLIZZARD_CLIENT_SECRET"]

    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        creds = tmp / "blizzard_client.txt"
        creds.write_text("# comment\nid3\nsec3\n", encoding="utf-8")
        env = tmp / ".env"
        env.write_text("BLIZZARD_CLIENT_ID=id4\nBLIZZARD_CLIENT_SECRET=sec4\n",
                       encoding="utf-8")
        old = B.CREDENTIALS_FILE, B.COLLECTOR_PROJECT
        B.CREDENTIALS_FILE, B.COLLECTOR_PROJECT = creds, tmp
        try:
            check("file third", B.load_credentials(), ("id3", "sec3"))
            creds.unlink()
            check("collector project last", B.load_credentials(), ("id4", "sec4"))
            env.unlink()
            try:
                B.load_credentials()
            except SystemExit:
                check("none is an error", True, True)
            else:
                check("none is an error", False, True)
        finally:
            B.CREDENTIALS_FILE, B.COLLECTOR_PROJECT = old


def check_fetcher() -> None:
    print("6. fetcher outcomes")
    responses = {
        ROOT: SUMMARY,
        ROOT + "/collections/mounts": MOUNTS,
        ROOT + "/professions": {"primaries": [{"tiers": [{"known_recipes": [{"id": 42}]}]}]},
        ROOT + "/collections/transmogs": TRANSFORMS_403,
    }

    async def run_ok():
        client = FakeClient(responses)
        fetcher = B.CollectionFetcher(client, ApiPacer(rate=100.0), "eu")
        return await fetcher.character("Kazzak", "TestChar"), client

    result, client = asyncio.run(run_ok())
    check("outcome", result["outcome"], "OK")
    check("requests incl. summary", len(client.calls), 4)
    check("mounts kept", result["owned"]["mounts"], [6, 7])
    check("recipes parsed", result["owned"]["recipes"], [42])
    check("hidden appearances have no bit", "appearances" in result["owned"], False)
    check("hidden appearances recorded", result["statuses"]["appearances"], 403)

    async def run_404():
        client = FakeClient({})
        fetcher = B.CollectionFetcher(client, ApiPacer(rate=100.0), "eu")
        return await fetcher.character("Kazzak", "Ghost"), client

    result, client = asyncio.run(run_404())
    check("404 is NOT_FOUND", result["outcome"], "NOT_FOUND")
    check("404 costs one request", len(client.calls), 1)
    check("404 has no ownership", result["owned"], {})

    async def run_heirlooms():
        client = FakeClient({
            ROOT: SUMMARY,
            ROOT + "/collections/mounts": MOUNTS,
            ROOT + "/professions": {},
            ROOT + "/collections/transmogs": TRANSFORMS_403,
            ROOT + "/collections/heirlooms": HEIRLOOMS,
        })
        fetcher = B.CollectionFetcher(client, ApiPacer(rate=100.0), "eu",
                                     kinds=("mounts", "recipes", "appearances", "heirlooms"))
        return await fetcher.character("Kazzak", "TestChar")

    result = asyncio.run(run_heirlooms())
    check("heirlooms opt-in", result["owned"]["heirlooms"], [5])


def check_pacer() -> None:
    print("7. the pacer spaces slots")

    async def run(n: int, rate: float) -> float:
        pacer = ApiPacer(rate=rate, sustained_rate=rate)
        started = time.monotonic()
        for _ in range(n):
            await pacer.slot()
        return time.monotonic() - started

    elapsed = asyncio.run(run(4, 20.0))
    print(f"   4 slots at 20/s -> {elapsed:.3f}s")
    check("spaced", elapsed >= 0.10, True)


def main() -> int:
    check_slugs()
    check_parse()
    check_credentials()
    check_fetcher()
    check_pacer()
    print()
    if FAILS:
        print(f"{len(FAILS)} FAILED: {', '.join(FAILS)}")
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
