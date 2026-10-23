import io
import sys
import traceback

sys.path.insert(0, r"C:\FarmAnalysis")
import scripts.intel_addon_harness as h

try:
    rc = h.main()
    print("rc =", rc)
except SystemExit:
    raise
except Exception:
    traceback.print_exc()
