#!/usr/bin/env python3
"""Native Mac CI watchdog, installed beside (not inside) the runner checkout.

macOS has no cgroup memory.max. This is a sampled stop limit, not a hard RAM
reservation. A VM is required for a hard 20 GiB boundary. Never touch model or
desktop processes: the only signal target is our own runner process group.
"""
from __future__ import annotations

import os
import re
import signal
import subprocess
import sys
import time

GIB = 1024**3


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


def main() -> int:
    if sys.platform != "darwin" or len(sys.argv) < 2:
        raise SystemExit("Use on macOS with a runner command")
    while True:
        try:
            ready = headroom() >= 40 * GIB and os.getloadavg()[0] < (os.cpu_count() or 1) * 0.8
        except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError):
            ready = False
        if ready:
            break
        print("Aura runner offline: waiting for 40 GiB headroom and CPU capacity", flush=True)
        time.sleep(15)
    runner = subprocess.Popen(sys.argv[1:], start_new_session=True)

    def stop(*_):
        try:
            os.killpg(runner.pid, signal.SIGTERM)
            runner.wait(timeout=10)
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
                safe = headroom() >= 20 * GIB and group_rss(runner.pid) <= 20 * GIB
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
