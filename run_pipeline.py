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

**No lost work.**  What is due is decided from ``pipeline_stat`` in the
configured store (Postgres in production), not from a notification or an
in-memory timer.  A missed tick is not a lost tick: the next cycle sees the
task as overdue and runs it.  Redis, when present, is a mirror for
observability and never the source of truth.

Cadence
-------
Each task has an interval and the *span* of the data matters more than the
count of runs.  A sweep every hour for three days is what lets the market
panel close a 48-hour horizon; ten sweeps in one afternoon is not.  The
defaults therefore favour ``fetch`` heavily and the expensive jobs lightly:

    fetch        every 1 h   -- all 92 books plus the commodity book
    tsm_csv      every 6 h   -- the 187 TSM CSVs, ten seconds apart
    aggregates   every 6 h   -- the statistical record, rebuilt for a window
    strategy     every 6 h   -- the posting plan, from the current snapshot
    sentinel     every 24 h  -- retrain, gated by chronological validation
    decisions    every 6 h   -- replay scans into the relist ledger, link outcomes
    export       every 6 h   -- write the addon dataset
    visibility   every 24 h  -- were our sales visible in the Blizzard book
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
import hashlib
import json
import os
import re
import subprocess
import sys
import threading
import time
import uuid
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Optional, Sequence

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from woweconomy.dbcompat import load_env, redact_dsn, database_url  # noqa: E402
from woweconomy.intel import alerts                      # noqa: E402
from woweconomy.intel import store                       # noqa: E402
from woweconomy.pipeline import leases                    # noqa: E402
from woweconomy.pricing.dataset import dataset_content_signature  # noqa: E402

PY = sys.executable
HOUR = 3600

#: EX_CONFIG, from sysexits.h.  A task exits with it to say "this machine
#: cannot do this at all", which the pipeline reports as a skip rather than
#: as a success or a failure.
EX_CONFIG = 78

