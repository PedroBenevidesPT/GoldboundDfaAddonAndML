#!/usr/bin/env python3
import subprocess, sys
from pathlib import Path

H = Path("scripts/intel_addon_harness.py")
r = subprocess.run([sys.executable, str(H), "DFA_AuctionIntelligence"],
                   capture_output=True, text=True, timeout=180)
print("returncode:", r.returncode)
print("stdout tail:", (r.stdout or "").strip().splitlines()[-3:])
print("stderr tail:", (r.stderr or "").strip().splitlines()[-5:])
sys.exit(r.returncode or 0)
