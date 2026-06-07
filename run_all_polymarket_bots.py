#!/usr/bin/env python3
import json
import os
import signal
import subprocess
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent
AGGREGATED_FILENAME = ROOT / "aggregated_bot_results.json"

SCRIPTS = [
    {
        "name": "polymarket_btc_bot_v4",
        "path": ROOT / "polymarket_btc_bot_v4.py",
        "args": [],
        "output": ROOT / "bot_session_v4.json",
    },
    {
        "name": "polymarket_btc_bot_v5",
        "path": ROOT / "polymarket_btc_bot_v5.py",
        "args": ["--paper"],
        "output": ROOT / "bot_session_v5_paper.json",
    },
    {
        "name": "polymarket_btc_bot_v6",
        "path": ROOT / "polymarket_btc_bot_v6.py",
        "args": ["--paper"],
        "output": ROOT / "bot_v6_paper.json",
    },
]

stop_requested = False
current_proc = None


def load_json(path: Path):
    try:
        with path.open("r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def save_aggregate(runs):
    existing = []
    if AGGREGATED_FILENAME.exists():
        existing = load_json(AGGREGATED_FILENAME) or []
        if not isinstance(existing, list):
            existing = []

    existing.extend(runs)
    with AGGREGATED_FILENAME.open("w", encoding="utf-8") as f:
        json.dump(existing, f, indent=2, ensure_ascii=False)
    print(f"Saved aggregated results to {AGGREGATED_FILENAME}")


def signal_handler(signum, frame):
    global stop_requested, current_proc
    stop_requested = True
    print("\nCtrl+C detected: stopping current script and saving results...")
    if current_proc and current_proc.poll() is None:
        try:
            current_proc.send_signal(signal.SIGINT)
        except Exception:
            pass


def run_script(script, windows):
    global current_proc
    start_time = datetime.now().astimezone().isoformat()
    args = [sys.executable, str(script["path"]), "--windows", str(windows)] + script["args"]
    print(f"\nRunning {script['name']} with: {' '.join(args)}")
    proc = subprocess.Popen(args, cwd=ROOT)
    current_proc = proc
    try:
        proc.wait()
    except KeyboardInterrupt:
        # This is handled by the signal handler
        pass
    finally:
        current_proc = None

    end_time = datetime.now().astimezone().isoformat()
    status = "completed" if proc.returncode == 0 else "error"
    if stop_requested and proc.returncode != 0:
        status = "interrupted"

    payload = {
        "script_name": script["name"],
        "script_path": str(script["path"].name),
        "started_at": start_time,
        "ended_at": end_time,
        "exit_code": proc.returncode,
        "status": status,
        "args": script["args"],
        "output_file": str(script["output"].name),
        "output": None,
    }

    if script["output"].exists():
        payload["output"] = load_json(script["output"])
    else:
        payload["output"] = {
            "note": f"Output file {script['output'].name} not found."
        }

    return payload


def main():
    global stop_requested
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    import argparse

    parser = argparse.ArgumentParser(
        description="Run all Polymarket BTC bot scripts and aggregate their JSON outputs."
    )
    parser.add_argument("--windows", "-w", type=int, default=6,
                        help="Number of windows to pass to each bot script (default: 6)")
    args = parser.parse_args()

    results = []
    for script in SCRIPTS:
        if stop_requested:
            print("Stopping before next script because interrupt was requested.")
            break

        if not script["path"].exists():
            print(f"Script not found: {script['path']}")
            results.append({
                "script_name": script["name"],
                "status": "missing",
                "note": f"{script['path'].name} not found",
            })
            continue

        result = run_script(script, args.windows)
        results.append(result)

    if results:
        save_aggregate(results)
    else:
        print("No results to save.")

    if stop_requested:
        print("Interrupted by user.")
        sys.exit(130)


if __name__ == "__main__":
    main()
