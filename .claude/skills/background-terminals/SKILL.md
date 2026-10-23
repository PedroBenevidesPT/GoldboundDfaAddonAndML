---
name: background-terminals
description: Run long commands and daemons detached from the chat session with a log file and a recorded PID, then check and stop them cleanly. Use when a command takes minutes, must survive the end of a turn, or must be killed on purpose instead of left running and blocking the session.
---

# Background terminals

A command that runs in the foreground of the agent session is a command the
session pays for: the turn cannot end, output piles up in context, and an
interactive prompt (a `y/n`, a `pager`, an `input()`) hangs everything. Long
work should be **detached**, **logged to a file**, and **identified by a PID**,
so that it can be checked with a `tail` and stopped with a `kill`.

## The contract

Every background job gets three files, written before the job starts:

| What | Where | Why |
|---|---|---|
| log | `logs/<name>.log` | the only thing you read back |
| pid | `logs/<name>.pid` | the only thing you kill; never a name pattern |
| exit | `logs/<name>.exit` | `0`/non-zero, written by the wrapper, so "did it work?" is not a guess |

All three live under `logs/`, which is gitignored in full. Do not put runtime
files in `data/`: only a handful of names there are ignored (`watch.lock`,
`watch_state.json`, the databases), so a `data/<name>.pid` would show up as an
untracked file in `git status`.

## Use the helper first

`scripts/bg.py` already implements this contract, and its supervisor is what
records the exit code after the terminal is gone:

```bash
python scripts/bg.py start export -- python -u scripts/dfa_pricing_export.py --all-realms
python scripts/bg.py status export      # or no name: every job in logs/
python scripts/bg.py logs export -n 40
python scripts/bg.py stop export
```

It writes `logs/<name>.meta.json` too, so `status` can show the command and how
long it has been running.  The recipes below are what it does — reach for them
only when the helper is not available, or when a command needs flags it does
not expose.

## Running something long, once

Git Bash on Windows; the same shape works on Linux/macOS.

```bash
mkdir -p logs
NAME=fetch_tsm

nohup python -u scripts/fetch_tsm_snapshots.py --limit 4 \
  > "logs/$NAME.log" 2>&1 < /dev/null &
echo $! > "logs/$NAME.pid"
echo "started $NAME pid $(cat logs/$NAME.pid)"
```

- `-u` / `PYTHONUNBUFFERED=1` — without it the log stays empty for minutes and
  looks like a hang.
- `< /dev/null` — the job can never wait on stdin. This is the single most
  common cause of "it just stopped".
- `2>&1` into the same log — a traceback in a lost stderr is the same as no
  traceback.
- Don't pipe the job into `tail`/`head`: the pipe process dies with the shell
  and takes the job's output with it.

To capture the exit code without keeping the shell alive, wrap it — and mind
that `$!` is then the **wrapper shell**, not the python process (measured on
Git Bash: killing the wrapper left the child running):

```bash
nohup bash -c 'python -u scripts/dfa_pricing_export.py --all-realms & \
  child=$!; echo "$child" > logs/export.worker.pid; \
  wait "$child"; echo $? > logs/export.exit' \
  > logs/export.log 2>&1 < /dev/null &
echo $! > logs/export.pid
```

Without the wrapper (`nohup python … &`) `$!` *is* the python PID, so the
simple recipe needs only one file. The wrapper is what introduces the second
PID, and the worker is the one that must be signalled.

For a daemon the project already owns (`scripts/watch.py`), use the daemon
itself instead of reinventing it — it holds `data/watch.lock`, writes
`logs/watch.log` and refuses to start twice:

```bash
nohup python -u scripts/watch.py > logs/watch.log 2>&1 < /dev/null &
echo $! > logs/watch.pid
```

## Checking on it

```bash
kill -0 "$(cat logs/export.pid)" 2>/dev/null && echo running || echo gone
tail -n 40 logs/export.log
cat logs/export.exit 2>/dev/null || echo "still running (no exit yet)"
```

