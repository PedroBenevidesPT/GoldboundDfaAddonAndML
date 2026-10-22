"""DFA Sentinel: the loop that does the chores, so the player does not.

Running the same seven commands by hand after every session is how data
goes missing.  This script watches what changes and runs only what the
change implies:

===================  ==========================================  ==========
source               change                                     task
===================  ==========================================  ==========
addon SavedVariables  /reload or logout wrote it                import + HUD actions
Journalator archive   a sale, posting or failure was archived   journal import
TSM AppData.lua       the desktop app refreshed the prices      market sync
DFA_Estimate export   (after any of the above, 1x/gap)          pricing export
DFA_Estimate data     the canonical dataset changed             deploy to both copies
Blizzard auctions     hourly, per realm                          AH fetch + undercut
-------------------  ------------------------------------------  ----------
train                 new journal rows and >= 6h since last     one model per realm
===================  ==========================================  ==========

The HUD actions are the in-game half: a press writes an action into
SavedVariables, the daemon imports it, and the follow-up runs -- an
undercut scan from the HUD becomes relist advice plus an alert without
the player typing another command.

    python scripts/watch.py                  # loop forever
    python scripts/watch.py --once           # one tick, print the plan
    python scripts/watch.py --once --dry-run # decide, do not run or write
    python scripts/watch.py --once --offline # skip Blizzard/network jobs

Every run appends to ``logs/watch.log``; the state (file signatures, last
train, alert cooldowns) lives in ``data/watch_state.json``.  A webhook is
read from ``DFA_DISCORD_WEBHOOK``; the URL is never written to the log or
the state file.  Only one daemon runs: it holds ``data/watch.lock`` and a
second one exits instead of racing the first.
"""
from __future__ import annotations

import argparse
import glob
import json
import logging
import os
import sqlite3
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
PY = sys.executable

from woweconomy.intel import actions as action_log  # noqa: E402
from woweconomy.intel import ah_undercut, alerts, inventory, relist, store  # noqa: E402
from woweconomy.ml.market_context import build_realm_market  # noqa: E402

DEFAULT_STATE = ROOT / "data" / "watch_state.json"
DEFAULT_LOG = ROOT / "logs" / "watch.log"
DEFAULT_LOCK = ROOT / "data" / "watch.lock"
#: where the locked byte lives, past the pid text at offset 0
LOCK_REGION = 1024
JOURNAL_GLOBS = (
    r"C:/Program Files (x86)/World of Warcraft/*/WTF/Account/*/SavedVariables/Journalator_Statistics.lua",
    r"C:/Program Files/World of Warcraft/*/WTF/Account/*/SavedVariables/Journalator_Statistics.lua",
    r"C:/Users/*/World of Warcraft/*/WTF/Account/*/SavedVariables/Journalator_Statistics.lua",
)

logger = logging.getLogger("dfa.watch")


@dataclass
class Config:
    interval: int = 120
    realms: List[str] = field(default_factory=list)
    train_every_h: float = 6.0
    #: the validated Sentinel refit is heavier; once a day is enough, and
    #: the command itself skips the fit when no new confirmed labels arrived
    sentinel_every_h: float = 24.0
    ah_every_h: float = 1.0
    #: the export refits the shipped model (minutes); six hours is enough,
    #: and any change to the inputs triggers it sooner
    export_every_min: float = 360.0
    #: the export refits the shipped model and takes minutes, so consecutive
    #: changes are coalesced: it runs at most once per gap, and a change that
    #: arrives inside the gap is queued, never dropped
    export_min_gap_min: float = 30.0
    offline: bool = False
    #: wait for a SavedVariables file to stop changing before reading it
    stable_s: int = 30
    #: fetching the public TSM CSVs is slow and not needed every tick; the
    #: desktop app's AppData.lua already carries the prices
    with_csv: bool = False
    dry_run: bool = False
    notify: bool = True
    webhook: str = ""
    addons_dir: str = ""
    #: the canonical dataset the export writes; empty uses PricingConfig,
    #: which is the only place these paths are configured
    dataset_path: str = ""
    state_path: Path = DEFAULT_STATE
    log_path: Path = DEFAULT_LOG
    addon_path: str = ""
    intel_db: str = ""
    tsm_db: str = ""


@dataclass
class Task:
    name: str
    reason: str
    args: Dict[str, Any] = field(default_factory=dict)