#: The dataset the addon loads.  A "new dataset" alert is about *this file
#: changing*, not about the export having run: the export runs on a cadence
#: and usually rewrites identical bytes.
DATASET = ROOT / "DFA_Estimate" / "Data" / "DFA_Estimate_Data.lua"


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
    #: A skip from this task is not worth a digest line.  Used by the tasks
    #: that poll an input: "nothing new" every fifteen minutes is not an
    #: action, and reporting it would out-shout the tasks that did work.
    quiet_skip: bool = False
    #: Reads or writes a very large table, and its peak memory is proportional
    #: to that table.  Heavy tasks are serialised against each other (see
    #: :func:`_concurrent_cycle`) so that ``--workers 3`` cannot put three
    #: multi-gigabyte scans on the machine at once.  Measured: an export that
    #: takes 312 s alone took 826 s while two aggregate builds were running,
    #: and Postgres reached 2.68 GB RSS with 89.8% CPU on the host.
    heavy: bool = False

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
        # What only the client can see: auction-house mailbox invoices, the
        # undercut scans, the event log and the HUD's presses.  The client
        # writes SavedVariables on `/reload` or logout and nowhere else, so
        # this has to poll the file rather than be told when it changed; the
        # import is idempotent and a file that has not moved inserts nothing.
        TaskDef("addon",
                ["scripts/intel.py", "addon-import", "--if-changed"],
                every_s=15 * 60, timeout_s=300, quiet_skip=True),
        # 187 CSVs (92 connected-realm groups plus 3 region files) at the ten
        # seconds between requests TSM needs to not start answering 429.  The
        # sweep is conditional (ETag/If-Modified-Since), so a run where TSM has
        # not republished costs 187 round trips and no bodies.  187 x 10 s is
        # 31 minutes of pacing; the measured first download of a republished
        # set is much slower than that (the CDN, not the delay, is the
        # bottleneck), so 90 minutes cut a live sweep off at 114 of 187 files.
        # Three hours fits a full cold sweep and still leaves the next one
        # due before the data is stale.
        TaskDef("tsm_csv",
                ["scripts/fetch_tsm_snapshots.py", "--delay", "10"],
                every_s=6 * HOUR, timeout_s=3 * HOUR),
        TaskDef("aggregates",
                # ``--last-days``, not ``--days``: the latter names specific
                # days and would silently rebuild everything instead of the
                # window, which is the whole cost saving.
                ["scripts/aggregates.py", "build", "--last-days", "2"],
                every_s=6 * HOUR, timeout_s=1800, after=("fetch",),
                heavy=True),
        TaskDef("strategy",
                ["scripts/strategy.py", "build"],
                every_s=6 * HOUR, timeout_s=1800, after=("fetch",)),
        TaskDef("sentinel",
                ["scripts/dfa_ml.py", "sentinel", "--if-needed"],
                every_s=24 * HOUR, timeout_s=7200, heavy=True),
        # The advice was being written down only when the interactive watcher
        # happened to scan.  Replaying the stored scans and attaching the
        # confirmed events makes the ledger a pipeline fact rather than a
        # side effect of someone looking at the market that day.
        TaskDef("decisions",
                ["scripts/intel.py", "decisions", "--backfill", "--link"],
                every_s=6 * HOUR, timeout_s=900, after=("fetch",)),
        # Deliberately not `after=("strategy",)`: the plan is replaced in a
        # single transaction, so a concurrent export reads either the old
        # plan or the new one, never half of it -- and a machine without the
        # DfA demand export (which strategy needs) would otherwise block the
        # dataset for ever.
        TaskDef("export",
                ["scripts/dfa_pricing_export.py"],
                every_s=6 * HOUR, timeout_s=1800, after=("fetch",),
                heavy=True),
        # Cheap (0.03 s), and it only becomes answerable once a sale happens
        # while snapshots are being taken -- which is exactly when nobody is
        # watching.  It writes the report rather than failing, so a run with
        # no overlap yet does not read as a broken task.
        TaskDef("visibility",
                ["scripts/sale_visibility.py", "--by-realm",
                 "--json", "reports/sale_visibility.json"],
                every_s=24 * HOUR, timeout_s=300, after=("fetch",)),
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


# ------------------------------------------------------------------- notices
def dataset_fingerprint(path: Path = DATASET) -> str:
    """The *content* signature of the exported dataset, or "" when absent.

    Not the sha256 of the file: that covers ``generatedAt``, so every
    export would look new and the alert would fire every six hours with
    nothing to say.  ``dataset_content_signature`` strips the build time
    and compares floats at 1% -- the resolution at which a probability is
    worth telling someone about -- and is the one definition of "changed"
    in the project.
    """
    if not Path(path).exists():
        return ""
    text = Path(path).read_text(encoding="utf-8", errors="replace")
    return dataset_content_signature(text)


#: Scalar fields worth quoting in a "new dataset" alert.  They are read from
#: the generated Lua rather than from the exporter's memory, so the alert
#: describes the bytes that actually shipped.
_DATASET_FIELDS = ("version", "engineVersion", "itemCount", "realmCount",
                   "priceCount", "indexedRealms", "measuredRealms",
                   "sentinelModel", "meanConfidence", "farmStops")


def dataset_facts(path: Path = DATASET) -> Dict[str, str]:
    facts: Dict[str, str] = {}
    if not Path(path).exists():
        return facts
    text = Path(path).read_text(encoding="utf-8", errors="replace")
    for field_name in _DATASET_FIELDS:
        match = re.search(rf"^\s*{field_name}\s*=\s*(.+?),?\s*$", text,
                          re.MULTILINE)
        if match:
            facts[field_name] = match.group(1).strip().rstrip(",")
    # The status counts matter more than the totals: they say how many
    # items carry a validated probability and how many fall back.
    statuses = re.search(r"predictionStatus\s*=\s*\{(.*?)\}", text, re.DOTALL)
    if statuses:
        pairs = re.findall(r'\["([^"]+)"\]\s*=\s*(\d+)', statuses.group(1))
        if pairs:
            facts["predictionStatus"] = ", ".join(f"{k}: {v}"
                                                  for k, v in pairs)
    return facts


