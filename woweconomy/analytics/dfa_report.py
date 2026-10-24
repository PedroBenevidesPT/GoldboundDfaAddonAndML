"""Generate the collector + market report from the two databases.

The report answers two questions that live in different trees: what the
collectors we sampled actually look like per realm (``dfa.db``, written by the
scanner in ``C:\\economics``) and what the transmogs they still lack are worth
in each realm with a TSM snapshot (``tsm_markets.db``, this project).

Why the depth queries are duplicated here
    ``dataforazeroth depths`` computes the same thing, but it lives in the other
    package and cannot be imported (both are called ``woweconomy``).  Rather
    than parse its printed output, the two SQL queries are repeated below with
    the same definition -- "the realm's N-th best collector on that ladder" --
    and `tests/test_dfa_report.py` pins the numbers so the two cannot drift
    silently.

What the report refuses to claim
    TSM has price and supply per realm but velocity only per region, so the
    market half measures what is ASKED.  The report says that in every table
    that could be misread, and lists what is missing (per-realm demand, the
    realms TSM never scanned, real mog sales) instead of filling the gap with a
    guess.

Run
    python -m woweconomy.analytics.dfa_report
    python -m woweconomy.analytics.dfa_report --out reports/realm_report.md
    python -m woweconomy.analytics.dfa_report --depth 50 --min-score 60
"""

from __future__ import annotations

import argparse
import csv
import sqlite3
import statistics
import sys
import time
import zlib
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

from woweconomy.analytics import realm_market as rm

COPPER_PER_GOLD = 10000.0
DEFAULT_MIN_SCORE = 70.0
DEFAULT_DEPTH = 100
DEFAULT_THRESHOLD = 1_000_000
SAMPLE_TOP = 6

# Present in every file this module writes, and in no hand-written one: it is
# how `_output_path` tells its own output from a report somebody typed, so the
# default run can never eat the latter.
GENERATED_MARKER = "por `python -m woweconomy.analytics.dfa_report`"


def _output_path(preferred: Path) -> Path:
    """The file to write, which is not always the one asked for.

    The dated default (``reports/realm_report_<data>.md``) is the same name a
    hand-written report of that date uses, and clobbering it would lose text
    nobody has a copy of.  A file carrying this module's own marker is ours and
    is overwritten as always; anything else gets a sibling ``.auto.md`` and a
    line explaining why.  ``--out`` is explicit and always honoured.
    """
    if not preferred.exists():
        return preferred
    try:
        head = preferred.read_text(encoding="utf-8", errors="replace")[:800]
    except OSError:
        return preferred
    if GENERATED_MARKER in head:
        return preferred
    alt = preferred.with_suffix(".auto.md")
    print(f"aviso: {preferred.name} nao foi escrito por este comando "
          f"(relatorio a mao) -- escrevo antes para {alt.name}; "
          f"usa --out para escolher outro nome")
    return alt


# ----------------------------------------------------------------------
# dfa.db (read-only: the scan may be writing)
# ----------------------------------------------------------------------

def _ro(db: Path) -> sqlite3.Connection:
    """Read-only, on whichever backend holds the store."""
    from woweconomy.dbcompat import connect_store

    return connect_store(db, readonly=True)


@dataclass
class SampleRealm:
    realm: str
    accounts: int
    average: float
    best: int


@dataclass
class DepthRealm:
    board: str
    realm: str
    listed: int
    scored: int
    at_depth: Optional[int]
    above: int
    best: int
    average: float

    @property
    def local(self) -> bool:
        return "/" in self.board

    @property
    def ladder(self) -> str:
        return self.board.split("/", 1)[1] if self.local else self.board


@dataclass
class OwnedRealm:
    """One realm's stored collections: how many accounts, and their bitmaps."""

    realm: str
    accounts: int
    slots: int
    blobs: List[bytes]
    scores: List[int]


@dataclass
class DfaFacts:
    accounts: int
    scanned_kinds: Dict[str, int]
    boards: List[Tuple[str, int]]
    sample: List[SampleRealm]
    depths: List[DepthRealm]
    missing_realm_rows: int
    by_realm_accounts: int
    max_rank: Dict[str, int] = field(default_factory=dict)
    owned: List[OwnedRealm] = field(default_factory=list)
    slots: Dict[int, int] = field(default_factory=dict)