# --------------------------------------------------------------------- sources
def find_sources() -> Dict[str, Optional[str]]:
    """Where each input lives right now.  A missing source is not an error."""
    sources: Dict[str, Optional[str]] = {}
    try:
        from woweconomy.analytics import auction_intel as ai
        path = ai.find_saved_variables()
        sources["addon"] = str(path) if path else None
    except Exception:  # noqa: BLE001 - a broken addon file must not stop the loop
        sources["addon"] = None
    journal = [Path(p) for pattern in JOURNAL_GLOBS for p in glob.glob(pattern)]
    sources["journal"] = str(max(journal, key=lambda p: p.stat().st_mtime)) if journal else None
    try:
        import scripts.calc_price as cp  # type: ignore
        appdata = cp.find_appdata(None)
        sources["tsm"] = str(appdata) if appdata else None
    except Exception:  # noqa: BLE001
        sources["tsm"] = None
    csv_files = list((ROOT / "csv").glob("*.csv"))
    sources["csv"] = (str(max(csv_files, key=lambda p: p.stat().st_mtime))
                      if csv_files else None)
    # the canonical dataset is written by the export, but also by a manual
    # run; watching it means a dataset deployed by hand still reaches the
    # game without waiting for the daemon's own export
    try:
        from woweconomy.pricing.config import PricingConfig
        dataset = Path(PricingConfig().dataset_path)
        sources["dataset"] = str(dataset) if dataset.is_file() else None
    except Exception:  # noqa: BLE001 - a broken config must not stop the loop
        sources["dataset"] = None
    return sources


def signature(path: Optional[str]) -> Dict[str, int]:
    if not path:
        return {}
    try:
        stat = os.stat(path)
    except OSError:
        return {}
    return {"mtime": int(stat.st_mtime), "size": int(stat.st_size)}


def changed_sources(state: Dict[str, Any],
                    sources: Dict[str, Optional[str]],
                    now: Optional[int] = None,
                    stable_s: int = 0) -> Dict[str, bool]:
    """Which inputs changed, ignoring a file the client is still writing.

    SavedVariables is written in one go on /reload or logout, but a large
    file can take a second and a running client may rewrite it while the
    daemon is looking.  A file whose mtime is newer than ``stable_s`` is
    not read yet: half a Lua table parses as an error, not as data.
    """
    seen = state.setdefault("sources", {})
    now = int(now if now is not None else time.time())
    changed: Dict[str, bool] = {}
    for name, path in sources.items():
        current = signature(path)
        previous = seen.get(name) or {}
        fresh = bool(current) and current != previous
        if fresh and stable_s and now - int(current.get("mtime") or 0) < stable_s:
            fresh = False
        changed[name] = fresh
    return changed


def settling_sources(state: Dict[str, Any],
                     sources: Dict[str, Optional[str]],
                     now: Optional[int] = None,
                     stable_s: int = 0) -> set:
    """Sources that changed but are too fresh to read this tick.

    Their signature must not be recorded: the write is still in progress,
    and marking it seen would drop the change without ever reading it.
    """
    if not stable_s:
        return set()
    seen = state.get("sources") or {}
    now = int(now if now is not None else time.time())
    settling = set()
    for name, path in sources.items():
        current = signature(path)
        previous = seen.get(name) or {}
        if (current and current != previous
                and now - int(current.get("mtime") or 0) < stable_s):
            settling.add(name)
    return settling


def mark_sources(state: Dict[str, Any], sources: Dict[str, Optional[str]],
                 skip: Optional[set] = None) -> None:
    seen = state.setdefault("sources", {})
    for name, path in sources.items():
        if skip and name in skip:
            continue
        current = signature(path)
        if current:
            seen[name] = current
        else:
            seen.pop(name, None)