def dataset_alert(facts: Dict[str, str], digest: str) -> "alerts.Alert":
    lines = [f"{key}: {value}" for key, value in facts.items()
             if key != "predictionStatus"]
    if "predictionStatus" in facts:
        lines.append(f"status: {facts['predictionStatus']}")
    lines.append(f"sha256: {digest[:12]}")
    # An alert that only reports a number leaves the reader to work out what
    # to do with it.  The dataset is read at load time, so the client needs
    # a reload before any of these numbers reach a tooltip.
    lines.append("action: type /reload in game (or log out and back in) to "
                 "pick up the new prices")
    return alerts.Alert(key="dataset:new",
                        title="New DfA dataset published",
                        lines=lines)


def notify_new_dataset(con, *, notify: bool = True,
                       path: Path = DATASET,
                       now: Optional[float] = None) -> List[Dict[str, object]]:
    """Post one alert when the exported dataset actually changed.

    Decided by content hash, not by the export having run.  The export runs
    every few hours and usually produces the same bytes; an alert per run
    would be an alert nobody reads, and the one that mattered would be lost
    in it.
    """
    digest = dataset_fingerprint(path)
    if not digest:
        return []
    previous = str(leases.read_stat(con, "dataset:sha256", "") or "")
    if digest == previous:
        return []
    state = leases.read_stat(con, "alerts:state", None) or {"alerted": {}}
    # The webhook URL has to be handed over explicitly: `deliver` posts only
    # when it is given one, and its default is "no webhook".  Without this the
    # dataset alert was a desktop toast and nothing ever reached Discord.
    #
    # No cooldown, either: the digest above *is* the rate limit -- it only
    # differs when the dataset really changed -- and a six-hour cooldown could
    # silently drop that change and re-announce it later, or (because the
    # digest is only recorded once a channel took it) delay it by hours.
    delivered = alerts.deliver([dataset_alert(dataset_facts(path), digest)],
                               state, int(now if now is not None else time.time()),
                               url=alerts.webhook_url(), cooldown_s=0,
                               notify=notify) or []
    leases.publish_stat(con, "alerts:state", state)
    # Only claim the dataset was announced if a channel actually took it.
    # Publishing the digest unconditionally would mark an undelivered alert
    # as delivered, and the next cycle would compare equal and stay silent --
    # the alert would be lost for good because a webhook was down once.
    if any(item.get("webhook") or item.get("toast") for item in delivered):
        leases.publish_stat(con, "dataset:sha256", digest)
    return delivered


def cycle_digest(results: Sequence[TaskResult], *,
                 now: Optional[float] = None) -> Optional["alerts.Alert"]:
    """One message per cycle, naming every action it took.

    The pipeline already records each run in ``pipeline_run`` and announces it
    on the Redis channel, but neither is readable from a phone.  This is the
    same list folded into one embed: a task that ran, a task that skipped
    (with the reason it gave) and a task that failed each get their own line,
    so "what did the pipeline do at 14:00" is answerable without a terminal.

    ``None`` when the cycle did nothing: an empty digest is a heartbeat, and
    the rule for every other alert here is that an alert is a *change*.
    """
    results = [r for r in results if not r.lease_skip and not r.quiet]
    if not results:
        return None
    lines: List[str] = []
    failed = 0
    for result in results:
        if result.skipped:
            lines.append(f"{result.task:<11} skipped -- {result.skipped}"[:180])
        elif result.ok:
            lines.append(f"{result.task:<11} ok       {result.seconds:7.1f}s")
        else:
            failed += 1
            lines.append(f"{result.task:<11} FAILED   {result.seconds:7.1f}s"
                         f"  exit={result.exit_code}")
    at = time.time() if now is None else float(now)
    stamp = time.strftime("%H:%M:%S", time.localtime(at))
    title = (f"Pipeline cycle {stamp}: {len(results)} action(s)"
             + (f", {failed} failed" if failed else ""))
    return alerts.Alert(key="pipeline:cycle", title=title, lines=lines,
                        level="warn" if failed else "info")


