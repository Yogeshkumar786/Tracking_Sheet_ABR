"""
run_all.py
──────────────────────────────────────────────────────────────────────────────
Unified runner — FMS Smart + CTYF / GTrophy → same Google Sheets.

Execution order (fastest safe approach):
  STEP 1  CTYF  — fast (~25s). Writes ctyf_vehicles.json so FMS knows
                  which rows are CTYF-managed and must never be blanked.
  STEP 2  FMS   — reads ctyf_vehicles.json, writes trip data for all vehicles.

Why not fully parallel?
  Both scripts write to the same Google Sheets service account (shared API quota:
  60 writes/min). Running simultaneously would cause 429 rate-limit errors.
  The fetch phases are independent, but since each script is a self-contained
  subprocess, we can't split fetch from write without refactoring both.

Usage:
    python run_all.py                        # single run
    python run_all.py --loop                 # repeat every 20 minutes
    python run_all.py --loop --interval 600  # repeat every 10 minutes
"""

import argparse
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

# Reconfigure stdout to support unicode prints on Windows console without crashes
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

HERE = Path(__file__).parent


def _run(script: str) -> int:
    """Run a Python script in the same interpreter. Returns exit code."""
    result = subprocess.run(
        [sys.executable, str(HERE / script)],
        check=False,
    )
    if result.returncode != 0:
        print(f"  [WARN] {script} exited with code {result.returncode}", flush=True)
    return result.returncode


def run_once():
    ts = datetime.now().strftime("%d-%b-%Y %H:%M:%S")
    print(f"\n{'='*62}")
    print(f"  ALL TRACKERS  |  {ts}")
    print(f"{'='*62}")

    # STEP 1: CTYF — runs first so ctyf_vehicles.json exists before FMS
    # needs it (FMS reads it during stranger-removal for hub sheets).
    print("\n>>> STEP 1: CTYF / GTrophy Tracker")
    t0 = time.time()
    _run("ctyf_to_sheets.py")
    ctyf_elapsed = time.time() - t0
    print(f"  CTYF done in {ctyf_elapsed:.0f}s", flush=True)

    # STEP 2: FMS — reads ctyf_vehicles.json (now guaranteed to exist).
    print("\n>>> STEP 2: FMS Smart Tracker")
    t0 = time.time()
    _run("fms_to_sheets.py")
    fms_elapsed = time.time() - t0
    print(f"  FMS done in {fms_elapsed:.0f}s", flush=True)

    total = ctyf_elapsed + fms_elapsed
    print(f"\n[OK] All done in {total:.0f}s  |  {datetime.now().strftime('%H:%M:%S')}")


def main():
    parser = argparse.ArgumentParser(description="Run FMS + CTYF trackers together")
    parser.add_argument("--loop",     action="store_true",
                        help="Run continuously on a schedule")
    parser.add_argument("--interval", type=int, default=1200,
                        help="Seconds between runs in loop mode (default: 1200 = 20 min)")
    args = parser.parse_args()

    if args.loop:
        mins = args.interval // 60
        secs = args.interval % 60
        print(f"Loop mode ON - interval: {args.interval}s ({mins}m {secs}s)")
        while True:
            try:
                run_once()
            except KeyboardInterrupt:
                print("\nStopped by user.")
                break
            except Exception as exc:
                print(f"\n[ERROR] {exc}")
            print(f"\n  Next run in {args.interval}s ... (Ctrl+C to stop)", flush=True)
            time.sleep(args.interval)
    else:
        run_once()


if __name__ == "__main__":
    main()
