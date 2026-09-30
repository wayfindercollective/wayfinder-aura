#!/usr/bin/env python3
"""Native Mac CI watchdog, installed beside (not inside) the runner checkout.

macOS has no cgroup memory.max. This is a sampled stop limit, not a hard RAM
reservation. Model peak, server growth and a cloud agent take priority. Never touch model or
desktop processes: the only signal target is our own runner process group.
"""
from __future__ import annotations

import os
import json
import math
import re
import signal
import subprocess
import sys
import time
import urllib.request

GIB = 1024**3
CI_BUDGET = 4 * GIB
# Additional headroom beyond current server/process RSS: one cloud coding
# agent, server growth, and an OS safety margin. Never borrow model headroom.
PROTECTED_HEADROOM = 24 * GIB


def headroom() -> int:
    raw = subprocess.check_output(["/usr/bin/vm_stat"], text=True, timeout=5)
    page = int(re.search(r"page size of (\d+) bytes", raw)[1])
    pages = {k: int(v) for k, v in re.findall(r"^(.+?):\s+(\d+)\.", raw, re.M)}
    total = int(subprocess.check_output(["/usr/sbin/sysctl", "-n", "hw.memsize"], timeout=5))
    # Same accounting as the existing Mac memory-monitor/process-guardian.
    return total - (pages["Anonymous pages"] + pages["Pages wired down"]
                    + pages["Pages occupied by compressor"] - pages["Pages purgeable"]) * page


def group_rss(group: int) -> int:
    raw = subprocess.check_output(["/bin/ps", "-axo", "pgid=,rss="], text=True, timeout=5)
    return sum(int(row.split()[1]) * 1024 for row in raw.splitlines()
               if len(row.split()) == 2 and int(row.split()[0]) == group)


def spare_after_reserves(available: int, model_used: int, model_max: int) -> int:
    if any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v)
           for v in (available, model_used, model_max)) or not 0 <= model_used <= model_max or model_max <= 0:
        raise ValueError("Invalid model capacity telemetry")
    return available - (model_max - model_used) - PROTECTED_HEADROOM


def ci_headroom() -> int:
    with urllib.request.urlopen("http://127.0.0.1:1236/api/status", timeout=5) as response:
        status = json.load(response)
    return spare_after_reserves(headroom(), status["model_memory_used"], status["model_memory_max"])


def main() -> int:
    if sys.platform != "darwin" or len(sys.argv) < 2:
        raise SystemExit("Use on macOS with a runner command")
    while True:
        try:
            ready = ci_headroom() >= CI_BUDGET and os.getloadavg()[0] < (os.cpu_count() or 1) * 0.8
        except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError):
            ready = False
        if ready:
            break
        print("Aura runner offline: model peak, server and agent reserves leave insufficient CI capacity", flush=True)
        time.sleep(15)
    runner = subprocess.Popen(sys.argv[1:], start_new_session=True)

    def stop(*_):
        try:
            os.killpg(runner.pid, signal.SIGTERM)
            runner.wait(timeout=10)
        except ProcessLookupError:
            pass
        except subprocess.TimeoutExpired:
            pass
        finally:
            try:
                os.killpg(runner.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass

    signal.signal(signal.SIGTERM, lambda *_: (stop(), sys.exit(143)))
    signal.signal(signal.SIGINT, lambda *_: (stop(), sys.exit(130)))
    try:
        while runner.poll() is None:
            try:
                # Existing CI RSS is already deducted from host headroom.
                rss = group_rss(runner.pid)
                safe = rss <= CI_BUDGET and ci_headroom() >= CI_BUDGET - rss
            except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError):
                safe = False
            if not safe:
                print("Aura CI stopped: memory budget exceeded or telemetry unavailable", flush=True)
                stop()
                return 75
            time.sleep(2)
        return runner.returncode
    finally:
        stop()


if __name__ == "__main__":
    raise SystemExit(main())