def post_cycle_digest(results: Sequence[TaskResult], *, notify: bool = True,
                      now: Optional[float] = None) -> List[Dict[str, object]]:
    """Post the cycle's digest to the webhook; never fail the cycle for it.

    A dead channel is reported on stderr and otherwise ignored: the work has
    already happened and is recorded in the database, which stays the source
    of truth for what ran.
    """
    if not notify:
        return []
    alert = cycle_digest(results, now=now)
    if alert is None:
        return []
    posted = alerts.post_discord(alerts.webhook_url(),
                                 alerts.discord_payload(alert))
    if not posted.get("ok"):
        print(f"cycle digest not posted: {posted.get('error') or posted.get('status')}",
              file=sys.stderr)
    return [{"key": alert.key, **posted}]


# ------------------------------------------------------------------- execution
@dataclass
class TaskResult:
    task: str
    ok: bool
    seconds: float
    exit_code: Optional[int] = None
    skipped: str = ""
    detail: Dict[str, object] = field(default_factory=dict)
    #: The lease was held by someone else, so this cycle did not attempt the
    #: work.  Not an action: reporting it would post a digest every couple of
    #: minutes for as long as another worker is running the task.
    lease_skip: bool = False
    #: The task had nothing to do (an unchanged input).  Same treatment.
    quiet: bool = False

    def line(self) -> str:
        if self.skipped:
            return f"{self.task:<11} skipped: {self.skipped}"
        status = "ok" if self.ok else "FAILED"
        return (f"{self.task:<11} {status:<6} {self.seconds:6.1f}s"
                f"  exit={self.exit_code}")