def read_dfa(db: Path, depth: int, threshold: int) -> DfaFacts:
    con = _ro(db)
    try:
        accounts = con.execute("SELECT COUNT(*) FROM account").fetchone()[0]
        scanned = dict(con.execute(
            "SELECT kind, COUNT(*) FROM scanned GROUP BY kind").fetchall())
        boards = con.execute(
            "SELECT board, COUNT(*) FROM board_member "
            "GROUP BY board ORDER BY 2 DESC").fetchall()
        sample = [
            SampleRealm(realm=realm, accounts=n,
                        average=float(avg or 0), best=int(best or 0))
            for realm, n, avg, best in con.execute(
                "SELECT a.realm, COUNT(*), AVG(c.score), MAX(c.score) "
                "FROM account a LEFT JOIN collector c ON c.region = a.region "
                "  AND c.realm = a.realm AND c.name = a.name "
                "GROUP BY a.realm ORDER BY 2 DESC, 1")
        ]
        depths = _depth_rows(con, depth, threshold)
        missing_rows = con.execute(
            "SELECT COUNT(*) FROM missing_realm").fetchone()[0]
        by_realm = con.execute(
            "SELECT COALESCE(SUM(by_realm), 0) FROM scanned").fetchone()[0]
        # How deep the scan actually went per realm: the last rank it read on
        # that realm's ladder.  Without it a realm's average is unreadable,
        # because a ladder walked to rank 2335 drags its mean down.
        max_rank = dict(con.execute(
            "SELECT realm, MAX(rank) FROM board_member WHERE rank IS NOT NULL "
            "GROUP BY realm"))
        owned = _owned_by_realm(con, "appearances")
        try:
            slots = dict(con.execute(
                "SELECT item_id, slot FROM item_slot WHERE kind='appearances'"))
        except sqlite3.OperationalError:
            slots = {}
    finally:
        con.close()
    return DfaFacts(accounts=accounts, scanned_kinds=scanned, boards=boards,
                    sample=sample, depths=depths,
                    missing_realm_rows=missing_rows,
                    by_realm_accounts=int(by_realm or 0),
                    max_rank={str(k): int(v) for k, v in max_rank.items()},
                    owned=owned,
                    slots={int(k): int(v) for k, v in slots.items()})


def _owned_by_realm(con: sqlite3.Connection, kind: str) -> List[OwnedRealm]:
    """Read every stored collection bitmap, grouped by the account's realm.

    The table only exists once ``collections`` (or ``scan --keep-collections``)
    has written something, so a database from before that returns nothing and
    the report says so instead of failing.
    """
    try:
        rows = con.execute(
            "SELECT a.realm, o.bits, o.slots, COALESCE(c.score, 0) "
            "FROM owned o JOIN account a ON a.account = o.account "
            "LEFT JOIN collector c ON c.region = a.region AND c.realm = a.realm "
            "  AND c.name = a.name WHERE o.kind = ? ORDER BY a.realm, "
            "COALESCE(c.score, 0) DESC", (kind,)).fetchall()
    except sqlite3.OperationalError:
        return []
    out: Dict[str, OwnedRealm] = {}
    for realm, bits, slots, score in rows:
        entry = out.setdefault(str(realm), OwnedRealm(
            realm=str(realm), accounts=0, slots=int(slots or 0), blobs=[],
            scores=[]))
        entry.accounts += 1
        entry.slots = max(entry.slots, int(slots or 0))
        entry.blobs.append(bits)
        entry.scores.append(int(score or 0))
    return list(out.values())


def _depth_rows(con: sqlite3.Connection, depth: int,
                threshold: int) -> List[DepthRealm]:
    """Realm's N-th best: ladder from board_member, score from collector.

    Same definition as ``dataforazeroth depths``: ordering the realm's own
    characters by ladder position and taking the N-th.  Nothing is inferred
    when the realm has fewer than N.
    """
    rows = con.execute(
        "SELECT bm.board, bm.realm, COUNT(*) listed, COUNT(c.score) scored, "
        "       MAX(c.score) best, AVG(c.score) average, "
        "       SUM(CASE WHEN c.score >= ? THEN 1 ELSE 0 END) above "
        "FROM board_member bm "
        "LEFT JOIN collector c ON c.region = bm.region AND c.realm = bm.realm "
        "  AND c.name = bm.name "
        "WHERE bm.rank IS NOT NULL GROUP BY bm.board, bm.realm",
        (threshold,)).fetchall()
    out: List[DepthRealm] = []
    for board, realm, listed, scored, best, average, above in rows:
        at = con.execute(
            "SELECT c.score FROM board_member bm "
            "JOIN collector c ON c.region = bm.region AND c.realm = bm.realm "
            "  AND c.name = bm.name "
            "WHERE bm.board=? AND bm.realm=? AND c.score IS NOT NULL "
            "ORDER BY bm.rank LIMIT 1 OFFSET ?",
            (board, realm, max(depth, 1) - 1)).fetchone()
        out.append(DepthRealm(board=board, realm=realm, listed=listed or 0,
                              scored=scored or 0,
                              at_depth=at[0] if at else None,
                              above=above or 0, best=int(best or 0),
                              average=float(average or 0)))
    return out


# ----------------------------------------------------------------------
# tsm_markets.db
# ----------------------------------------------------------------------