# ------------------------------------------------------------------- planning
def plan(now: int, state: Dict[str, Any], changed: Dict[str, bool],
         cfg: Config) -> List[Task]:
    """Which jobs this tick should run, in order.  Pure: easy to test."""
    last = state.setdefault("last", {})
    tasks: List[Task] = []
    names = set()

    def add(task: Task) -> None:
        if task.name not in names:
            names.add(task.name)
            tasks.append(task)

    if changed.get("addon"):
        add(Task("addon", "SavedVariables changed"))
    if changed.get("journal"):
        add(Task("journal", "Journalator archive changed"))
    if changed.get("tsm"):
        add(Task("sync", "TSM AppData changed"))
    if (cfg.with_csv and changed.get("csv")
            and now - int(last.get("csv") or 0) >= 24 * 3600):
        add(Task("csv", "market CSV changed"))

    due_train = (float(last.get("train") or 0) == 0
                 or now - float(last.get("train") or 0) >= cfg.train_every_h * 3600)
    if (changed.get("journal") or changed.get("addon")) and due_train:
        add(Task("train", f">= {cfg.train_every_h:g}h since the last fit"))

    due_sentinel = (float(last.get("sentinel") or 0) == 0
                    or now - float(last.get("sentinel") or 0)
                    >= cfg.sentinel_every_h * 3600)
    if due_sentinel:
        add(Task("sentinel", f">= {cfg.sentinel_every_h:g}h since the last Sentinel fit"))

    # The realm list is discovered by the task itself; requiring --realms
    # here meant the hourly scan never ran on a daemon started without it.
    if not cfg.offline:
        if now - float(last.get("ah") or 0) >= cfg.ah_every_h * 3600:
            add(Task("ah", "hourly Blizzard auction refresh"))

    # The dataset is built from journal and TSM references; an inventory
    # snapshot does not change a single price in it, and the export refits
    # the model, so it does not run for an addon-only change.
    #
    # It also takes minutes while TSM rewrites AppData.lua every few
    # minutes; running it per change meant one export after another with
    # the loop blocked inside them.  A change inside the gap is *queued*
    # (``export_pending``) and runs on the first tick after the gap, so a
    # quiet input still reaches the game.
    gap_s = max(0.0, float(cfg.export_min_gap_min)) * 60
    since_export = now - float(last.get("export") or 0)
    cooling = bool(last.get("export")) and since_export < gap_s
    if changed.get("journal") or changed.get("tsm") or "train" in names:
        if cooling:
            state["export_pending"] = True
        else:
            add(Task("export", "pricing inputs changed"))
    if state.get("export_pending") and not cooling:
        add(Task("export", "inputs changed while the export was cooling down"))
    elif since_export >= cfg.export_every_min * 60:
        add(Task("export", "stale dataset"))

    # Our own export deploys its bytes directly, so this is for a dataset
    # written outside the daemon (a manual export, a report run).  It runs
    # after the export: by then the copies usually already match.
    if changed.get("dataset"):
        add(Task("deploy", "canonical dataset changed"))
    return tasks


# ------------------------------------------------------------------ execution
class Runner:
    """Runs the project's own CLIs and reads their JSON answer."""

    def __init__(self, cwd: Path = ROOT, dry_run: bool = False):
        self.cwd = cwd
        self.dry_run = dry_run
        self.calls: List[List[str]] = []

    def __call__(self, argv: Sequence[str], timeout: int = 1800) -> Dict[str, Any]:
        self.calls.append(list(argv))
        if self.dry_run:
            return {"dry_run": True, "argv": list(argv), "returncode": 0}
        try:
            result = subprocess.run(list(argv), cwd=self.cwd, capture_output=True,
                                    text=True, timeout=timeout)
        except subprocess.TimeoutExpired:
            return {"error": f"timed out after {timeout}s", "returncode": -1}
        except OSError as exc:
            return {"error": str(exc), "returncode": -1}
        out = (result.stdout or "").strip()
        payload: Any = None
        if out:
            try:
                payload = json.loads(out[out.index("{"):])
            except (ValueError, json.JSONDecodeError):
                payload = None
        return {"returncode": result.returncode,
                "stdout": out[-4000:], "payload": payload,
                "stderr": (result.stderr or "")[-2000:]}


