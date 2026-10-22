"""Run the pipeline: one entry point, several workers, no double work.

The individual jobs already exist as scripts -- `scripts/intel.py` fetches,
`scripts/aggregates.py` rolls up, `scripts/dfa_ml.py` trains,
`scripts/dfa_pricing_export.py` writes the addon data.  What was missing is
something that runs them on a cadence without two copies of itself doing the
same job twice, and without a crash leaving a lock behind forever.

Three properties, and each one is a specific failure this avoids:

**No double work.**  Every task holds a lease while it runs
(:mod:`woweconomy.pipeline.leases`).  A second pipeline started by hand, by a
scheduler and by a login hook at the same time gets "not yours" instead of
fetching the same 92 books again.

**No permanent block after a crash.**  A lease expires.  The TTL is derived
from the task's own timeout, so a task can never outlive its lease while it
is legitimately running, and a killed task frees its lease within one TTL
rather than needing an operator.

**No lost work.**  What is due is decided from ``pipeline_stat`` in SQLite,
not from a notification or an in-memory timer.  A missed tick is not a lost
tick: the next cycle sees the task as overdue and runs it.  Redis, when
present, is a mirror for observability and never the source of truth.

Cadence
-------
Each task has an interval and the *span* of the data matters more than the
count of runs.  A sweep every hour for three days is what lets the market
panel close a 48-hour horizon; ten sweeps in one afternoon is not.  The
defaults therefore favour ``fetch`` heavily and the expensive jobs lightly:

    fetch        every 1 h   -- all 92 books plus the commodity book
    aggregates   every 6 h   -- the statistical record, rebuilt for a window
    sentinel     every 24 h  -- retrain, gated by chronological validation
    export       every 6 h   -- write the addon dataset
    deploy       after export -- copy the toc-listed files into the game

Usage
-----
    python run_pipeline.py --once              # one cycle, then exit
    python run_pipeline.py --loop              # keep cycling
    python run_pipeline.py --task fetch        # one named task
    python run_pipeline.py --status            # what ran, what holds what
    python run_pipeline.py --once --dry-run    # print the plan only

Atomic publication: the export and the deploy both write through
``write_atomic`` (temp file, fsync, ``os.replace``), so a reader never sees a
half-written dataset or a half-copied Lua file.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Optional, Sequence

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from woweconomy.intel import store                      # noqa: E402
from woweconomy.pipeline import leases                   # noqa: E402

PY = sys.executable
HOUR = 3600


# --------------------------------------------------------------------- tasks
@dataclass(frozen=True)
class TaskDef:
    """One job the pipeline knows how to run.

    ``every_s`` is the cadence; ``timeout_s`` is also the basis of the lease
    TTL, because a lease shorter than the work would let a second worker
    start the same job while the first is still running.
    """

    name: str
    argv: Sequence[str]
    every_s: int
    timeout_s: int = 1800
    #: Runs only when every task in this list has succeeded at least once.
    after: Sequence[str] = ()
    #: Not run by ``--loop`` unless explicitly named.
    optional: bool = False

    def command(self) -> List[str]:
        return [PY, *self.argv]

    @property
    def lease_ttl(self) -> float:
        # A task must never outlive its own lease.  One minute of margin
        # covers process start-up and the final write.
        return float(self.timeout_s) + 60.0


def default_tasks() -> Dict[str, TaskDef]:
    """The pipeline as it is configured today.

    Every entry is an existing script with an existing CLI.  Nothing here
    re-implements a job; this is the schedule and the coordination.
    """
    defs = [
        TaskDef("fetch",
                ["scripts/intel.py", "ah-fetch", "--all-realms",
                 "--commodities", "--sync-realms"],
                every_s=HOUR, timeout_s=1800),
        TaskDef("aggregates",
                ["scripts/aggregates.py", "build", "--days", "2"],
                every_s=6 * HOUR, timeout_s=1800, after=("fetch",)),
        TaskDef("sentinel",
                ["scripts/dfa_ml.py", "sentinel", "--if-needed"],
                every_s=24 * HOUR, timeout_s=7200),
        TaskDef("export",
                ["scripts/dfa_pricing_export.py"],
                every_s=6 * HOUR, timeout_s=1800, after=("fetch",)),
        TaskDef("deploy",
                ["scripts/deploy_addons.py"],
                every_s=6 * HOUR, timeout_s=300, after=("export",)),
    ]
    return {d.name: d for d in defs}


# ------------------------------------------------------------------ scheduling
def last_ok(con, task: str) -> float:
    value = leases.read_stat(con, f"last_ok:{task}", 0)
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def due(con, tasks: Dict[str, TaskDef], now: Optional[float] = None,
        force: bool = False) -> List[str]:
    """Which tasks should run now, in dependency order.

    Read from the durable store rather than from memory, so a restart does
    not reset the clock and a missed tick is simply overdue on the next one.
    """
    now = time.time() if now is None else float(now)
    ready: List[str] = []
    for name in _order(tasks):
        task = tasks[name]
        if force:
            ready.append(name)
            continue
        since = now - last_ok(con, name)
        if since < task.every_s:
            continue
        if any(last_ok(con, dep) <= 0 for dep in task.after):
            continue  # its input has never been produced
        ready.append(name)
    return ready


def _order(tasks: Dict[str, TaskDef]) -> List[str]:
    """Topological order by ``after``, stable on the declaration order."""
    ordered: List[str] = []
    remaining = dict(tasks)
    while remaining:
        progressed = False
        for name, task in list(remaining.items()):
            if all(dep in ordered or dep not in tasks for dep in task.after):
                ordered.append(name)
                remaining.pop(name)
                progressed = True
        if not progressed:  # a cycle: fall back to declaration order
            ordered.extend(remaining)
            break
    return ordered


# ------------------------------------------------------------------- execution
@dataclass
class TaskResult:
    task: str
    ok: bool
    seconds: float
    exit_code: Optional[int] = None
    skipped: str = ""
    detail: Dict[str, object] = field(default_factory=dict)

    def line(self) -> str:
        if self.skipped:
            return f"{self.task:<11} skipped: {self.skipped}"
        status = "ok" if self.ok else "FAILED"
        return (f"{self.task:<11} {status:<6} {self.seconds:6.1f}s"
                f"  exit={self.exit_code}")


def run_task(con, task: TaskDef, *, runner: Optional[Callable] = None,
             dry_run: bool = False,
             owner: str = "") -> TaskResult:
    """Acquire, run, record -- in that order, always.

    A task that cannot take its lease returns ``skipped`` and does nothing.
    That is the normal outcome for the second of two pipelines, not an
    error, so it is reported as a skip and does not touch ``last_ok``.
    """
    if dry_run:
        return TaskResult(task.name, True, 0.0, skipped="dry run")

    token = owner or f"run_pipeline-{os.getpid()}"
    held = _acquire(con, task.name, task.lease_ttl, token)
    if held is None:
        return TaskResult(task.name, True, 0.0,
                          skipped="another worker holds the lease")

    started = time.time()
    try:
        call = runner or _subprocess_runner
        code, output = call(task)
        ok = code == 0
        finished = time.time()
        detail = {"exit_code": code, "output_tail": (output or "")[-2000:]}
        leases.record_run(con, task.name, token, started_at=started,
                          finished_at=finished, ok=ok, detail=detail)
        if ok:
            leases.publish_stat(con, f"last_ok:{task.name}", finished, token,
                                now=finished)
            leases.publish_stat(con, f"duration_s:{task.name}",
                                round(finished - started, 1), token,
                                now=finished)
        else:
            leases.publish_stat(con, f"last_fail:{task.name}",
                                {"at": int(finished), "exit": code}, token,
                                now=finished)
        return TaskResult(task.name, ok, finished - started, exit_code=code,
                          detail=detail)
    finally:
        _release(con, held)


def _acquire(con, name: str, ttl: float, owner: str):
    try:
        return leases.open_lease_store(con).acquire(name, ttl=ttl, owner=owner)
    except Exception as exc:  # a broken mirror must not stop the pipeline
        print(f"lease acquire failed for {name}: {exc}", file=sys.stderr)
        return None


def _release(con, held) -> None:
    """Give the lease back, by identity.

    ``release`` takes the ``Lease`` that ``acquire`` returned, not a name:
    the row is deleted only where the owner token still matches, so a worker
    whose lease lapsed cannot delete the lease somebody else now holds.
    """
    if held is None:
        return
    try:
        leases.open_lease_store(con).release(held)
    except leases.LeaseLost:
        pass  # the lease lapsed and somebody else took it; not ours to free
    except Exception as exc:
        print(f"lease release failed for {held.name}: {exc}", file=sys.stderr)


def _subprocess_runner(task: TaskDef):
    """Run the task's script and return ``(exit_code, output)``.

    ``cwd`` is the project root so the relative script paths resolve, and
    the timeout is the task's own -- a hung fetch must not hold its lease
    for ever.
    """
    try:
        proc = subprocess.run(task.command(), cwd=str(ROOT),
                              capture_output=True, text=True,
                              timeout=task.timeout_s)
        return proc.returncode, (proc.stdout or "") + (proc.stderr or "")
    except subprocess.TimeoutExpired as exc:
        return 124, f"timed out after {task.timeout_s}s: {exc}"
    except FileNotFoundError as exc:
        return 127, str(exc)


# ---------------------------------------------------------------------- cycles
def cycle(con, tasks: Dict[str, TaskDef], *, force: bool = False,
          only: Optional[Sequence[str]] = None,
          runner: Optional[Callable] = None,
          dry_run: bool = False, now: Optional[float] = None
          ) -> List[TaskResult]:
    """One pass over whatever is due.

    Sequential by default.  The bottleneck is the download, and the pacer
    inside the client already enforces the request ceiling, so running two
    fetches at once would multiply memory for no throughput.
    """
    if only:
        unknown = [name for name in only if name not in tasks]
        if unknown:
            raise SystemExit(f"unknown task(s): {', '.join(unknown)}")
        names = list(only)
    else:
        names = due(con, tasks, now=now, force=force)
    results: List[TaskResult] = []
    for name in names:
        results.append(run_task(con, tasks[name], runner=runner,
                                dry_run=dry_run))
    return results


def status(con) -> str:
    """What the pipeline knows right now, for an operator or a log line."""
    lines: List[str] = []
    stats = leases.all_stats(con)
    tasks = default_tasks()
    lines.append(f"{'task':<11} {'last ok':<20} {'every':>7}  state")
    for name in _order(tasks):
        task = tasks[name]
        when = last_ok(con, name)
        stamp = (time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(when))
                 if when else "never")
        overdue = bool(when) and (time.time() - when) > task.every_s
        state = ("never run" if not when
                 else ("overdue" if overdue else "ok"))
        if f"last_fail:{name}" in stats:
            state += " (last attempt failed)"
        lines.append(f"{name:<11} {stamp:<20} {task.every_s // 60:>5}m  {state}")

    held = leases.SqliteLeaseStore(con).snapshot()
    lines.append("")
    if held:
        lines.append("held leases:")
        for lease in held:
            left = float(lease.get("expires_at") or 0) - time.time()
            lines.append(f"  {lease['name']:<11} owner={lease['owner']}"
                         f" expires in {left:6.0f}s")
    else:
        lines.append("held leases: none")

    runs = leases.recent_runs(con, limit=8)
    lines.append("")
    lines.append("recent runs:")
    if not runs:
        lines.append("  (none)")
    for run in runs:
        stamp = time.strftime("%Y-%m-%d %H:%M:%S",
                              time.localtime(run["started_at"]))
        mark = "ok" if run["ok"] else "FAILED"
        lines.append(f"  {stamp}  {run['task']:<11} {mark}")
    return "\n".join(lines)


# ------------------------------------------------------------------------- CLI
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run the DfA pipeline on a cadence, without double work.")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--once", action="store_true",
                      help="run one cycle and exit (default)")
    mode.add_argument("--loop", action="store_true",
                      help="keep cycling until interrupted")
    mode.add_argument("--status", action="store_true",
                      help="show what ran and what holds a lease")
    parser.add_argument("--task", action="append", default=[],
                        metavar="NAME",
                        help="run only this task (repeatable)")
    parser.add_argument("--force", action="store_true",
                        help="ignore the cadence and run the task now")
    parser.add_argument("--dry-run", action="store_true",
                        help="print the plan; run nothing")
    parser.add_argument("--interval", type=float, default=60.0,
                        metavar="SECONDS",
                        help="how often --loop looks for due work (default 60)")
    parser.add_argument("--db", default=str(ROOT / "data" / "intel.sqlite"),
                        help="the intelligence database holding the lease tables")
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    con = store.connect(args.db)

    if args.status:
        print(status(con))
        return 0

    tasks = default_tasks()
    if args.dry_run:
        names = args.task or due(con, tasks, force=args.force)
        print("plan: " + (", ".join(names) if names else "nothing is due"))
        for name in names:
            print(f"  {name:<11} {' '.join(tasks[name].command()[1:])}")
        return 0

    if not args.loop:
        results = cycle(con, tasks, force=args.force, only=args.task or None)
        for result in results:
            print(result.line())
        if not results:
            print("nothing is due")
        return 0 if all(r.ok for r in results) else 1

    try:
        while True:
            for result in cycle(con, tasks, force=False,
                                only=args.task or None):
                print(time.strftime("%H:%M:%S"), result.line(), flush=True)
            time.sleep(max(5.0, float(args.interval)))
    except KeyboardInterrupt:
        print("stopped")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