@dataclass
class TsmFacts:
    rows: int
    items: int
    realms: List[str]
    scanned_at: str
    journal: Tuple[int, int, int, int]
    # The realm the journal's own sales come from: the report's only evidence of
    # where the reader actually trades, and what makes "your realm" a fact
    # instead of an assumption.
    home_realm: str = ""


def read_tsm(db: Path) -> TsmFacts:
    from woweconomy.dbcompat import connect_store

    con = connect_store(db, readonly=True)
    try:
        rows = con.execute("SELECT COUNT(*) FROM tsm_market").fetchone()[0]
        items = con.execute(
            "SELECT COUNT(DISTINCT itemId) FROM tsm_market").fetchone()[0]
        realms = [r for (r,) in con.execute(
            "SELECT DISTINCT realm FROM tsm_market ORDER BY realm")
            if not any(tag in r for tag in rm._NOT_A_REALM)]
        stamps = []
        for key, value in con.execute(
                "SELECT key, value FROM tsm_meta WHERE key LIKE 'dl_%DATA_%'"):
            try:
                stamps.append(int(value))
            except (TypeError, ValueError):
                continue
        scanned_at = (time.strftime("%Y-%m-%d %H:%M", time.gmtime(max(stamps)))
                      + " UTC") if stamps else "?"
        journal = con.execute(
            "SELECT COUNT(*), COALESCE(SUM(sold_count),0), "
            "       COALESCE(SUM(failures),0), COALESCE(SUM(sold_copper),0) "
            "FROM journal_stats").fetchone()
        home = con.execute(
            "SELECT realm, COUNT(*) FROM journal_stats WHERE realm <> '' "
            "GROUP BY realm ORDER BY 2 DESC, 1 LIMIT 1").fetchone()
    finally:
        con.close()
    return TsmFacts(rows=rows, items=items, realms=realms,
                    scanned_at=scanned_at, journal=journal,
                    home_realm=str(home[0]) if home else "")


# ----------------------------------------------------------------------
# Market table (reuses realm_market so the numbers cannot diverge)
# ----------------------------------------------------------------------

@dataclass
class MarketRow:
    label: str
    realms: Tuple[str, ...]
    items: int
    cover: float
    value: float
    premium: float
    supply: float
    capped: int
    liquid: int = 0            # items with >= 3 listings
    liquid_share: float = 0.0
    liquid_premium: float = 0.0


def market_rows(basket, groups, markets, common, cap) -> List[MarketRow]:
    ref = rm.reference_prices(basket, markets)
    scores, _common, _g = rm.score_realms(basket, cap, groups)
    by_realm = {s.realm: s for s in scores}

    def wmed(pairs):
        return rm._weighted_median(pairs)

    out: List[MarketRow] = []
    for realm in markets:
        scored = by_realm.get(realm)
        if scored is None:
            continue
        priced = [i for i in common if i.realm_prices.get(realm)]
        liquid = [i for i in priced if i.realm_auctions.get(realm, 0) >= 3
                  and ref.get(i.appearance_id)]
        prem = wmed([(rm._weight(i),
                      rm.cap_price(i.realm_prices[realm], ref[i.appearance_id], cap)
                      / ref[i.appearance_id])
                     for i in liquid]) if liquid else 0.0
        out.append(MarketRow(
            label=rm.group_label(groups[realm]), realms=groups[realm],
            items=scored.items, cover=scored.cover, value=scored.value,
            premium=scored.premium, supply=scored.supply,
            capped=scored.capped, liquid=len(liquid),
            liquid_share=100.0 * len(liquid) / len(priced) if priced else 0.0,
            liquid_premium=prem,
        ))
    return out


# ----------------------------------------------------------------------
# Rendering
# ----------------------------------------------------------------------

def _n(value: Optional[float]) -> str:
    if value is None:
        return "—"
    return f"{value:,.0f}".replace(",", " ")


def _money(value: float) -> str:
    return f"{value:,.0f}g".replace(",", " ")


def _pct(value: float) -> str:
    return f"{value:.1f}%".replace(".", ",")


def _ratio(value: float) -> str:
    return f"{value:.2f}×".replace(".", ",")


def _verdict(row: MarketRow, depth: Optional[int] = None) -> str:
    """Mechanical reading of the three measured axes, no editorial invention."""
    thin = row.liquid_share < 50
    expensive = row.premium >= 1.5
    if thin and expensive and depth:
        return ("fino e caro, mas com coleccionadores: margem com paciência, "
                "só no que tem valor alto")
    if thin and expensive:
        return "fino e caro: pouca prateleira, pedidos altos -- margem com paciência"
    if thin:
        return "fino: mercado raso, preço sem sinal"
    if expensive:
        return "caro com prateleira: testar com lote pequeno"
    if depth and depth > 0:
        return "fundo, barato e com compradores: rotação"
    return "fundo e barato: rotação, margem baixa"