def run_task(con, task: TaskDef, *, runner: Optional[Callable] = None,
             dry_run: bool = False, owner: str = "",
             notify: bool = True) -> TaskResult:
    """Acquire, run, record -- in that order, always.

    A task that cannot take its lease returns ``skipped`` and does nothing.
    That is the normal outcome for the second of two pipelines, not an
    error, so it is reported as a skip and does not touch ``last_ok``.
    """
    if dry_run:
        return TaskResult(task.name, True, 0.0, skipped="dry run")

    # The owner token must be unique per process *start*, not per pid: every
    # container's scheduler is PID 1, so a lease left by a killed container
    # carried the same token as the container that replaced it.  The new
    # worker then looked like the old holder and waited out a two-hour TTL.
    token = owner or f"run_pipeline-{os.getpid()}-{uuid.uuid4().hex[:8]}"
    held = _acquire(con, task.name, task.lease_ttl, token)
    if held is None:
        leases.publish_event("skipped", task=task.name, owner=token,
                             reason="another worker holds the lease")
        return TaskResult(task.name, True, 0.0,
                          skipped="another worker holds the lease",
                          lease_skip=True)

    started = time.time()
    # Say when a task *starts*, not only when it finishes.  A fetch against
    # the whole region takes minutes, and without this line the pipeline
    # looks dead in `docker compose logs` for exactly as long as it is
    # doing the most work.
    if runner is None:
        print(f"{time.strftime('%H:%M:%S')} {task.name:<11} start"
              f"  {' '.join(task.command()[1:])}", flush=True)
    leases.publish_event("start", task=task.name, owner=token)
    try:
        call = runner or _subprocess_runner
        code, output = call(task)
        # 78 is EX_CONFIG: the task cannot run on this machine at all, which
        # is not a failure and not a success.  `deploy` uses it when the
        # game's AddOns folder is not mounted into the container -- the
        # honest report is "skipped", and `last_ok` must not advance, or the
        # pipeline would claim it deployed something it never touched.
        if code == EX_CONFIG:
            finished = time.time()
            reason = _last_line(output)
            print(f"{time.strftime('%H:%M:%S')} {task.name:<11} skipped"
                  f"  {reason}", flush=True)
            leases.publish_event("skipped", task=task.name, owner=token,
                                 reason=reason)
            # A skip is an answer, not a failure, so it takes the place of
            # the previous attempt in the statistics: leaving `last_fail`
            # behind made every later status line repeat a failure that had
            # already been explained away (a task with no local input looked
            # broken for ever).
            leases.publish_stat(con, f"last_skip:{task.name}",
                                {"at": int(finished), "exit": code,
                                 "reason": reason}, token, now=finished)
            leases.clear_stat(con, f"last_fail:{task.name}")
            return TaskResult(task.name, True, finished - started,
                              exit_code=code, skipped=reason,
                              quiet=task.quiet_skip,
                              detail={"exit_code": code})
        ok = code == 0
        finished = time.time()
        detail = {"exit_code": code, "output_tail": (output or "")[-2000:]}
        # A new dataset is announced after the export, from the bytes that
        # were written.  Failure here must not fail the task: the dataset is
        # published, and a dead webhook does not un-publish it.  Recorded
        # before `record_run` so the attempt carries what the channel said.
        if ok and task.name == "export" and notify:
            try:
                detail["dataset_alert"] = notify_new_dataset(con, now=finished)
            except Exception as exc:  # pragma: no cover - channel failure
                print(f"dataset alert failed: {exc}", file=sys.stderr)
        leases.record_run(con, task.name, token, started_at=started,
                          finished_at=finished, ok=ok, detail=detail)
        if ok:
            leases.publish_stat(con, f"last_ok:{task.name}", finished, token,
                                now=finished)
            leases.publish_stat(con, f"duration_s:{task.name}",
                                round(finished - started, 1), token,
                                now=finished)
            # `last_fail` means "the last attempt failed", so a success has
            # to retire it -- otherwise `status` keeps reporting a failure
            # that a later run has already answered.
            leases.clear_stat(con, f"last_fail:{task.name}")
            leases.clear_stat(con, f"last_skip:{task.name}")
        else:
            leases.publish_stat(con, f"last_fail:{task.name}",
                                {"at": int(finished), "exit": code}, token,
                                now=finished)
            leases.clear_stat(con, f"last_skip:{task.name}")
        # Announced after the fact is durable, so a monitor can never show a
        # result the database has not recorded.
        leases.publish_event("ok" if ok else "failed", task=task.name,
                             owner=token, exit=code,
                             seconds=round(finished - started, 1))
        return TaskResult(task.name, ok, finished - started, exit_code=code,
                          detail=detail)
    finally:
        _release(con, held)