class Watch:
    def __init__(self, cfg: Config, runner: Optional[Callable[..., Dict[str, Any]]] = None,
                 sources_fn: Callable[[], Dict[str, Optional[str]]] = find_sources):
        self.cfg = cfg
        self.runner = runner or Runner(dry_run=cfg.dry_run)
        self.sources_fn = sources_fn
        self.state = self.load_state()
        self.summary: List[Dict[str, Any]] = []
        #: discovering the addon file parses a 19 MB SavedVariables to tell
        #: an export from a backup; doing that every 2 minutes is waste, so
        #: the paths are cached and re-globbed every 10 minutes
        self._sources: Optional[Dict[str, Optional[str]]] = None
        self._sources_at = 0.0

    # -- state ---------------------------------------------------------
    def load_state(self) -> Dict[str, Any]:
        try:
            return json.loads(Path(self.cfg.state_path).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def save_state(self) -> None:
        if self.cfg.dry_run:
            return
        path = Path(self.cfg.state_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.state, indent=2, sort_keys=True), encoding="utf-8")
        tmp.replace(path)

    # -- helpers -------------------------------------------------------
    def sources(self, now: Optional[float] = None) -> Dict[str, Optional[str]]:
        now = float(now if now is not None else time.time())
        if self._sources is None or now - self._sources_at > 600:
            self._sources = self.sources_fn()
            self._sources_at = now
        return self._sources

    def con(self) -> sqlite3.Connection:
        return store.connect(self.cfg.intel_db or store.DEFAULT_DB)

    def tsm(self) -> sqlite3.Connection:
        con = sqlite3.connect(str(self.cfg.tsm_db or (ROOT / "tsm_markets.db")))
        con.row_factory = sqlite3.Row
        return con

    def realms(self) -> List[str]:
        """Every realm the account actually trades on, from the data.

        Inventory snapshots, undercut scans and recorded sales are the
        evidence: a realm appears as soon as one of them names it, and the
        list is rebuilt on every call -- so a new selling realm needs no
        edit and no restart.  There is deliberately no cap and no fixed
        fallback: guessing a realm would scan a market the player is not
        on and report it as the player's own.
        """
        if self.cfg.realms:
            return list(self.cfg.realms)
        names: List[str] = []
        try:
            con = self.con()
            for table in ("inventory_snapshot", "undercut_observation"):
                try:
                    rows = con.execute(
                        f"SELECT DISTINCT realm FROM {table}"
                        " WHERE realm <> ''").fetchall()
                except sqlite3.OperationalError:
                    continue
                names.extend(str(row[0]) for row in rows)
        except sqlite3.OperationalError:
            pass
        try:
            tsm = self.tsm()
            try:
                rows = tsm.execute(
                    "SELECT DISTINCT realm FROM auction_events"
                    " WHERE event_type='sale' AND realm <> ''").fetchall()
                names.extend(str(row[0]) for row in rows)
            finally:
                tsm.close()
        except (sqlite3.Error, OSError):
            pass
        seen = set()
        ordered: List[str] = []
        for name in names:
            key = name.strip().lower()
            if key and key not in seen:
                seen.add(key)
                ordered.append(name.strip())
        return sorted(ordered, key=str.lower)

    def alert(self, items: List[alerts.Alert], now: int) -> List[Dict[str, Any]]:
        if not items:
            return []
        delivered = alerts.deliver(items, self.state, now,
                                   url=self.cfg.webhook,
                                   notify=self.cfg.notify,
                                   dry_run=self.cfg.dry_run)
        logger.info("alerts: %s", json.dumps(delivered)[:500])
        return delivered

    # -- tasks ---------------------------------------------------------
    def task_addon(self, now: int) -> Dict[str, Any]:
        addon_path = self.cfg.addon_path or self.sources().get("addon") or ""
        result = self.runner([PY, "scripts/intel.py", "addon-import"])
        out: Dict[str, Any] = {"import": result}
        if result.get("returncode") not in (0, None):
            self.state.setdefault("errors", {})["addon"] = str(result.get("stderr"))[:300]
            return out
        self.state.setdefault("errors", {}).pop("addon", None)
        payload = result.get("payload") or {}
        out["import"] = {key: payload.get(key) for key in
                         ("events", "inventory", "sellers", "mail", "undercut",
                          "kills")}

        # The HUD queue: presses since the last import.
        seen = self.state.setdefault("actions_seen", {})
        new: List[Dict[str, Any]] = []
        if addon_path:
            try:
                new = action_log.new_actions(
                    action_log.parse_export_file(addon_path), seen)
            except (OSError, ValueError) as exc:
                logger.warning("could not read HUD actions: %s", exc)
        if new:
            action_log.mark_seen(new, seen, now)
            out["actions"] = new
            logger.info("HUD actions: %s", [a["name"] for a in new])
        # Every import is a fresh chance to match the posted list against
        # the last Blizzard snapshot, without waiting for the hourly job.
        out["undercut"] = self.derive_undercut(now)
        inventory_summary = (payload.get("inventory") or {}).get("reconciliation")
        if inventory_summary:
            out["inventory"] = inventory_summary
        # the alert names a realm; without one discovered there is nothing
        # honest to name, so it is skipped instead of guessing
        realms = self.realms()
        if realms:
            self.alert(alerts.inventory_alerts(inventory_summary or {},
                                               realms[0]), now)
        return out

    def task_journal(self, now: int) -> Dict[str, Any]:
        result = self.runner([PY, "scripts/journal.py", "import"])
        if result.get("returncode") not in (0, None):
            self.state.setdefault("errors", {})["journal"] = str(result.get("stderr"))[:300]
        else:
            self.state.setdefault("errors", {}).pop("journal", None)
        return {"journal": result.get("returncode"), "stdout": result.get("stdout", "")[-400:]}

    def task_sync(self, now: int) -> Dict[str, Any]:
        result = self.runner([PY, "scripts/sync_market.py", "--quiet", "--no-csv"])
        return {"sync": result.get("returncode"),
                "stdout": result.get("stdout", "")[-400:]}

    def task_csv(self, now: int) -> Dict[str, Any]:
        result = self.runner([PY, "scripts/fetch_csv.py"], timeout=3600)
        return {"csv": result.get("returncode"),
                "stdout": result.get("stdout", "")[-400:]}

    def task_train(self, now: int) -> Dict[str, Any]:
        result = self.runner([PY, "scripts/intel.py", "realm-ml"], timeout=3600)
        if result.get("returncode") not in (0, None):
            self.alert([alerts.error_alert("realm-ml",
                                           result.get("stderr") or "failed")], now)
            return {"train": result.get("returncode")}
        return {"train": "ok", "payload": result.get("payload")}

    def task_sentinel(self, now: int) -> Dict[str, Any]:
        """Refit only when the data justifies it, then leave the artifact."""
        result = self.runner([PY, "scripts/dfa_ml.py", "sentinel", "--if-needed"],
                             timeout=5400)
        out = {"sentinel": result.get("returncode"),
               "stdout": result.get("stdout", "")[-800:]}
        if result.get("returncode") not in (0, None):
            self.alert([alerts.error_alert("Sentinel training",
                                           result.get("stderr") or "failed")], now)
        # The prediction layer only reaches the game through the dataset;
        # rebuild it when a new artifact was promoted.
        if "promoted=True" in (result.get("stdout") or ""):
            out["export"] = self.task_export(now)
        return out

    def task_export(self, now: int) -> Dict[str, Any]:
        result = self.runner([PY, "scripts/dfa_pricing_export.py",
                              "--realm-model", "--all-realms"])
        out = {"export": result.get("returncode"),
               "stdout": result.get("stdout", "")[-600:]}
        if result.get("returncode") not in (0, None):
            # the game still reads the previous dataset: keep the change
            # queued so the retry lands after the gap, not on every tick
            self.state["export_pending"] = True
            self.alert([alerts.error_alert("pricing export",
                                           result.get("stderr") or "failed")], now)
            return out
        self.state.pop("export_pending", None)
        deployed = self.deploy_dataset(now)
        if deployed:
            out["deploy"] = deployed
        return out

    def deploy_dataset(self, now: int) -> Dict[str, Any]:
        """Ship the fresh dataset to the project copy and to the game's addon.

        The paths come from ``PricingConfig`` and ``--addons-dir``; nothing
        is searched for.  A deployment that only reached one of them is a
        failure and says so.
        """
        if self.cfg.dry_run:
            return {"skipped": "dry-run"}
        from woweconomy.pricing import deploy as deployment
        from woweconomy.pricing.config import PricingConfig

        config = PricingConfig()
        if self.cfg.dataset_path:
            config.dataset_path = Path(self.cfg.dataset_path)
        addons = Path(self.cfg.addons_dir) if self.cfg.addons_dir else None
        result = deployment.deploy(config, addons_dir=addons)
        if result.superseded:
            # a newer export landed mid-copy: queue it, the next tick after
            # the gap ships the newer bytes to both copies
            self.state["export_pending"] = True
        logger.info("dataset deploy: ok=%s %s", result.ok, result.summary())
        if not result.ok:
            self.alert([alerts.error_alert("dataset deploy", result.summary())], now)
        return result.as_dict()

    def task_deploy(self, now: int) -> Dict[str, Any]:
        """Ship a dataset that changed outside the daemon to both copies."""
        return {"deploy": self.deploy_dataset(now)}

    def task_ah(self, now: int) -> Dict[str, Any]:
        con = self.con()
        realms = self.realms()
        if not realms:
            logger.info("ah: no selling realm discovered yet; nothing to scan")
            return {"realms": {}, "skipped": "no realm discovered"}
        out: Dict[str, Any] = {"realms": {}}
        for realm in realms:
            row = con.execute(
                "SELECT connected_realm_id FROM ah_realm"
                " WHERE LOWER(realm_name) = LOWER(?) LIMIT 1", (realm,)).fetchone()
            realms_state = self.state.setdefault("ah", {}).setdefault(realm, {})
            argv = [PY, "scripts/intel.py", "ah-fetch", "--realm", realm]
            if not row:
                argv.append("--sync-realms")
            elif realms_state.get("last_modified"):
                argv.extend(["--modified-since", realms_state["last_modified"]])
            result = self.runner(argv, timeout=900)
            entry = {"returncode": result.get("returncode")}
            if result.get("returncode") not in (0, None):
                entry["error"] = str(result.get("stderr"))[:300]
                out["realms"][realm] = entry
                continue
            if "304" in str(result.get("stderr") or "") or result.get("payload") is None:
                entry["unchanged"] = True
            else:
                entry["snapshot"] = result.get("payload")
                from datetime import datetime, timezone
                # The header Blizzard sent is the vintage of the data; the
                # local clock is only a fallback.  Storing "now" made the
                # next If-Modified-Since newer than the server's copy, so a
                # realm could answer 304 long after it had new auctions.
                stored = con.execute(
                    "SELECT api_last_modified FROM ah_snapshot"
                    " WHERE LOWER(realm) = LOWER(?)"
                    " ORDER BY captured_at DESC LIMIT 1",
                    (realm,)).fetchone()
                realms_state["last_modified"] = (
                    (stored[0] if stored else "")
                    or datetime.now(timezone.utc).strftime(
                        "%a, %d %b %Y %H:%M:%S GMT"))
                entry["undercut"] = self.derive_undercut(now, realm)
                entry["resets"] = self.detect_resets(realm)
            out["realms"][realm] = entry
        return out

    def derive_undercut(self, now: int, realm: str = "") -> Dict[str, Any]:
        if self.cfg.dry_run:
            return {"dry_run": True}
        con = self.con()
        targets = [realm] if realm else self.realms()
        out: Dict[str, Any] = {}
        for target in targets:
            result = ah_undercut.scan_realm(con, target)
            if result.get("error"):
                logger.info("undercut %s: %s", target, result["error"])
                out[target] = result
                continue
            # The market around the listings the scan saw: measured noise,
            # price path, supply change and our own share of the stock.  It
            # is a reading of the databases already on disk, not a new scan,
            # and a realm with no snapshot still gets the price path.
            market = self.realm_market(target, now)
            advice = relist.advise(con, target, days=14, market=market)
            result["advice"] = advice.get("counts")
            self.alert(alerts.undercut_alerts(advice), now)
            out[target] = result
        return out if not realm else out.get(realm, {})

    def realm_market(self, realm: str, now: int):
        """The market context for the realm's watched items, or None.

        Only the items the undercut scan knows about are asked for: this is
        a reading of one realm's listings, not a full market build.  A
        database that cannot be read leaves the context empty, and the
        relist advice then falls back to its gap/queue/time rules.
        """
        con = self.con()
        try:
            items = [int(row[0]) for row in con.execute(
                "SELECT DISTINCT item_id FROM undercut_observation"
                " WHERE LOWER(realm) = LOWER(?) AND item_id > 0", (realm,))]
        except sqlite3.OperationalError:
            return None
        if not items:
            return None
        try:
            return build_realm_market(
                self.cfg.tsm_db or (ROOT / "tsm_markets.db"),
                self.cfg.intel_db or store.DEFAULT_DB,
                realm, items, now=int(now), intel=con)
        except Exception as exc:  # pragma: no cover - defensive
            logger.info("market context unavailable for %s: %s", realm, exc)
            return None

    def detect_resets(self, realm: str) -> List[Dict[str, Any]]:
        con = self.con()
        heads = con.execute(
            "SELECT snapshot_id, captured_at FROM ah_snapshot"
            " WHERE LOWER(realm) = LOWER(?) ORDER BY captured_at DESC LIMIT 2",
            (realm,)).fetchall()
        if len(heads) < 2:
            return []
        current = ah_undercut.price_state(
            ah_undercut.load_api_auctions(con, heads[0]["snapshot_id"]))
        previous = ah_undercut.price_state(
            ah_undercut.load_api_auctions(con, heads[1]["snapshot_id"]))
        resets = ah_undercut.detect_price_resets(previous, current)
        if resets:
            # ah_auction carries ids and prices, not names; the names come
            # from the stock picture and from earlier undercut scans.
            names: Dict[int, str] = {}
            try:
                for row in con.execute(
                        "SELECT item_id, item_name FROM undercut_observation"
                        " WHERE LOWER(realm) = LOWER(?) AND item_name <> ''"
                        " GROUP BY item_id", (realm,)):
                    names[int(row["item_id"])] = str(row["item_name"])
            except sqlite3.OperationalError:
                pass
            for row in con.execute(
                    "SELECT item_id, MAX(item_name) FROM inventory_posted_listing"
                    " WHERE item_name <> '' GROUP BY item_id"):
                names.setdefault(int(row[0]), str(row[1]))
            self.alert(alerts.reset_alerts(resets, names, realm), int(time.time()))
        return resets[:10]

    # -- the tick ------------------------------------------------------
    def tick(self, now: Optional[int] = None) -> Dict[str, Any]:
        now = int(now if now is not None else time.time())
        sources = self.sources(now)
        settling = settling_sources(self.state, sources, now, self.cfg.stable_s)
        changed = changed_sources(self.state, sources, now, self.cfg.stable_s)
        tasks = plan(now, self.state, changed, self.cfg)
        logger.info("tick: changed=%s tasks=%s",
                    [name for name, flag in changed.items() if flag],
                    [task.name for task in tasks])
        results = []
        for task in tasks:
            handler = getattr(self, f"task_{task.name}", None)
            if handler is None:
                logger.warning("no handler for %s", task.name)
                continue
            started = time.time()
            try:
                detail = handler(now)
            except Exception as exc:  # noqa: BLE001 - one bad task, not a dead loop
                logger.exception("task %s failed", task.name)
                detail = {"error": str(exc)}
                self.alert([alerts.error_alert(task.name, str(exc))], now)
            results.append({"task": task.name, "reason": task.reason,
                            "seconds": round(time.time() - started, 1),
                            "detail": detail})
            self.mark_task(task.name, now)
        if not self.cfg.dry_run:
            mark_sources(self.state, sources, skip=settling)
        self.save_state()
        self.summary = results
        if results:
            logger.info("tick done: %s", json.dumps([
                {"task": item["task"], "seconds": item["seconds"],
                 "error": (item["detail"] or {}).get("error")
                 if isinstance(item.get("detail"), dict) else None}
                for item in results]))
        return {"at": now, "changed": [k for k, v in changed.items() if v],
                "tasks": results}

    def mark_task(self, name: str, now: int) -> None:
        if self.cfg.dry_run:
            return
        point = {"addon": "addon", "journal": "journal", "sync": "tsm",
                 "csv": "csv", "train": "train", "sentinel": "sentinel",
                 "export": "export", "ah": "ah"}.get(name)
        if point:
            self.state.setdefault("last", {})[point] = int(now)


