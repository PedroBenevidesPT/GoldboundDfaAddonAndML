import io
import sys

p = r"C:\FarmAnalysis\scripts\intel_addon_harness.py"
s = io.open(p, encoding="utf-8").read()
s = s.replace(
    '        print(f"EVENT FAILED: {exc}", file=sys.stderr)',
    '        import traceback; traceback.print_exc()')
s = s.replace(
    '        print(f"LOAD FAILED: {exc}", file=sys.stderr)',
    '        import traceback; traceback.print_exc()')
with io.open(p, "w", encoding="utf-8", newline="") as fh:
    fh.write(s)
print("patched")