def _last_line(output: str) -> str:
    """The last non-empty line of a task's output, for a one-line report."""
    for line in reversed((output or "").strip().splitlines()):
        if line.strip():
            return line.strip()[:200]
    return ""


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
          dry_run: bool = False, now: Optional[float] = None,
          notify: bool = True, workers: int = 1,
          heavy_lanes: Optional[int] = None,
          connect: Optional[Callable[[], object]] = None
          ) -> List[TaskResult]:
    """One pass over whatever is due.

    Sequential with ``workers=1`` (the default), concurrent above it.  A
    task starts only when every task in its ``after`` list has *finished in
    this cycle* (or is not part of the plan), so the dependency order is
    honoured without making unrelated work wait.

    Concurrency is safe because a task already holds its own lease while it
    runs and writes its own rows; the scheduler only decides what may
    start.  Each worker gets its own connection: a psycopg2 connection is
    not shared between threads.

    ``heavy_lanes`` bounds how many :attr:`TaskDef.heavy` tasks may be in
    flight at once, independently of ``workers``.  Two large scans on the
    same store do not run faster than one -- they contend for the same
    buffers -- and the machine they contend on is also someone's desktop.
    """
    if only:
        unknown = [name for name in only if name not in tasks]
        if unknown:
            raise SystemExit(f"unknown task(s): {', '.join(unknown)}")
        names = list(only)
    else:
        names = due(con, tasks, now=now, force=force)
    if workers and int(workers) > 1 and len(names) > 1:
        return _concurrent_cycle(names, tasks, runner=runner, dry_run=dry_run,
                                 notify=notify, workers=int(workers),
                                 heavy_lanes=heavy_lanes, connect=connect)
    results: List[TaskResult] = []
    for name in names:
        results.append(run_task(con, tasks[name], runner=runner,
                                dry_run=dry_run, notify=notify))
    return results


def _heavy_lanes(override: Optional[int] = None) -> int:
    """How many heavy tasks may overlap.  One unless configured otherwise."""
    if override is not None:
        return max(1, int(override))
    try:
        return max(1, int(os.environ.get("PIPELINE_HEAVY_LANES", "1")))
    except (TypeError, ValueError):
        return 1


def _concurrent_cycle(names: Sequence[str], tasks: Dict[str, TaskDef], *,
                      runner: Optional[Callable], dry_run: bool, notify: bool,
                      workers: int, heavy_lanes: Optional[int] = None,
                      connect: Optional[Callable]) -> List[TaskResult]:
    """Start every task whose inputs are done, up to ``workers`` at once."""
    lanes = threading.BoundedSemaphore(_heavy_lanes(heavy_lanes))

    def execute(task: TaskDef) -> TaskResult:
        task_con = connect() if connect is not None else store.connect()
        try:
            if not task.heavy:
                return run_task(task_con, task, runner=runner, dry_run=dry_run,
                                notify=notify)
            with lanes:
                return run_task(task_con, task, runner=runner, dry_run=dry_run,
                                notify=notify)
        finally:
            try:
                task_con.close()  # type: ignore[attr-defined]
            except Exception:  # pragma: no cover - a closed connection is fine
                pass

    results: List[TaskResult] = []
    pending = list(names)
    running: Dict[object, str] = {}
    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        while pending or running:
            running_names = set(running.values())
            for name in list(pending):
                if len(running) >= workers:
                    break
                task = tasks[name]
                if any(dep in pending or dep in running_names
                       for dep in task.after):
                    continue
                running[pool.submit(execute, task)] = name
                running_names.add(name)
                pending.remove(name)
            if not running:
                break  # a dependency cycle; `_order` still reports the rest
            done, _ = wait(list(running), return_when=FIRST_COMPLETED)
            for future in done:
                running.pop(future)
                results.append(future.result())
    position = {name: index for index, name in enumerate(names)}
    results.sort(key=lambda result: position.get(result.task, len(position)))
    return results


