"""The background job helper: pid/log/exit files, status vocabulary, stopping.

These tests run real child processes, because the thing being tested is the
lifecycle -- a pid that belongs to the command, an exit code written when it
ends, and a stop that leaves nothing behind.  They use a short interpreter
command and a private ``--logs-dir``, so they never touch ``logs/`` and never
depend on a pipeline database.
"""
from __future__ import annotations

import importlib.util
import json
import os
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def bg():
    spec = importlib.util.spec_from_file_location("bg_cli", ROOT / "scripts" / "bg.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def wait_for(path: Path, timeout: float = 25.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if path.exists():
            return True
        time.sleep(0.05)
    return False


def test_start_records_pid_log_and_exit(bg, tmp_path):
    """The pid is the command's own, the log holds its output, the exit code lands."""
    rc = bg.main(["--logs-dir", str(tmp_path), "start", "hello", "--",
                  sys.executable, "-u", "-c", "print('marker-ok')"])
    assert rc == 0

    assert wait_for(tmp_path / "hello.exit"), "the supervisor never wrote an exit code"
    assert (tmp_path / "hello.exit").read_text().strip() == "0"
    assert "marker-ok" in (tmp_path / "hello.log").read_text(encoding="utf-8")
    assert (tmp_path / "hello.pid").read_text().strip().isdigit()

    meta = json.loads((tmp_path / "hello.meta.json").read_text())
    # the `--` split is what keeps the helper's own flags out of the job's argv
    assert meta["command"][0] == sys.executable
    assert "print('marker-ok')" in meta["command"]


def test_status_speaks_one_vocabulary(bg, tmp_path, capsys):
    bg.main(["--logs-dir", str(tmp_path), "start", "quick", "--",
             sys.executable, "-u", "-c", "print('x')"])
    assert wait_for(tmp_path / "quick.exit")

    capsys.readouterr()
    assert bg.main(["--logs-dir", str(tmp_path), "status", "quick", "--json"]) == 0
    info = json.loads(capsys.readouterr().out)
    assert info["state"] == "exited"
    assert info["exit"] == 0
    assert info["log_bytes"] > 0


def test_a_pid_without_an_exit_code_is_lost(bg, tmp_path, capsys):
    """A job killed from outside has no exit code; the helper must not call it absent."""
    (tmp_path / "ghost.pid").write_text("999999\n")
    assert bg.main(["--logs-dir", str(tmp_path), "status", "ghost", "--json"]) == 0
    info = json.loads(capsys.readouterr().out)
    assert info["state"] == "lost"


def test_never_started_is_absent(bg, tmp_path, capsys):
    (tmp_path / "fresh.meta.json").write_text("{}")
    assert bg.main(["--logs-dir", str(tmp_path), "status", "fresh", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["state"] == "absent"


def test_stop_ends_the_job_and_removes_the_pid(bg, tmp_path, capsys):
    command = ["-u", "-c", "import time; print('up', flush=True); time.sleep(120)"]
    assert bg.main(["--logs-dir", str(tmp_path), "start", "sleeper", "--",
                    sys.executable] + command) == 0
    capsys.readouterr()

    assert bg.main(["--logs-dir", str(tmp_path), "stop", "sleeper"]) == 0
    assert not (tmp_path / "sleeper.pid").exists()
    assert wait_for(tmp_path / "sleeper.exit")
    assert (tmp_path / "sleeper.exit").read_text().strip() != "0"

    # and a second stop is honest about it, not a crash
    assert bg.main(["--logs-dir", str(tmp_path), "stop", "sleeper"]) == 0


def test_a_second_start_of_the_same_name_is_refused(bg, tmp_path, capsys):
    command = ["-u", "-c", "import time; time.sleep(120)"]
    assert bg.main(["--logs-dir", str(tmp_path), "start", "busy", "--",
                    sys.executable] + command) == 0
    capsys.readouterr()
    try:
        assert bg.main(["--logs-dir", str(tmp_path), "start", "busy", "--",
                        sys.executable] + command) == 2
        assert "already running" in capsys.readouterr().err
    finally:
        bg.main(["--logs-dir", str(tmp_path), "stop", "busy"])
        wait_for(tmp_path / "busy.exit")


def test_names_are_file_names(bg):
    with pytest.raises(SystemExit):
        bg.safe_name("../escape")
    with pytest.raises(SystemExit):
        bg.safe_name("has space")
    assert bg.safe_name("tsm-fetch_2") == "tsm-fetch_2"


def test_tail_reads_the_end_of_the_log(bg, tmp_path):
    path = tmp_path / "many.log"
    path.write_text("".join(f"line {i}\n" for i in range(500)), encoding="utf-8")
    text = bg.tail(path, 3)
    assert text.splitlines() == ["line 497", "line 498", "line 499"]


@pytest.mark.skipif(os.name != "nt", reason="taskkill is the Windows stopper")
def test_windows_liveness_does_not_terminate_the_probe(bg):
    """``os.kill(pid, 0)`` kills on Windows; the probe must only look."""
    import subprocess

    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        assert bg.pid_alive(child.pid) is True
        assert child.poll() is None, "the liveness check terminated the process"
    finally:
        child.kill()
        child.wait()
