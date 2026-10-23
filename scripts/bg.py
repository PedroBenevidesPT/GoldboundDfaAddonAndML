"""Start, inspect and stop a pipeline command in the background.

The long steps of this project (the TSM fetch, the pricing export, a Sentinel
fit) take minutes.  Running one in the foreground costs the whole turn; running
one and forgetting it costs a rate limit, a half-written database or a stale
lock.  This is the small wrapper both mistakes need:

    python scripts/bg.py start export -- python -u scripts/dfa_pricing_export.py --all-realms
    python scripts/bg.py status export
    python scripts/bg.py logs export -n 40
    python scripts/bg.py stop export
    python scripts/bg.py status                  # every job in logs/

Each job owns three files under ``logs/`` (gitignored in full):

    <name>.log        stdout+stderr, appended, never truncated by this script
    <name>.pid        the pid of the command itself -- the only thing signalled
    <name>.exit       the exit code, written the moment the command ends
    <name>.meta.json  command line, cwd and start time (for ``status``)

A job is started through a detached supervisor, so this command returns while
the work continues after the terminal closes.  The pid recorded is the
*command's* pid, not the supervisor's, so stopping cannot leave the worker
behind: the supervisor exits when the command does, and writes ``.exit``.

Stopping: POSIX sends SIGTERM to the job's process group and escalates to
SIGKILL after ``--timeout``.  Windows has no SIGTERM for a console program:
``taskkill /T`` is only a request there, so the helper gives it a short grace
(or ``--timeout`` if smaller) and then does ``taskkill /T /F``.  Either way the
kill walks the *recorded pid's* tree, never an image name.  A hard stop can
leave ``*.sqlite-wal`` next to a database; SQLite recovers on the next open,
but prefer letting a writer finish when the job is short.

Names are free-form (``[A-Za-z0-9._-]``); one job per name.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_LOGS = ROOT / "logs"

#: a name is a file name, so keep it to what every filesystem accepts
ALLOWED = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-")

#: how long ``start`` waits for the supervisor to report the command's pid
START_TIMEOUT_S = 10.0

#: Windows: the child gets no console of its own and is not killed by Ctrl+C
DETACHED_PROCESS = 0x00000008
CREATE_NEW_PROCESS_GROUP = 0x00000200
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
STILL_ACTIVE = 259
ERROR_ACCESS_DENIED = 5


# --------------------------------------------------------------------- paths
def safe_name(name: str) -> str:
    if not name or any(ch not in ALLOWED for ch in name):
        raise SystemExit(f"invalid job name {name!r}: use letters, digits, '.', '_' or '-'")
    if name.startswith("."):
        raise SystemExit(f"invalid job name {name!r}: cannot start with '.'")
    return name


def job_paths(logs: Path, name: str) -> dict:
    return {
        "log": logs / f"{name}.log",
        "pid": logs / f"{name}.pid",
        "exit": logs / f"{name}.exit",
        "meta": logs / f"{name}.meta.json",
    }


def write_atomic(path: Path, text: str) -> None:
    """A reader must never see a half-written pid or exit code."""
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def read_int(path: Path):
    text = read_text(path)
    try:
        return int(text)
    except ValueError:
        return None


# --------------------------------------------------------------- liveness
def pid_alive(pid: int) -> bool:
    if not pid or pid <= 0:
        return False
    if os.name == "nt":
        return _windows_alive(pid)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # someone else's process: it exists
    return True


def _windows_alive(pid: int) -> bool:
    """``os.kill(pid, 0)`` is not a probe on Windows -- it terminates.

    ``TerminateProcess`` is what ``os.kill`` maps to there, so liveness has to
    go through ``OpenProcess`` + ``GetExitCodeProcess``.
    """
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.restype = wintypes.HANDLE
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        # a live process owned by another user answers ACCESS_DENIED
        return ctypes.get_last_error() == ERROR_ACCESS_DENIED
    try:
        code = wintypes.DWORD()
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
            return False
        return code.value == STILL_ACTIVE
    finally:
        kernel32.CloseHandle(handle)


# ------------------------------------------------------------- supervisor
def cmd_supervise(name: str, logs: Path, argv: list) -> int:
    """Run one command detached, record its pid, then its exit code.

    This is what ``start`` launches.  It is deliberately dumb: no retries, no
    parsing, no opinion about the command -- a wrapper that reinterprets a job
    is a second place for the job's behaviour to be wrong.

    Every failure is written to the job's own log.  A supervisor that dies
    quietly is indistinguishable from a command that did nothing.
    """
    paths = job_paths(logs, name)
    started = time.time()
    header = (
        f"\n=== {name} start {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(started))}"
        f" | {' '.join(argv)} ===\n"
    )
    log = paths["log"].open("ab")
    log.write(header.encode("utf-8", "replace"))
    log.flush()

    kwargs = dict(stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                  cwd=str(ROOT))
    if os.name == "nt":
        kwargs["creationflags"] = DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
    else:
        kwargs["start_new_session"] = True

    try:
        child = subprocess.Popen(argv, **kwargs)
    except OSError as exc:
        import traceback
        log.write(f"could not start {argv!r}: {exc}\n".encode("utf-8", "replace"))
        log.write(traceback.format_exc().encode("utf-8", "replace"))
        log.close()
        # 127 is the shell's "command not found", which is what this is
        write_atomic(paths["exit"], "127\n")
        return 127

    write_atomic(paths["meta"], json.dumps({
        "name": name,
        "command": argv,
        "cwd": str(ROOT),
        "started": started,
        "started_iso": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(started)),
    }, indent=2))
    # The pid goes last: once it exists, the job is discoverable and stoppable,
    # so a reader can never see a pid for a supervisor that has not started.
    write_atomic(paths["pid"], f"{child.pid}\n")

    code = child.wait()
    write_atomic(paths["exit"], f"{code}\n")
    log.write(f"=== {name} end {time.strftime('%Y-%m-%d %H:%M:%S')} | exit {code} ===\n"
              .encode("utf-8", "replace"))
    log.close()
    return 0


# ----------------------------------------------------------------- state
def job_state(paths: dict) -> dict:
    """One vocabulary for every caller: running / exited / lost / absent."""
    pid = read_int(paths["pid"])
    code = read_int(paths["exit"])
    meta = {}
    if paths["meta"].exists():
        try:
            meta = json.loads(read_text(paths["meta"]))
        except ValueError:
            meta = {}
    alive = pid_alive(pid) if pid else False

    if code is not None:
        state = "exited"
    elif pid and alive:
        state = "running"
    elif pid:
        # a pid on disk, no exit code, no process: killed outside this script
        state = "lost"
    else:
        state = "absent"

    elapsed = None
    if meta.get("started"):
        end = paths["exit"].stat().st_mtime if paths["exit"].exists() else time.time()
        elapsed = max(0.0, end - float(meta["started"]))

    return {
        "name": paths["log"].stem,
        "state": state,
        "pid": pid,
        "exit": code,
        "started": meta.get("started_iso", ""),
        "elapsed_s": round(elapsed, 1) if elapsed is not None else None,
        "command": " ".join(meta.get("command", [])),
        "log": str(paths["log"]),
        "log_bytes": paths["log"].stat().st_size if paths["log"].exists() else 0,
    }


def known_jobs(logs: Path) -> list:
    """Jobs this script managed: a pid, an exit code or a meta file.

    A bare ``*.log`` is not enough -- ``logs/`` also holds files written by the
    pipeline itself, and those are not jobs anyone can stop.
    """
    names = set()
    for path in logs.glob("*"):
        if path.suffix in {".pid", ".exit"}:
            names.add(path.stem)
        elif path.name.endswith(".meta.json"):
            names.add(path.name[: -len(".meta.json")])
    return sorted(n for n in names if n)


def human(info: dict) -> str:
    line = f"{info['name']:<24} {info['state']:<7}"
    if info["pid"]:
        line += f" pid {info['pid']:<7}"
    if info["elapsed_s"] is not None:
        minutes, seconds = divmod(int(info["elapsed_s"]), 60)
        line += f" {minutes}m{seconds:02d}s"
    if info["exit"] is not None:
        line += f" exit {info['exit']}"
    line += f"  {info['log_bytes']:,} B"
    return line


# ---------------------------------------------------------------- verbs
def cmd_start(args) -> int:
    name = safe_name(args.name)
    logs = Path(args.logs_dir)
    logs.mkdir(parents=True, exist_ok=True)
    paths = job_paths(logs, name)

    pid = read_int(paths["pid"])
    if pid and pid_alive(pid):
        print(f"{name} is already running (pid {pid}); stop it first", file=sys.stderr)
        return 2
    # a stale pid/exit pair from an earlier run must not answer for this one
    for stale in ("pid", "exit"):
        paths[stale].unlink(missing_ok=True)

    # the `--` is what marks the command for `_supervise`; without it its own
    # parser would see the command's flags as its own unrecognised arguments
    supervisor = [sys.executable, "-u", str(Path(__file__).resolve()),
                  "_supervise", name, "--"] + list(args.command)
    kwargs = dict(stdin=subprocess.DEVNULL,
                  stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                  cwd=str(ROOT), env={**os.environ, "DFA_BG_LOGS_DIR": str(logs)})
    if os.name == "nt":
        kwargs["creationflags"] = DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
    else:
        kwargs["start_new_session"] = True
    subprocess.Popen(supervisor, **kwargs)

    deadline = time.time() + START_TIMEOUT_S
    while time.time() < deadline and read_int(paths["pid"]) is None:
        # the supervisor records 127 when the command itself is unusable
        if read_int(paths["exit"]) is not None:
            break
        time.sleep(0.1)
    child = read_int(paths["pid"])
    if child is None:
        code = read_int(paths["exit"])
        print(f"{name} did not start (exit {code}); last lines of {paths['log']}:",
              file=sys.stderr)
        print(tail(paths["log"], 10) if paths["log"].exists() else "", file=sys.stderr)
        return 1

    info = job_state(paths)
    if args.json:
        print(json.dumps(info, indent=2))
    else:
        print(f"started {name} pid {child}")
        print(f"  log    {paths['log']}")
        print(f"  status python scripts/bg.py status {name}")
    return 0


def cmd_status(args) -> int:
    logs = Path(args.logs_dir)
    if not logs.exists():
        print(f"no logs directory at {logs}", file=sys.stderr)
        return 1
    names = [safe_name(args.name)] if args.name else known_jobs(logs)
    if not names:
        print(f"no jobs under {logs}", file=sys.stderr)
        return 1

    infos = [job_state(job_paths(logs, n)) for n in names]
    if args.json:
        print(json.dumps(infos if args.name is None else infos[0], indent=2))
        return 0
    for info in infos:
        print(human(info))
        if info["command"]:
            print(f"  {info['command']}")
    # a job that never wrote an exit code is the only one that needs a human
    lost = [i["name"] for i in infos if i["state"] == "lost"]
    for name in lost:
        print(f"\n{name}: pid is gone with no exit code recorded "
              f"(killed outside this script, or the machine restarted)", file=sys.stderr)
    return 0


def _terminate(pid: int, timeout: float) -> str:
    """Ask, then insist.  Never by name -- only this pid and its children."""
    if not pid_alive(pid):
        return "already-exited"
    if os.name == "nt":
        taskkill = shutil.which("taskkill")
        if taskkill:
            # /T walks the recorded pid's tree; /F only if the polite pass fails.
            # A console program has no window to close, so the polite pass is a
            # request it usually never sees -- do not spend the whole timeout on it.
            subprocess.run([taskkill, "/PID", str(pid), "/T"],
                           capture_output=True, text=True)
            deadline = time.time() + min(timeout, 3.0)
            while time.time() < deadline and pid_alive(pid):
                time.sleep(0.2)
            if pid_alive(pid):
                subprocess.run([taskkill, "/PID", str(pid), "/T", "/F"],
                               capture_output=True, text=True)
        else:  # no taskkill: TerminateProcess via os.kill
            os.kill(pid, signal.SIGTERM)
    else:
        try:
            os.killpg(os.getpgid(pid), signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            pass
        deadline = time.time() + timeout
        while time.time() < deadline and pid_alive(pid):
            time.sleep(0.2)
        if pid_alive(pid):
            try:
                os.killpg(os.getpgid(pid), signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass

    deadline = time.time() + 5.0
    while time.time() < deadline and pid_alive(pid):
        time.sleep(0.2)
    return "stopped" if not pid_alive(pid) else "still-running"


def cmd_stop(args) -> int:
    name = safe_name(args.name)
    logs = Path(args.logs_dir)
    paths = job_paths(logs, name)
    pid = read_int(paths["pid"])
    code = read_int(paths["exit"])

    if not pid:
        if code is not None:
            print(f"{name} is not running (last exit {code})")
            return 0
        print(f"no pid recorded for {name} under {logs}", file=sys.stderr)
        return 1

    if not pid_alive(pid):
        paths["pid"].unlink(missing_ok=True)
        print(f"{name} was not running; pid {pid} was stale")
        return 0

    result = _terminate(pid, args.timeout)
    if result != "stopped":
        print(f"{name} (pid {pid}) still running after {args.timeout:.0f}s; "
              f"it ignored the stop", file=sys.stderr)
        return 1

    # the supervisor writes the exit code when the command dies; give it a beat
    deadline = time.time() + 5.0
    while time.time() < deadline and read_int(paths["exit"]) is None:
        time.sleep(0.1)
    paths["pid"].unlink(missing_ok=True)

    code = read_int(paths["exit"])
    if code is None:
        print(f"stopped {name} (pid {pid}); no exit code recorded")
    else:
        print(f"stopped {name} (pid {pid}); exit {code}")
    return 0


def cmd_logs(args) -> int:
    name = safe_name(args.name)
    path = Path(args.logs_dir) / f"{name}.log"
    if not path.exists():
        print(f"no log for {name} at {path}", file=sys.stderr)
        return 1
    text = tail(path, args.lines)
    sys.stdout.write(text)
    if text and not text.endswith("\n"):
        sys.stdout.write("\n")
    return 0


def tail(path: Path, lines: int) -> str:
    """Read the end of a log without loading a 500 MB file into memory."""
    with path.open("rb") as handle:
        handle.seek(0, os.SEEK_END)
        size = handle.tell()
        block = min(size, 1 << 16)
        handle.seek(size - block)
        chunk = handle.read()
    text = chunk.decode("utf-8", "replace")
    return "".join(text.splitlines(keepends=True)[-lines:])


# ---------------------------------------------------------------- entry
def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--logs-dir", default=str(DEFAULT_LOGS),
                        help="where the .log/.pid/.exit files live (default: logs/)")
    sub = parser.add_subparsers(dest="verb", required=True)

    start = sub.add_parser("start", help="run a command detached")
    start.add_argument("name")
    start.add_argument("--json", action="store_true")
    start.set_defaults(func=cmd_start)

    status = sub.add_parser("status", help="state of one job, or all of them")
    status.add_argument("name", nargs="?")
    status.add_argument("--json", action="store_true")
    status.set_defaults(func=cmd_status)

    stop = sub.add_parser("stop", help="stop a job by its recorded pid")
    stop.add_argument("name")
    stop.add_argument("--timeout", type=float, default=15.0,
                      help="seconds to wait before the hard kill (default: 15)")
    stop.set_defaults(func=cmd_stop)

    logs = sub.add_parser("logs", help="tail a job's log")
    logs.add_argument("name")
    logs.add_argument("-n", "--lines", type=int, default=40)
    logs.set_defaults(func=cmd_logs)

    # internal: what `start` launches, detached, to own the child and its exit code
    sup = sub.add_parser("_supervise")
    sup.add_argument("name")
    sup.set_defaults(func=None)

    # Everything after the first `--` is the command, verbatim.  REMAINDER
    # cannot be used for this: it also swallows options that precede it, which
    # silently turned `--logs-dir X` into the job's argv.
    raw = list(sys.argv[1:] if argv is None else argv)
    if "--" in raw:
        split = raw.index("--")
        head, command = raw[:split], raw[split + 1:]
    else:
        head, command = raw, []

    args = parser.parse_args(head)
    if args.verb in {"_supervise", "start"} and not command:
        parser.error(f"{args.verb} needs a command after --")

    if args.verb == "_supervise":
        # `start` passes the directory out of band: the job's own argv is
        # closed to anything this script needs for itself
        logs = Path(os.environ.get("DFA_BG_LOGS_DIR", str(DEFAULT_LOGS)))
        logs.mkdir(parents=True, exist_ok=True)
        return cmd_supervise(safe_name(args.name), logs, command)
    args.command = command
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