def status(con, *, now: Optional[float] = None) -> str:
    """What the pipeline knows right now, for an operator or a log line."""
    now = time.time() if now is None else float(now)
    lines: List[str] = []
    stats = leases.all_stats(con)
    tasks = default_tasks()
    lines.append(f"{'task':<11} {'last ok':<20} {'every':>7}  state")
    for name in _order(tasks):
        task = tasks[name]
        when = last_ok(con, name)
        stamp = (time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(when))
                 if when else "never")
        overdue = bool(when) and (now - when) > task.every_s
        state = ("never run" if not when
                 else ("overdue" if overdue else "ok"))
        # Whichever came last -- a skip or a failure -- is what the line
        # should report.  A skip explains itself ("no DFA_DIR") and must
        # not be masked by a failure from an earlier attempt.
        skip = stats.get(f"last_skip:{name}")
        failure = stats.get(f"last_fail:{name}")
        if isinstance(skip, dict) and float(skip.get("at") or 0) >= when:
            skipped_at = time.strftime("%H:%M:%S", time.localtime(
                float(skip.get("at") or 0)))
            reason = str(skip.get("reason") or "").strip()[:90] or "no output"
            state += (f" (last attempt skipped {skipped_at}"
                      f" exit={skip.get('exit')}: {reason})")
        elif isinstance(failure, dict):
            failed_at = time.strftime("%H:%M:%S", time.localtime(
                float(failure.get("at") or 0)))
            state += (f" (last attempt failed {failed_at}"
                      f" exit={failure.get('exit')}:"
                      f" {_failure_tail(con, name)})")
        lines.append(f"{name:<11} {stamp:<20} {task.every_s // 60:>5}m  {state}")

    held = leases.SqliteLeaseStore(con).snapshot()
    lines.append("")
    if held:
        lines.append("held leases:")
        for lease in held:
            expiry = float(lease.get("expires_at") or 0) - now
            elapsed = now - float(lease.get("acquired_at") or 0)
            mark = "live" if expiry > 0 else "lapsed"
            lines.append(f"  {lease['name']:<11} owner={lease['owner']}"
                         f" running {elapsed:5.0f}s, {mark}"
                         f" (expires in {expiry:6.0f}s)")
    else:
        lines.append("held leases: none")

    redis = leases.redis_status(con)
    if not redis.get("configured"):
        lines.append("redis: not configured (Postgres is the authority)")
    elif redis.get("reachable"):
        lines.append(f"redis: connected, {redis.get('stats', 0)} stats,"
                     f" {redis.get('leases', 0)} lease keys"
                     + (f", {len(redis.get('behind') or [])} stale mirror keys"
                        if redis.get("behind") else ""))
    else:
        detail = redis.get("error") or "connection failed"
        lines.append(f"redis: configured but unreachable ({detail};"
                     " the pipeline still runs)")

    runs = leases.recent_runs(con, limit=8)
    lines.append("")
    lines.append("recent runs:")
    if not runs:
        lines.append("  (none)")
    for run in runs:
        stamp = time.strftime("%Y-%m-%d %H:%M:%S",
                              time.localtime(run["started_at"]))
        mark = "ok" if run["ok"] else "FAILED"
        seconds = float(run["finished_at"] or 0) - float(run["started_at"])
        lines.append(f"  {stamp}  {run['task']:<11} {mark:<6} {seconds:6.0f}s")
    return "\n".join(lines)


def _failure_tail(con, task: str) -> str:
    """The last line of the most recent failed attempt, for the status line."""
    for run in leases.recent_runs(con, limit=25):
        if run["task"] == task and not run["ok"]:
            detail = run.get("detail") or {}
            tail = (detail.get("output_tail") or "").strip().splitlines()
            return tail[-1][:90] if tail else "no output"
    return "no output"