`kill -0` answers "is this PID alive", which is not the same question as "is it
making progress". Progress is the log's mtime:

```bash
ls -l --time-style=+%H:%M:%S logs/export.log
```

A job with a live PID and a log that has not moved in ten minutes is stuck, not
busy. Read the last lines before deciding either way.

## Stopping it — and letting go of the session

Stop by the PID you recorded. Signal first, wait, escalate only if needed:

```bash
PID=$(cat logs/export.pid)

kill "$PID"                      # SIGTERM: let it close the DB / finish a row
for i in 1 2 3 4 5 6 7 8 9 10; do
  kill -0 "$PID" 2>/dev/null || break
  sleep 1
done
kill -0 "$PID" 2>/dev/null && kill "$PID"   # only now, and only this PID
pkill -P "$PID" 2>/dev/null                 # children of *this* pid, if any
```

If the job was started through the exit-code wrapper, signal the worker too —
otherwise it keeps the log open and holds the database until it finishes:

```bash
[ -f logs/export.worker.pid ] && kill "$(cat logs/export.worker.pid)"
```

Then confirm nothing survives, and clean the runtime files:

```bash
ps -ef | grep -c "[d]fa_pricing_export.py"   # 0
rm -f logs/export.pid logs/export.worker.pid
```

Rules that matter:

- **Never** `pkill -f <script>`, `pgrep -f | xargs kill` or `killall python`.
  The agent's own command line can contain the same words; a pattern kill can
  take down the shell running it, a test suite, or the session itself.
- Killing a parent does not kill a `python` child on Windows reliably. If the
  job spawned workers (TSM fetcher, export), use `pkill -P "$PID"` — children
  of the recorded PID — not a name pattern.
- A SIGKILLed writer leaves `*.sqlite-wal` / lock files behind. Prefer SIGTERM,
  and after a kill check `data/watch.lock` and stale `-wal`/`-shm` files before
  starting the same job again.
- Keep the `.log` after stopping: it is the only record of why the job failed.

### Stop using the session

"Detached" is not done until the foreground is empty:

1. Every backgrounded command ends with `&` **and** `</dev/null` **and**
   `2>&1` to its own log — nothing waits on the terminal.
2. Interactive prompts are disabled at the source: `--yes`, `--quiet`,
   `--no-input`, `--non-interactive`, or `--once` for daemons. A job that can
   ask a question will ask it at 3 a.m. and sit there for days.
3. After starting a job, print the pid and the log path and end the turn. Do
   not `tail -f`, do not `wait`, do not sleep in a loop — that is the session
   being used by a job that does not need it.
4. Before the turn ends, decide explicitly for each job: **keep running**
   (say so, with its pid and log) or **stopped** (say so, with its exit code).
   Leaving it undecided is how a stray fetch burns a rate limit all night.

## Concurrent jobs in this project

- SQLite writers do not share a file well. Two imports/exports against
  `data/intel.sqlite` or `tsm_markets.db` at the same time can block each
  other; run them one after another, or let `scripts/watch.py` serialise them.
- `scripts/fetch_tsm_snapshots.py` is rate limited by IP (~60 requests/hour).
  A background run that is killed and immediately restarted re-requests the
  same files; check the log for the last `429` and honour the recorded cooldown
  instead of retrying.
- The desktop app writes `AppData.lua` every few minutes. A job reading it
  while it is being rewritten reads half a file; wait for 30 s of stable
  size/mtime (the daemon's `--stable-s`) before trusting it.

## Checklist

- [ ] log, pid and exit file under `logs/`
- [ ] `</dev/null`, `2>&1`, `-u`, non-interactive flags
- [ ] pid printed and reported in the answer
- [ ] checked with `kill -0` + log tail, not assumed
- [ ] stopped with `kill <pid>` (wrapper jobs: also the worker pid)
- [ ] no stray worker left behind — `ps -ef | grep -c "[<script>]"` reads 0
- [ ] lock/`-wal` files checked, pid files removed, log kept
- [ ] turn ends with every job either running-and-named or stopped
