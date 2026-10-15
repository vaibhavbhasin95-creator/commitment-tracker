"""
Runs every test script in this folder and prints a summary.

    python src/tests/run_all.py            # all suites
    python src/tests/run_all.py parsing    # only files whose name contains "parsing"

Every suite uses its own temporary database and fake external services, so it
is safe to run at any time — it never touches commitment_tracker.db, your
Google Sheet, or sends real messages. Exit code is non-zero if anything fails.
"""

import re
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
NOISE = re.compile(r"oneDNN|absl|tensorflow|cpu_feature|rebuild TensorFlow|DeprecationWarning|utcnow|isoformat\(timespec|Loading weights|HF_TOKEN|To enable")


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="replace")  # a suite's output may hold characters the console can't show
    pattern = sys.argv[1] if len(sys.argv) > 1 else ""
    suites = sorted(p for p in HERE.glob("test_*.py") if pattern in p.name)
    totals, failed_suites = {"PASS": 0, "FAIL": 0}, []
    for suite in suites:
        start = time.time()
        proc = subprocess.run([sys.executable, str(suite)], cwd=HERE, capture_output=True, text=True,
                              encoding="utf-8", errors="replace")
        lines = [l for l in (proc.stdout + proc.stderr).splitlines() if l.strip() and not NOISE.search(l)]
        passed = sum(l.startswith("PASS") for l in lines)
        failed = [l for l in lines if l.startswith("FAIL")]
        crashed = proc.returncode != 0 and not failed
        totals["PASS"] += passed
        totals["FAIL"] += len(failed) + (1 if crashed else 0)
        status = "ok" if proc.returncode == 0 else "FAILED"
        print(f"{suite.name:32s} {passed:3d} passed  {len(failed):2d} failed  {time.time() - start:5.1f}s  {status}")
        for l in failed:
            print("    " + l)
        if crashed:
            print("    crashed:\n      " + "\n      ".join(lines[-8:]))
        if proc.returncode != 0:
            failed_suites.append(suite.name)
    print(f"\nTOTAL {totals['PASS']} passed, {totals['FAIL']} failed")
    sys.exit(1 if failed_suites else 0)


if __name__ == "__main__":
    main()