def _above(row: MarketRow, at: Dict[str, DepthRealm]) -> int:
    """How many collectors of that market are above the score cut."""
    return max((at[r].above for r in row.realms if r in at and at[r].local),
               default=0)


def _collectors_cell(row: MarketRow, at: Dict[str, DepthRealm],
                     every: Dict[str, DepthRealm], depth: int, cut: str) -> str:
    """The collector column: local depth when there is one, elite otherwise.

    A realm that only ever showed up on the region ladder has a *count* of
    accounts above the cut and no depth at all -- its rank is a position in the
    region, so the N-th best of that realm cannot be read from it.  Saying so is
    the difference between a comparison and a coincidence.
    """
    local = _market_depth(row, at)
    if local:
        return f"{_n(local)} @{depth}, {_above(row, at)} ≥ {cut}"
    regional = max((every[r].above for r in row.realms
                    if r in every and not every[r].local), default=0)
    if regional:
        return f"só elite regional ({regional} ≥ {cut})"
    return "sem escada nenhuma"


def _market_depth(row: MarketRow, at: Dict[str, DepthRealm]) -> Optional[int]:
    """The market's own depth: the best N-th score among the realms on it.

    A market is an auction house, so a connected group contributes the deepest
    ladder it has; realms that only ever appear on the region ladder have no
    local depth at all and are not guessed at.
    """
    depths = [at[r].at_depth for r in row.realms
              if r in at and at[r].local and at[r].at_depth]
    return max(depths) if depths else None


def market_wants(facts: DfaFacts, rows: List[MarketRow], common,
                 top: int = 10) -> Dict[str, Tuple[int, List[Tuple[str, int, float, Optional[float]]]]]:
    """What each market still wants, from the stored collections.

    A bitmap answers this offline: an item is wanted by an account when its bit
    is 0, and the account's own `slots` covers the catalogue it was written
    against.  Only the priced basket is scored -- the whole appearance catalogue
    would rank farming targets, not things anyone can list today.  Markets whose
    realms have no bitmap are left out; the report says so and prints the
    command instead of a zero.
    """
    if not facts.owned or not facts.slots:
        return {}
    slots = {i.appearance_id: facts.slots[i.appearance_id] for i in common
             if i.appearance_id in facts.slots}
    if not slots:
        return {}
    by_app = {i.appearance_id: i for i in common}
    names = {a: (i.name or f"#{a}") for a, i in by_app.items()}
    out: Dict[str, Tuple[int, List[Tuple[str, int, float, Optional[float]]]]] = {}
    for row in rows:
        counts = dict.fromkeys(slots, 0)
        accounts = 0
        for owned in facts.owned:
            if owned.realm not in row.realms:
                continue
            for blob in owned.blobs:
                accounts += 1
                try:
                    raw = zlib.decompress(blob)
                except zlib.error:
                    continue
                for item_id, slot in slots.items():
                    byte = raw[slot >> 3] if slot >> 3 < len(raw) else 0
                    if not (byte >> (slot & 7) & 1):
                        counts[item_id] += 1
        if not accounts:
            continue
        ranked: List[Tuple[str, int, float, Optional[float]]] = []
        for item_id, count in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[:top]:
            if count <= 0:
                break
            price = None
            item = by_app.get(item_id)
            for realm in row.realms:
                if item is not None and item.realm_prices.get(realm):
                    price = item.realm_prices[realm] / COPPER_PER_GOLD
                    break
            ranked.append((names.get(item_id, f"#{item_id}"), count,
                           100.0 * count / accounts, price))
        if ranked:
            out[row.label] = (accounts, ranked)
    return out


def _recommendation(rows: List[MarketRow], at: Dict[str, DepthRealm],
                    home: str) -> List[str]:
    """The operational reading, every line traceable to a column above."""
    out: List[str] = []
    if not rows:
        return out
    with_depth = [r for r in rows if _market_depth(r, at)] or rows
    volume = max(with_depth, key=lambda r: (r.liquid_share, -r.premium))
    depth = _market_depth(volume, at)
    out.append(
        f"1. **Rotação:** listar em **{volume.label}** ao preço da mediana "
        f"({_ratio(volume.premium)}), aceitando a margem baixa. É onde o item "
        f"tem mais probabilidade de encontrar comprador: {_pct(volume.liquid_share)} "
        f"dos itens têm 3+ listagens"
        + (f" e o {DEFAULT_DEPTH}.º melhor coleccionador vale {_n(depth)}"
           if depth else "") + ".")
    own = next((r for r in rows if home and (home in r.realms or r.label == home)),
               None)
    if own is not None:
        out.append(
            f"2. **Margem:** no teu reino ({home}, do diário) o mercado pede "
            f"{_ratio(own.premium)} com {_pct(own.liquid_share)} de prateleira. "
            f"É onde há menos gente a competir: manter aqui só o que tem valor "
            f"alto e baixa frequência, com duração longa.")
    mid = [r for r in rows if 1.2 <= r.premium < 2.0 and r.liquid_share >= 25.0]
    if mid:
        pick = max(mid, key=lambda r: (r.liquid_share, r.premium))
        out.append(
            f"3. **Teste controlado:** {pick.label} é o único mercado "
            f"intermédio com preço e volume na mesma frase ({_ratio(pick.premium)} "
            f"com {_pct(pick.liquid_share)} de prateleira). Um lote de 10–20 itens "
            f"mede se o prémio é alcançável antes de se apostar nele.")
    dear = max(rows, key=lambda r: r.premium)
    out.append(
        f"4. **Não ler {_ratio(dear.premium)} ({dear.label}) como «vende-se mais "
        f"caro lá».** Significa «pede-se mais»: a prova de venda continua a ser "
        f"`saleRate`/`soldPerDay` (regional, não por reino) e o diário.")
    return out


