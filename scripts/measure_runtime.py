"""Measure the cost of the operations that read the operational databases.

Reports wall time, peak Python allocation (tracemalloc) and peak resident set
size, so a change to how data is stored or read can be compared like for like.

Usage:
    python scripts/measure_runtime.py --op tsm-history
    python scripts/measure_runtime.py --op tsm-history --op build-rows --json out.json

Only read-only paths are exercised.
"""

from __future__ import annotations

import argparse
import gc
import json
import os
import sys
import time
import tracemalloc
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


# -- resident set size ----------------------------------------------------
def _rss_bytes() -> int:
    """Current RSS in bytes, without a third-party dependency."""
    if sys.platform == "win32":
        import ctypes
        from ctypes import wintypes

        class PROCESS_MEMORY_COUNTERS(ctypes.Structure):
            _fields_ = [
                ("cb", wintypes.DWORD),
                ("PageFaultCount", wintypes.DWORD),
                ("PeakWorkingSetSize", ctypes.c_size_t),
                ("WorkingSetSize", ctypes.c_size_t),
                ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                ("PagefileUsage", ctypes.c_size_t),
                ("PeakPagefileUsage", ctypes.c_size_t),
            ]

        counters = PROCESS_MEMORY_COUNTERS()
        counters.cb = ctypes.sizeof(PROCESS_MEMORY_COUNTERS)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.GetCurrentProcess.restype = wintypes.HANDLE
        handle = kernel32.GetCurrentProcess()
        # Without explicit argtypes the handle is truncated to 32 bits and the
        # call silently fails, which is how this returned 0 the first time.
        get_info = ctypes.WinDLL("psapi", use_last_error=True).GetProcessMemoryInfo
        get_info.argtypes = [
            wintypes.HANDLE,
            ctypes.POINTER(PROCESS_MEMORY_COUNTERS),
            wintypes.DWORD,
        ]
        get_info.restype = wintypes.BOOL
        if get_info(handle, ctypes.byref(counters), counters.cb):
            return int(counters.WorkingSetSize)
        return 0
    try:
        with open("/proc/self/statm", "r", encoding="ascii") as fh:
            pages = int(fh.read().split()[1])
        return pages * os.sysconf("SC_PAGE_SIZE")
    except (OSError, ValueError, IndexError):
        return 0


def measure(name: str, fn: Callable[[], Any]) -> Dict[str, Any]:
    """Run ``fn`` once and report what it cost."""
    gc.collect()
    rss_before = _rss_bytes()
    tracemalloc.start()
    started = time.perf_counter()
    try:
        result = fn()
        error = None
    except Exception as exc:  # noqa: BLE001 - we report, we do not swallow
        result = None
        error = f"{type(exc).__name__}: {exc}"
    elapsed = time.perf_counter() - started
    current, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    rss_after = _rss_bytes()

    out: Dict[str, Any] = {
        "operation": name,
        "seconds": round(elapsed, 3),
        "peak_traced_mb": round(peak / 1e6, 1),
        "rss_before_mb": round(rss_before / 1e6, 1),
        "rss_after_mb": round(rss_after / 1e6, 1),
        "rss_delta_mb": round((rss_after - rss_before) / 1e6, 1),
        "error": error,
    }
    if isinstance(result, dict):
        out["result"] = result
    return out


# -- operations -----------------------------------------------------------
def op_tsm_history() -> Dict[str, Any]:
    from woweconomy.ml.pipeline import DfaLearningEngine

    engine = DfaLearningEngine()
    engine.load()
    history = engine.history
    series = getattr(history, "_series", {}) if history else {}
    changes = sum(len(s.changes) for s in series.values())
    keys = sum(
        sum(len(v) for v in s.changes.values()) for s in series.values()
    )
    return {
        "series": len(series),
        "snapshots_with_changes": changes,
        "change_rows_in_memory": keys,
    }


def op_build_rows() -> Dict[str, Any]:
    from woweconomy.ml.pipeline import DfaLearningEngine

    engine = DfaLearningEngine()
    frame, report, _link = engine.build_rows()
    return {
        "rows": int(len(frame)),
        "columns": int(len(frame.columns)),
        "report": getattr(report, "to_dict", lambda: {})(),
    }


OPS: Dict[str, Callable[[], Dict[str, Any]]] = {
    "tsm-history": op_tsm_history,
    "build-rows": op_build_rows,
}


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--op", action="append", choices=sorted(OPS),
                    help="operation to measure (repeatable)")
    ap.add_argument("--json", help="write results here")
    args = ap.parse_args(argv)

    names = args.op or ["tsm-history"]
    results = []
    for name in names:
        print(f"measuring {name} ...", flush=True)
        results.append(measure(name, OPS[name]))

    for r in results:
        print(
            f"\n{r['operation']}: {r['seconds']}s  "
            f"peak-traced {r['peak_traced_mb']}MB  "
            f"rss +{r['rss_delta_mb']}MB ({r['rss_after_mb']}MB)"
        )
        if r.get("error"):
            print(f"  ERROR {r['error']}")
        if r.get("result"):
            print(f"  {json.dumps(r['result'])[:400]}")

    if args.json:
        Path(args.json).write_text(json.dumps(results, indent=2), encoding="utf-8")
        print(f"\nwrote {args.json}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