def status_json(con, *, now: Optional[float] = None) -> Dict[str, object]:
    """The same view as :func:`status`, for a monitor that is not a person."""
    from woweconomy.dbcompat import backend

    now = time.time() if now is None else float(now)
    tasks = default_tasks()
    stats = leases.all_stats(con)
    rows: List[Dict[str, object]] = []
    for name in _order(tasks):
        task = tasks[name]
        when = last_ok(con, name)
        rows.append({
            "task": name,
            "every_s": task.every_s,
            "last_ok": when or None,
            "overdue": bool(when) and (now - when) > task.every_s,
            "last_fail": stats.get(f"last_fail:{name}"),
            "last_skip": stats.get(f"last_skip:{name}"),
            "duration_s": stats.get(f"duration_s:{name}"),
        })
    return {
        "now": int(now),
        "backend": backend(),
        "tasks": rows,
        "leases": leases.SqliteLeaseStore(con).snapshot(),
        "redis": leases.redis_status(con),
        "recent_runs": leases.recent_runs(con, limit=25),
        "dataset": {"sha256": leases.read_stat(con, "dataset:sha256", ""),
                    "path": str(DATASET)},
    }


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
    parser.add_argument("--workers", type=int, default=1, metavar="N",
                        help="run up to N independent tasks at once"
                             " (default 1: one at a time)")
    parser.add_argument("--heavy-lanes", type=int, default=None, metavar="N",
                        help="how many large-data tasks may overlap, whatever"
                             " --workers says (default: PIPELINE_HEAVY_LANES"
                             " or 1)")
    parser.add_argument("--json", action="store_true",
                        help="with --status, print the machine-readable view")
    parser.add_argument("--watch", type=float, default=0.0, metavar="SECONDS",
                        help="with --status, refresh every SECONDS until"
                             " interrupted")
    parser.add_argument("--db", default=str(ROOT / "data" / "intel.sqlite"),
                        help="the intelligence database holding the lease tables")
    parser.add_argument("--no-notify", action="store_true",
                        help="do not post the new-dataset alert")
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    # The tasks run as child processes and inherit this environment, so the
    # backend has to be decided here -- before anything connects.  Without
    # it every worker falls back to SQLite while the data lives in
    # Postgres: the split-brain the compat layer exists to prevent,
    # arriving through the one entry point that never read the `.env`.
    # Done in `main` and not at import, because importing this module must
    # not change the backend of the process that did it (the test suite
    # imports it, and would then write to the shared database).
    # Variables already set still win.
    load_env()
    # Say which database this run is on.  A pipeline that reports its
    # backend is a pipeline whose reports can be trusted to come from the
    # data you think they came from.
    from woweconomy.dbcompat import backend as _backend
    # `--status --json` is read by a parser, so the banner must not share its
    # stream: one line of prose in front of the object makes the whole
    # document unparseable.
    banner = sys.stderr if (args.json and args.status) else sys.stdout
    if _backend() == "postgres":
        print(f"backend: postgres ({redact_dsn(database_url())})", file=banner)
    else:
        print("backend: sqlite -- NOT the shared database"
              + (f"; {redact_dsn(database_url())} is configured but unused"
                 if database_url() else ""), file=banner)
    con = store.connect(args.db)

    if args.status:
        try:
            while True:
                view = (json.dumps(status_json(con), default=str)
                        if args.json else status(con))
                print(view, flush=True)
                if not args.watch:
                    break
                print(flush=True)
                time.sleep(max(1.0, float(args.watch)))
        except KeyboardInterrupt:
            return 0
        return 0

    tasks = default_tasks()
    if args.dry_run:
        names = args.task or due(con, tasks, force=args.force)
        print("plan: " + (", ".join(names) if names else "nothing is due"))
        for name in names:
            print(f"  {name:<11} {' '.join(tasks[name].command()[1:])}")
        return 0

    notify = not args.no_notify
    if not args.loop:
        results = cycle(con, tasks, force=args.force, only=args.task or None,
                        notify=notify, workers=args.workers,
                        heavy_lanes=args.heavy_lanes,
                        connect=lambda: store.connect(args.db))
        for result in results:
            print(result.line())
        if not results:
            print("nothing is due")
        post_cycle_digest(results, notify=notify)
        return 0 if all(r.ok for r in results) else 1

    try:
        while True:
            results = cycle(con, tasks, force=False,
                            only=args.task or None, notify=notify,
                            workers=args.workers,
                            heavy_lanes=args.heavy_lanes,
                            connect=lambda: store.connect(args.db))
            for result in results:
                print(time.strftime("%H:%M:%S"), result.line(), flush=True)
            post_cycle_digest(results, notify=notify)
            time.sleep(max(5.0, float(args.interval)))
    except KeyboardInterrupt:
        print("stopped")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