def render(facts: DfaFacts, tsm: TsmFacts, rows: List[MarketRow],
           counters: Dict[str, int], basket_size: int, common_size: int,
           depth: int, threshold: int, min_score: float, cap: float,
           source_dfa: Path, source_tsm: Path,
           wanted: Optional[Dict[str, Tuple[int, List[Tuple[str, int, float, Optional[float]]]]]] = None
           ) -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    local = [d for d in facts.depths if d.local]
    complete = [d for d in local
                if d.at_depth is not None and d.listed >= depth]
    partial = [d for d in local if d not in complete]
    complete.sort(key=lambda d: (-(d.at_depth or 0), -d.listed, d.realm))
    partial.sort(key=lambda d: (-d.listed, d.realm))
    regional = sorted((d for d in facts.depths if not d.local),
                      key=lambda d: (-d.above, -d.best, d.realm))
    at = {d.realm: d for d in complete}
    # Every realm the scan saw, local ladder first and the biggest row as the
    # tie-break: the cross table needs the regional-only realms too, and for
    # them only the count above the cut is readable.
    every: Dict[str, DepthRealm] = {}
    for d in facts.depths:
        cur = every.get(d.realm)
        if (cur is None or (d.local and not cur.local)
                or (d.local == cur.local and d.above > cur.above)):
            every[d.realm] = d

    out: List[str] = []
    out.append("# Relatório — coleccionadores e mercados de transmog")
    out.append("")
    out.append(f"**Gerado:** {stamp} por `python -m woweconomy.analytics.dfa_report`")
    out.append(f"**Fontes:** `{source_dfa}` e `{source_tsm}`")
    out.append("")

    # ---- summary -----------------------------------------------------
    out.append("## Sumário")
    out.append("")
    if complete:
        top, bottom = complete[0], complete[-1]
        out.append(
            f"1. À mesma profundidade (o {depth}.º melhor), "
            f"**{top.realm}** vale {_n(top.at_depth)} e o último reino "
            f"comparável, **{bottom.realm}**, {_n(bottom.at_depth)} — "
            f"{_ratio((top.at_depth or 0) / (bottom.at_depth or 1))}.")
    if facts.sample:
        big = max(facts.sample, key=lambda s: s.accounts)
        best_avg = max(facts.sample, key=lambda s: s.average)
        out.append(
            f"2. A amostra (`realms --sample`) tem {facts.accounts} contas; "
            f"{big.realm} domina com {big.accounts} e média {_n(big.average)}, "
            f"mas a média mais alta é {best_avg.realm} ({_n(best_avg.average)}) "
            f"com {best_avg.accounts} contas: a média mede a profundidade lida.")
    if rows:
        cheap = min(rows, key=lambda r: r.premium)
        dear = max(rows, key=lambda r: r.premium)
        out.append(
            f"3. O cabaz de {common_size} transmogs comparáveis pede entre "
            f"{_ratio(cheap.premium)} em **{cheap.label}** e "
            f"{_ratio(dear.premium)} em **{dear.label}** a mediana entre "
            f"mercados.")
        deepest = max(rows, key=lambda r: r.liquid_share)
        thin = min(rows, key=lambda r: r.liquid_share)
        out.append(
            f"4. O mercado mais fundo é **{deepest.label}** "
            f"({_pct(deepest.liquid_share)} dos itens com 3+ listagens); o mais "
            f"fino é **{thin.label}** ({_pct(thin.liquid_share)}), e é onde os "
            f"pedidos são mais altos ou mais irreais.")
        out.append(
            f"5. O prémio sobrevive ao filtro de liquidez: "
            f"**{max(rows, key=lambda r: r.liquid_premium).label}** "
            f"{_ratio(max(rows, key=lambda r: r.liquid_premium).liquid_premium)} "
            f"e **{max(rows, key=lambda r: r.premium).label}** "
            f"{_ratio(max(rows, key=lambda r: r.premium).premium)} com 3+ "
            f"listagens. É pedido, não venda: o TSM não tem velocidade por reino.")
    out.append("")

    # ---- data used ---------------------------------------------------
    out.append("## Dados usados")
    out.append("")
    out.append("| fonte | o que tem | idade |")
    out.append("|---|---|---|")
    kinds = ", ".join(f"{k} {v}" for k, v in sorted(facts.scanned_kinds.items()))
    boards = ", ".join(f"`{b}` {n}" for b, n in facts.boards)
    out.append(f"| `dfa.db` | {facts.accounts} contas; escaneado: {kinds}; "
               f"boards: {boards} | scan pode estar a correr |")
    out.append(f"| `tsm_markets.db` | {_n(tsm.rows)} linhas, {_n(tsm.items)} itens, "
               f"{len(tsm.realms)} nomes de reino | scan de {tsm.scanned_at} |")
    out.append(f"| demanda | {counters['wanted']} aparições com valor ≥ "
               f"{min_score:g}; {basket_size} com preço; {common_size} "
               f"comparáveis nos {len(rows)} mercados | gerada do `dfa.db` |")
    out.append("")
    out.append(f"Fora do cabaz: {counters['fora_bop']} BoP (nunca vão ao leilão), "
               f"{counters['fora_era']} era-Classic (preço de outro jogo), "
               f"{counters['sem_preco']} sem preço TSM em reino nenhum, "
               f"{counters['sem_item']} sem item BoE no DB2.")
    out.append("")

    # ---- sample vs depth --------------------------------------------
    out.append("## Porque a amostra engana")
    out.append("")
    out.append("| reino | contas | média | @N | ≥ corte | máximo | lido até |")
    out.append("|---|---:|---:|---:|---:|---:|---:|")
    for s in facts.sample[:SAMPLE_TOP]:
        d = at.get(s.realm)
        out.append(f"| {s.realm} | {s.accounts} | {_n(s.average)} | "
                   f"{_n(d.at_depth) if d else '—'} | "
                   f"{d.above if d else '—'} | {_n(s.best)} | "
                   f"{_n(facts.max_rank.get(s.realm)) if facts.max_rank.get(s.realm) else '—'} |")
    out.append("")
    out.append(f"`@N` é o score do {depth}.º melhor daquele reino na própria "
               f"escada; a média é sobre *tudo o que foi lido*, e por isso cai "
               f"quando o scan desce a escada (`lido até` é o rank mais fundo que "
               f"aquela escada recebeu). Reinos sem escada local aparecem com "
               f"`—`: as suas linhas vêm da escada regional, onde o rank é a "
               f"posição na região.")
    out.append("")

    # ---- depth tables -------------------------------------------------
    out.append(f"## Qualidade a profundidade fixa (`--depth {depth}`)")
    out.append("")
    if complete:
        out.append("| # | reino | escada | chars | @N | média | ≥ corte | % | máximo |")
        out.append("|---:|---|---|---:|---:|---:|---:|---:|---:|")
        for i, d in enumerate(complete, 1):
            share = 100.0 * d.above / d.scored if d.scored else 0.0
            out.append(f"| {i} | {d.realm} | {d.ladder} | {d.listed} | "
                       f"**{_n(d.at_depth)}** | {_n(d.average)} | {d.above} | "
                       f"{_pct(share)} | {_n(d.best)} |")
    else:
        out.append(f"Nenhum reino com {depth}+ coleccionadores na escada.")
    out.append("")
    if partial:
        out.append(f"| reino | escada | chars | média | máximo |")
        out.append("|---|---|---:|---:|---:|")
        for d in partial:
            out.append(f"| {d.realm} | {d.ladder} | {d.listed} | "
                       f"{_n(d.average)} | {_n(d.best)} |")
        out.append("")
        out.append(f"Sem {depth}.º lugar nestes reinos: ficam fora do ranking "
                   f"para não comparar o 8.º com o 100.º.")
        out.append("")
    if regional:
        out.append(f"Escada regional (contagem, não profundidade): "
                   + ", ".join(f"{d.realm} {d.above}" for d in regional[:8])
                   + ".")
        out.append("")

    # ---- market -------------------------------------------------------
    out.append(f"## Mercado por mercado (cabaz comum de {common_size} itens)")
    out.append("")
    out.append("| mercado | itens | cobertura | preço mediano | vs mediana | "
               "leilões/item | cortes | com 3+ listagens | prémio (3+) |")
    out.append("|---|---:|---:|---:|---:|---:|---:|---:|---:|")
    for r in rows:
        out.append(
            f"| {r.label} | {r.items} | {_pct(r.cover)} | {_money(r.value)} | "
            f"**{_ratio(r.premium)}** | {r.supply:.0f} | {r.capped} "
            f"({_pct(100.0 * r.capped / r.items if r.items else 0)}) | "
            f"{_pct(r.liquid_share)} | {_ratio(r.liquid_premium)} |")
    out.append("")
    out.append(f"Preço cortado a {cap:g}× a mediana entre mercados; reinos "
               f"ligados aparecem como um mercado só (mesmo leilão no TSM). "
               f"`cortes` = pedidos acima desse teto; `com 3+ listagens` = itens "
               f"com prateleira a sério.")
    out.append("")

    # ---- verdicts -----------------------------------------------------
    out.append("## Leitura mecânica")
    out.append("")
    out.append("| mercado | @N (profundidade) | prémio | liquidez | leitura |")
    out.append("|---|---:|---:|---:|---|")
    for r in rows:
        out.append(f"| {r.label} | {_n(_market_depth(r, at))} | "
                   f"{_ratio(r.premium)} | "
                   f"{_pct(r.liquid_share)} | {_verdict(r, _market_depth(r, at))} |")
    out.append("")

    # ---- the two axes together ----------------------------------------
    out.append("## Onde pôr o quê (coleccionadores × mercado)")
    out.append("")
    out.append("| mercado | coleccionadores | mercado (liquidez) | leitura de uma linha |")
    out.append("|---|---|---|---|")
    cut = (f"{threshold / 1_000_000:g}M" if threshold >= 1_000_000
           else _n(threshold))
    for r in rows:
        d = _market_depth(r, at)
        collectors = _collectors_cell(r, at, every, depth, cut)
        market = f"{_pct(r.liquid_share)} com 3+ listagens, {_ratio(r.premium)}"
        out.append(f"| **{r.label}** | {collectors} | {market} | "
                   f"{_verdict(r, d)} |")
    out.append("")
    rec = _recommendation(rows, at, tsm.home_realm)
    if rec:
        out.append("**Recomendação operacional**")
        out.append("")
        for line in rec:
            out.append(line)
        out.append("")

    # ---- who wants what, from the stored collections -------------------
    out.append("## Quem quer (dos bitmaps de colecção)")
    out.append("")
    if wanted:
        out.append(f"Contas com colecção guardada: "
                   f"{sum(o.accounts for o in facts.owned)} em "
                   f"{len({o.realm for o in facts.owned})} reinos. Uma conta "
                   f"\"quer\" um item quando o bit da aparência está a 0; contados "
                   f"só os {common_size} itens do cabaz com preço.")
        out.append("")
        for label, (accounts, ranked) in wanted.items():
            out.append(f"**{label}** — {accounts} contas")
            out.append("")
            out.append("| item | querem | % | preço no mercado |")
            out.append("|---|---:|---:|---:|")
            for name, count, pct, price in ranked:
                out.append(f"| {name} | {count} | {_pct(pct)} | "
                           f"{_money(price) if price else '—'} |")
            out.append("")
    else:
        stored = sum(o.accounts for o in facts.owned)
        where = ", ".join(f"{o.realm} {o.accounts}" for o in facts.owned[:6])
        out.append("Nenhum bitmap de colecção nos reinos destes mercados — está a "
                   "única secção do relatório que responde *quem* em vez de "
                   "*quantos*, e é a que falta preencher."
                   + (f" Há bitmaps noutros reinos ({where}), mas nenhum deles tem "
                      "preço TSM neste scan." if stored else ""))
        out.append("")
        out.append("```bat")
        out.append("cd C:\\economics")
        out.append("python -m woweconomy.services.dataforazeroth collections "
                   "--kinds appearances --realm "
                   "dragonmaw,vashj,spinebreaker,haomarush,stormreaver")
        out.append("python -m woweconomy.services.dataforazeroth wants "
                   "--top 25 --realm dragonmaw")
        out.append("```")
        out.append("")
        out.append("1 pedido por conta e por kind; as de score mais alto vão "
                   "primeiro. Depois disso, `wants --item <id> --realm <reino>` "
                   "responde sem gastar pedidos, e esta secção enche-se sozinha.")
        out.append("")

    # ---- gaps ---------------------------------------------------------
    out.append("## O que falta para decidir")
    out.append("")
    scanned_realms = {s.realm for s in facts.sample if s.accounts >= 5}
    no_tsm = sorted(scanned_realms - set(tsm.realms))
    out.append(f"- **Demanda por reino:** `missing_realm` tem "
               f"{facts.missing_realm_rows} linhas; {facts.by_realm_accounts} "
               f"wishlists marcadas `by_realm`. Preencher com "
               f"`dataforazeroth reweight --realm <reino>`.")
    out.append(f"- **Preço TSM:** {len(no_tsm)} reinos com 5+ contas na amostra "
               f"não têm preço: {', '.join(no_tsm[:8])}"
               f"{'…' if len(no_tsm) > 8 else ''}.")
    out.append(f"- **Velocidade:** não existe por reino no TSM (é regional). Só "
               f"vendas reais a resolveriam; o diário tem "
               f"{tsm.journal[0]} itens, {tsm.journal[1]} unidades vendidas e "
               f"{tsm.journal[2]} falhas.")
    out.append(f"- **Profundidade dos reinos só-regionais:** "
               f"`leaderboard --realm <reino> --max-rank {depth}`, um pedido por "
               f"reino, para os trazer à tabela comparável.")
    out.append("")

    # ---- reproduce ----------------------------------------------------
    out.append("## Reproduzir")
    out.append("")
    out.append("```bat")
    out.append("cd C:\\FarmAnalysis")
    out.append("python -m woweconomy.analytics.dfa_report")
    out.append(f"python -m woweconomy.analytics.dfa_report --min-score "
               f"{min_score:g} --depth {depth} --out reports\\realm_report.md")
    out.append("```")
    out.append("")
    out.append("Os números de `dfa.db` mexem-se enquanto o scan corre; os de "
               "preço ficam parados até ao próximo scan do TSM.")
    return "\n".join(out) + "\n"


