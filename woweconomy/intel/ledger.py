"""An append-only ledger for hypotheses, actions, snapshots and outcomes.

A stress test is only evidence if the sequence can be audited later.
This ledger writes one JSON object per line, chained by SHA-256:

    hash_n = sha256(hash_{n-1} + canonical(record_n))

Editing a record invalidates every later hash, so ``verify`` can say
whether the log is intact.  It does not make the log unforgeable -- anyone
can recompute the chain -- but it does catch an accidental edit and it
makes the order of events explicit.

``leakage_report`` is the guard the ML brief asks for: a record that
states a fit/train window and an evaluation/test window which overlap is a
leak, and is reported as one.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

GENESIS = "0" * 64
KINDS = ("hypothesis", "action", "market_snapshot", "outcome", "decision", "note")


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _canonical(value: Dict[str, Any]) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, default=str)


def _hash(prev_hash: str, body: Dict[str, Any]) -> str:
    return hashlib.sha256((prev_hash + _canonical(body)).encode("utf-8")).hexdigest()


@dataclass
class LedgerRecord:
    seq: int
    ts: str
    kind: str
    payload: Dict[str, Any]
    prev_hash: str
    hash: str

    def body(self) -> Dict[str, Any]:
        return {"seq": self.seq, "ts": self.ts, "kind": self.kind,
                "payload": self.payload}

    def as_dict(self) -> Dict[str, Any]:
        return {**self.body(), "prev_hash": self.prev_hash, "hash": self.hash}


def read_records(path: Path | str) -> List[LedgerRecord]:
    records: List[LedgerRecord] = []
    file = Path(path)
    if not file.exists():
        return records
    for line in file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            raw = json.loads(line)
        except json.JSONDecodeError:
            continue
        try:
            records.append(LedgerRecord(
                seq=int(raw["seq"]), ts=str(raw["ts"]), kind=str(raw["kind"]),
                payload=raw.get("payload") or {}, prev_hash=str(raw["prev_hash"]),
                hash=str(raw["hash"])))
        except (KeyError, TypeError, ValueError):
            continue
    return records


class ExperimentLedger:
    """Append and verify one experiment's history."""

    def __init__(self, path: Path | str):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def _tail(self) -> Tuple[int, str]:
        records = read_records(self.path)
        if not records:
            return 0, GENESIS
        return records[-1].seq, records[-1].hash

    def append(self, kind: str, payload: Dict[str, Any],
               ts: Optional[str] = None) -> LedgerRecord:
        if kind not in KINDS:
            raise ValueError(f"unknown ledger kind {kind!r}; expected one of {KINDS}")
        seq, prev = self._tail()
        body = {"seq": seq + 1, "ts": ts or _utcnow(), "kind": kind,
                "payload": dict(payload or {})}
        record = LedgerRecord(seq=body["seq"], ts=body["ts"], kind=kind,
                              payload=body["payload"], prev_hash=prev,
                              hash=_hash(prev, body))
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record.as_dict(), ensure_ascii=False,
                                    default=str) + "\n")
        return record

    def records(self) -> List[LedgerRecord]:
        return read_records(self.path)

    def verify(self) -> Dict[str, Any]:
        """Recompute the chain.  The first broken record stops the check."""
        records = self.records()
        prev = GENESIS
        for index, record in enumerate(records, start=1):
            if record.seq != index:
                return {"ok": False, "at": index,
                        "error": f"sequence {record.seq} != {index}"}
            if record.prev_hash != prev:
                return {"ok": False, "at": index, "error": "prev_hash mismatch"}
            expected = _hash(prev, record.body())
            if expected != record.hash:
                return {"ok": False, "at": index, "error": "record hash mismatch"}
            prev = record.hash
        return {"ok": True, "records": len(records), "head": prev}

    def outcome(self, hypothesis_seq: int, result: Dict[str, Any],
                verdict: str = "") -> LedgerRecord:
        return self.append("outcome", {"hypothesis_seq": hypothesis_seq,
                                       "result": dict(result),
                                       "verdict": verdict})


def _window(value: Any) -> Optional[Tuple[int, int]]:
    if isinstance(value, (list, tuple)) and len(value) == 2:
        try:
            return int(value[0]), int(value[1])
        except (TypeError, ValueError):
            return None
    return None


def leakage_report(records: Sequence[LedgerRecord]) -> Dict[str, Any]:
    """Find train/fit windows that overlap an evaluation window."""
    pairs = (("train_window", "test_window"), ("fit_window", "eval_window"))
    findings: List[Dict[str, Any]] = []
    for record in records:
        payload = record.payload or {}
        for train_key, test_key in pairs:
            train = _window(payload.get(train_key))
            test = _window(payload.get(test_key))
            if not train or not test:
                continue
            if train[0] <= test[1] and test[0] <= train[1]:
                findings.append({
                    "seq": record.seq, "kind": record.kind,
                    "train_window": list(train), "test_window": list(test),
                    "reason": "the fit window overlaps the evaluation window",
                })
    return {"ok": not findings, "findings": findings,
            "note": ("a temporal split is only a holdout when the fit window"
                     " ends before the evaluation window begins")}
