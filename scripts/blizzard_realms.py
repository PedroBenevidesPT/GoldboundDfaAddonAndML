"""Blizzard realm index + connected realms, straight from the API.

    GET /data/wow/realm/index             every realm of a region (id, slug, name)
    GET /data/wow/connected-realm/index   the id of every connected group
    GET /data/wow/connected-realm/{id}    which realms share one market

All three live in the *dynamic* namespace (``dynamic-eu`` / ``dynamic-us``), not
the static one the item client uses.  Note that ``realm/index`` does **not** carry
a ``connected_realm`` link -- only ``realm/{slug}`` does -- so the realm -> group
map is built from the groups themselves (one request each).

Credentials come from the same env vars as the item client
(``BLIZZARD_CLIENT_ID`` / ``BLIZZARD_CLIENT_SECRET``, loaded from .env).

Usage
    python scripts/blizzard_realms.py                       # EU, table + groups
    python scripts/blizzard_realms.py --region us
    python scripts/blizzard_realms.py --out reports/realms_eu.txt \
        --json reports/realms_eu.json
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from pathlib import Path

import requests

try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).resolve().parents[1] / ".env")
    load_dotenv(Path(".env"))
except ImportError:
    for _p in (Path(__file__).resolve().parents[1] / ".env", Path(".env")):
        try:
            for _line in _p.read_text(encoding="utf-8").splitlines():
                _line = _line.strip()
                if _line and not _line.startswith("#") and "=" in _line:
                    _k, _v = _line.split("=", 1)
                    os.environ.setdefault(_k.strip(), _v.strip().strip("'\""))
        except OSError:
            pass

HOSTS = {
    "eu": ("https://eu.battle.net/oauth/token", "https://eu.api.blizzard.com"),
    "us": ("https://us.battle.net/oauth/token", "https://us.api.blizzard.com"),
}

# The realm index also lists Blizzard's internal entries -- the per-datacentre
# account/instance shards, the RDB and the arena-pass realms.  They have no
# connected realm and no players, so they are only noise in a realm map.
PSEUDO_REALM = re.compile(
    r"(account-realm|inst(bfa|sl)?$|^rdb-|^zzz|arena-pass|auxiliary|partner"
    r"|^[a-z]{2}\d+\w*gru$)", re.I)


class Blizzard:
    """Token + one GET, dynamic namespace."""

    def __init__(self, region: str = "eu", locale: str = "en_US", delay: float = 0.0):
        cid = os.getenv("BLIZZARD_CLIENT_ID", "")
        secret = os.getenv("BLIZZARD_CLIENT_SECRET", "")
        if not cid or not secret:
            raise SystemExit("falta BLIZZARD_CLIENT_ID / BLIZZARD_CLIENT_SECRET "
                             "(poe-os no .env ou exporta-os)")
        self.token_url, self.api_url = HOSTS[region]
        self.namespace = f"dynamic-{region}"
        self.locale = locale
        self.delay = max(delay, 0.0)
        self.session = requests.Session()
        self._token = ""
        self._token_exp = 0.0
        self._cid, self._secret = cid, secret
        self.calls = 0

    def access_token(self) -> str:
        if self._token and time.time() < self._token_exp - 60:
            return self._token
        r = self.session.post(self.token_url,
                              data={"grant_type": "client_credentials"},
                              auth=(self._cid, self._secret), timeout=30)
        r.raise_for_status()
        body = r.json()
        self._token = body["access_token"]
        self._token_exp = time.time() + int(body.get("expires_in", 86399))
        return self._token

    def get(self, path: str) -> dict:
        if self.delay:
            time.sleep(self.delay)
        for attempt in range(5):
            r = self.session.get(
                self.api_url + path,
                params={"namespace": self.namespace, "locale": self.locale},
                headers={"Authorization": f"Bearer {self.access_token()}"},
                timeout=30)
            if r.status_code == 404:
                raise SystemExit(f"404 em {path}")
            if r.status_code in (429, 500, 502, 503, 504):
                wait = min(2 ** attempt, 30)
                print(f"  {r.status_code} em {path}, retry em {wait}s", file=sys.stderr)
                time.sleep(wait)
                continue
            if r.status_code == 401 and attempt == 0:
                self._token_exp = 0.0
                continue
            r.raise_for_status()
            self.calls += 1
            return r.json()
        raise SystemExit(f"{path} falhou depois de 5 tentativas")


def id_from_href(href: str) -> int | None:
    """``.../connected-realm/1417?namespace=dynamic-eu`` -> 1417."""
    path = href.split("?", 1)[0].rstrip("/")
    tail = path.rsplit("/", 1)[-1] if path else ""
    return int(tail) if tail.isdigit() else None


def realm_index(api: Blizzard) -> list[dict]:
    """Every realm, as the index gives it: id / slug / name (nothing else)."""
    body = api.get("/data/wow/realm/index")
    return [{"id": r.get("id"), "slug": r.get("slug", ""), "name": r.get("name", "")}
            for r in body.get("realms", [])]


def connected_index(api: Blizzard) -> list[int]:
    """The ids of the connected groups."""
    body = api.get("/data/wow/connected-realm/index")
    ids = []
    for c in body.get("connected_realms", []):
        cid = id_from_href(c.get("href", "") if isinstance(c, dict) else str(c))
        if cid:
            ids.append(cid)
    return sorted(set(ids))


def connected_realm(api: Blizzard, cid: int) -> dict:
    """One group: its status/population and the realms that share the market."""
    body = api.get(f"/data/wow/connected-realm/{cid}")
    members = []
    for m in body.get("realms", []):
        members.append({
            "id": m.get("id"),
            "slug": m.get("slug", ""),
            "name": m.get("name", ""),
            "category": m.get("category", ""),
            "locale": m.get("locale", ""),
            "timezone": m.get("timezone", ""),
            "type": (m.get("type") or {}).get("type", ""),
            "tournament": bool(m.get("is_tournament")),
        })
    return {
        "id": body.get("id", cid),
        "has_queue": bool(body.get("has_queue")),
        "status": (body.get("status") or {}).get("type", ""),
        "population": (body.get("population") or {}).get("type", ""),
        "members": members,
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--region", default="eu", choices=sorted(HOSTS),
                    help="regiao da API (default: eu)")
    ap.add_argument("--locale", default="en_US")
    ap.add_argument("--delay", type=float, default=0.0,
                    help="pausa entre requests, segundos (default: 0)")
    ap.add_argument("--json", metavar="PATH", help="grava o dump completo em JSON")
    ap.add_argument("--out", metavar="PATH",
                    help="grava o relatorio em texto, UTF-8 (em vez de `>` do "
                         "PowerShell, que descodifica em cp1252 e estraga os acentos)")
    args = ap.parse_args(argv)

    api = Blizzard(args.region, args.locale, args.delay)

    sink = Path(args.out).open("w", encoding="utf-8") if args.out else None

    def say(*parts) -> None:
        text = " ".join(str(p) for p in parts)
        print(text)
        if sink:
            sink.write(text + "\n")

    say(f"GET /data/wow/realm/index  (namespace dynamic-{args.region})")
    realms = realm_index(api)
    say(f"  {len(realms)} realms")
    if not realms:
        return 2

    say("GET /data/wow/connected-realm/index")
    group_ids = connected_index(api)
    say(f"  {len(group_ids)} connected realms")

    say(f"GET /data/wow/connected-realm/{{{len(group_ids)}}}")
    groups: dict[int, dict] = {}
    for i, cid in enumerate(group_ids, 1):
        g = connected_realm(api, cid)
        groups[cid] = g
        names = " + ".join(m["slug"] or m["name"] for m in g["members"])
        say(f"  [{i:>3}/{len(group_ids)}] cr={cid:<5} "
            f"{g['status'] or '?':<4} {g['population'] or '?':<7} "
            f"{len(g['members'])}: {names}")

    # realm -> group, both ways
    by_id = {r["id"]: r for r in realms}
    realm_group: dict[int, int] = {}
    for cid, g in groups.items():
        for m in g["members"]:
            realm_group[m["id"]] = cid

    say()
    say(f"-- REALMS ({args.region.upper()}): nome / slug / id / connected realm / grupo")
    for r in sorted(realms, key=lambda r: r["name"].lower()):
        cid = realm_group.get(r["id"])
        others = [m["slug"] or m["name"] for m in groups[cid]["members"]
                  if m["id"] != r["id"]] if cid else []
        say(f"{r['name']:<26} {r['slug']:<26} id={r['id']:<5} cr={str(cid):<6} "
            f"{' + '.join(others) if others else '(sozinho)'}")

    orphans = [r for r in realms if r["id"] not in realm_group]
    solo = [cid for cid, g in groups.items() if len(g["members"]) <= 1]
    groups_by_size: dict[int, int] = {}
    for g in groups.values():
        groups_by_size[len(g["members"])] = groups_by_size.get(len(g["members"]), 0) + 1

    say()
    say(f"-- GRUPOS ({len(groups)}): " + ", ".join(
        f"{size} realm(s) x{n}" for size, n in sorted(groups_by_size.items())))
    say(f"-- RESUMO: {len(realms)} realms no indice, {len(groups)} connected realms, "
        f"{len(realms) - len(orphans)} realms mapeados, "
        f"{len(solo)} grupos de 1 realm")
    say(f"   fora de qualquer grupo: {len(orphans)} "
        f"({sum(1 for r in orphans if PSEUDO_REALM.search(r['slug']))} pseudo-realms "
        f"internos: -account-realm / -inst / rdb / arena-pass / auxiliar)")
    real_orphans = [r for r in orphans if not PSEUDO_REALM.search(r["slug"])]
    if real_orphans:
        say("   ATENCAO, realms a serio sem grupo: " + ", ".join(
            f"{r['slug']}({r['id']})" for r in real_orphans))
    pseudo_mapped = [r for r in realms if r["id"] in realm_group
                     and PSEUDO_REALM.search(r["slug"])]
    if pseudo_mapped:
        say("   pseudo-realms dentro de um grupo: " + ", ".join(
            r["slug"] for r in pseudo_mapped))
    say(f"   {api.calls} requests a API")

    if args.json:
        Path(args.json).parent.mkdir(parents=True, exist_ok=True)
        Path(args.json).write_text(json.dumps({
            "region": args.region,
            "fetched_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "realms": realms,
            "connected_realms": [groups[c] for c in group_ids],
            "realm_to_connected": {str(k): v for k, v in realm_group.items()},
        }, indent=2, ensure_ascii=False), encoding="utf-8")
        say(f"   dump -> {args.json}")
    if sink:
        sink.close()
        print(f"   relatorio -> {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