# ----------------------------------------------------------------------
# CLI
# ----------------------------------------------------------------------

def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(
        description="Gera o relatório de coleccionadores + mercados a partir de "
                    "dfa.db e tsm_markets.db.")
    ap.add_argument("--dfa-dir", default="",
                    help="pasta do scanner (default: $DFA_DIR ou a de "
                         "C:\\economics)")
    ap.add_argument("--db", default="tsm_markets.db",
                    help="base TSM (default: %(default)s)")
    ap.add_argument("--out", default="",
                    help="ficheiro markdown de saída (default: "
                         "reports/realm_report_<data>.md)")
    ap.add_argument("--min-score", type=float, default=DEFAULT_MIN_SCORE,
                    help="valor DfA mínimo do cabaz (default: %(default)s)")
    ap.add_argument("--depth", type=int, default=DEFAULT_DEPTH,
                    help="N do 'N-ésimo melhor' (default: %(default)s)")
    ap.add_argument("--score", type=int, default=DEFAULT_THRESHOLD,
                    help="corte de score para a contagem (default: %(default)s)")
    ap.add_argument("--realm-cap", type=float, default=rm.DEFAULT_REALM_CAP,
                    help="teto de preço por reino, em múltiplos da mediana "
                         "(default: %(default)s)")
    ap.add_argument("--csv", default="",
                    help="escreve também a tabela de mercados neste CSV")
    args = ap.parse_args(argv)

    dfa_dir = Path(args.dfa_dir) if args.dfa_dir else rm.default_dfa_dir()
    if dfa_dir is None or not (dfa_dir / "dfa.db").exists():
        print("error: não encontrei o dfa.db (usa --dfa-dir ou DFA_DIR)",
              file=sys.stderr)
        return 2
    if not Path(args.db).exists():
        print(f"error: base não encontrada: {args.db}", file=sys.stderr)
        return 2

    facts = read_dfa(dfa_dir / "dfa.db", args.depth, args.score)
    tsm = read_tsm(Path(args.db))
    basket, counters = rm.build_basket(dfa_dir, args.db, "appearances",
                                       args.min_score)
    groups = rm.group_realms(basket)
    markets = sorted({m[0] for m in groups.values()})
    common = rm.common_basket(basket, markets) if markets else []
    rows = market_rows(basket, groups, markets, common, args.realm_cap)

    wanted = market_wants(facts, rows, common)
    text = render(facts, tsm, rows, counters, len(basket), len(common),
                  args.depth, args.score, args.min_score, args.realm_cap,
                  dfa_dir / "dfa.db", Path(args.db), wanted)
    out = Path(args.out) if args.out else _output_path(
        Path("reports") / f"realm_report_{datetime.now().strftime('%Y-%m-%d')}.md")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(text, encoding="utf-8")
    print(f"relatório -> {out} ({len(text.splitlines())} linhas, "
          f"{len(rows)} mercados, cabaz {len(basket)}/{len(common)})")

    if args.csv:
        with Path(args.csv).open("w", encoding="utf-8", newline="") as fh:
            w = csv.writer(fh, delimiter=";")
            w.writerow(["market", "items", "coverage_pct", "median_price_gold",
                        "premium", "median_auctions", "capped", "liquid_pct",
                        "liquid_premium"])
            for r in rows:
                w.writerow([r.label, r.items, f"{r.cover:.1f}",
                            f"{r.value:.0f}", f"{r.premium:.3f}",
                            f"{r.supply:.0f}", r.capped,
                            f"{r.liquid_share:.1f}", f"{r.liquid_premium:.3f}"])
        print(f"csv -> {args.csv}")
    if not basket:
        print("error: cabaz vazio -- vê os contadores no relatório",
              file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