# ----------------------------------------------------------------------- main
def acquire_lock(path: Path) -> Optional[Any]:
    """Hold an exclusive lock for the life of this process.

    Two daemons on one state file double every export and race the state
    writes -- the log then shows two tick cadences interleaved.  The OS
    drops the lock when the process dies, so a crash never leaves it stale.

    The locked byte sits far from the pid at offset 0, so ``type`` on the
    lock file still says who holds it.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_RDWR | os.O_CREAT, 0o644)
    handle = os.fdopen(descriptor, "r+", encoding="utf-8")
    try:
        if os.name == "nt":
            import msvcrt
            handle.seek(LOCK_REGION)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        return None
    handle.seek(0)
    handle.write(f"{os.getpid():<12}")
    handle.flush()
    return handle


def configure_logging(path: Path, verbose: bool) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handlers: List[logging.Handler] = [logging.FileHandler(path, encoding="utf-8")]
    if verbose:
        handlers.append(logging.StreamHandler())
    logging.basicConfig(level=logging.INFO, handlers=handlers,
                        format="%(asctime)s %(levelname)-7s %(message)s")


def load_env() -> None:
    """Read .env so the webhook can stay out of every command line."""
    try:
        from dotenv import load_dotenv
        load_dotenv(ROOT / ".env")
    except Exception:  # noqa: BLE001 - a missing dotenv is not fatal
        pass


def build_config(args) -> Config:
    return Config(
        interval=args.interval,
        realms=[r.strip() for r in args.realms.split(",") if r.strip()],
        train_every_h=args.train_every_h,
        ah_every_h=args.ah_every_h,
        export_every_min=args.export_every_min,
        export_min_gap_min=args.export_min_gap_min,
        offline=args.offline,
        stable_s=args.stable_s,
        with_csv=args.with_csv,
        dry_run=args.dry_run,
        notify=not args.no_notify,
        webhook=args.webhook_url or alerts.webhook_url(),
        addons_dir=args.addons_dir,
        state_path=Path(args.state),
        log_path=Path(args.log),
        addon_path=args.addon_path,
        intel_db=args.intel_db,
        tsm_db=args.tsm_db,
    )


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--once", action="store_true", help="one tick and exit")
    ap.add_argument("--interval", type=int, default=120, help="seconds between ticks")
    ap.add_argument("--dry-run", action="store_true",
                    help="print the plan, run nothing, write nothing")
    ap.add_argument("--offline", action="store_true",
                    help="skip the Blizzard jobs (file inputs only)")
    ap.add_argument("--stable-s", type=int, default=30,
                    help="wait this long after a file's last write before reading it")
    ap.add_argument("--with-csv", action="store_true",
                    help="also fetch the public TSM CSVs daily (slow)")
    ap.add_argument("--realms", default="", help="comma separated (default: from the inventory)")
    ap.add_argument("--train-every-h", type=float, default=6.0)
    ap.add_argument("--ah-every-h", type=float, default=1.0)
    ap.add_argument("--export-every-min", type=float, default=360.0,
                    help="freshness fallback: rebuild the dataset this often")
    ap.add_argument("--export-min-gap-min", type=float, default=30.0,
                    help="minimum minutes between two exports (changes queue)")
    ap.add_argument("--no-notify", action="store_true", help="no desktop toasts")
    ap.add_argument("--webhook-url", default="", help="Discord webhook (else DFA_DISCORD_WEBHOOK)")
    ap.add_argument("--addons-dir", default="", help="Interface/AddOns directory")
    ap.add_argument("--addon-path", default="", help="DFA SavedVariables file")
    ap.add_argument("--intel-db", default="", help="intel.sqlite (default data/intel.sqlite)")
    ap.add_argument("--tsm-db", default="", help="tsm_markets.db")
    ap.add_argument("--state", default=str(DEFAULT_STATE))
    ap.add_argument("--log", default=str(DEFAULT_LOG))
    ap.add_argument("--lock", default=str(DEFAULT_LOCK),
                    help="single-instance lock file")
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()

    load_env()
    cfg = build_config(args)
    configure_logging(cfg.log_path, args.verbose or args.once)
    watch = Watch(cfg)

    if args.once or args.dry_run:
        result = watch.tick()
        print(json.dumps(result, indent=2, ensure_ascii=False, default=str))
        failed = [item["task"] for item in result["tasks"]
                  if isinstance(item.get("detail"), dict)
                  and item["detail"].get("error")]
        return 1 if failed else 0

    lock = acquire_lock(Path(args.lock))
    if lock is None:
        logger.error("another DFA Sentinel already holds %s; exiting", args.lock)
        return 0

    logger.info("DFA Sentinel started; interval %ss, realms %s, webhook %s",
                cfg.interval, watch.realms(), "on" if cfg.webhook else "off")
    while True:
        started = time.time()
        try:
            watch.tick()
        except Exception:  # noqa: BLE001 - the loop outlives one bad tick
            logger.exception("tick failed")
        elapsed = time.time() - started
        time.sleep(max(5.0, cfg.interval - elapsed))


if __name__ == "__main__":
    raise SystemExit(main())
