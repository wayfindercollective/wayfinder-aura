"""CPU, GPU and RAM names for the Settings hardware readouts.

The Advanced and Basic Settings panes and the benchmark all show the same
three lines, so they all read them from here.
"""

from __future__ import annotations

import sys


def get_system_info() -> dict[str, str]:
    """Detect system hardware: CPU, GPU, RAM."""
    info = {"cpu": "Unknown", "gpu": "Unknown", "ram": "Unknown"}

    if sys.platform == "darwin":
        # macOS: use sysctl and system_profiler
        import subprocess
        try:
            # CPU name + core count
            result = subprocess.run(
                ["sysctl", "-n", "machdep.cpu.brand_string"],
                capture_output=True, text=True, timeout=5,
            )
            cpu_name = result.stdout.strip() if result.returncode == 0 else ""
            cpu_cores = ""
            try:
                r = subprocess.run(["sysctl", "-n", "hw.ncpu"], capture_output=True, text=True, timeout=5)
                total = r.stdout.strip()
                # Get P/E core breakdown on Apple Silicon
                rp = subprocess.run(["sysctl", "-n", "hw.perflevel0.logicalcpu"], capture_output=True, text=True, timeout=5)
                re = subprocess.run(["sysctl", "-n", "hw.perflevel1.logicalcpu"], capture_output=True, text=True, timeout=5)
                if rp.returncode == 0 and re.returncode == 0:
                    cpu_cores = f" ({rp.stdout.strip()}P+{re.stdout.strip()}E cores)"
                elif total:
                    cpu_cores = f" ({total} cores)"
            except Exception:
                pass
            if cpu_name:
                info["cpu"] = f"{cpu_name}{cpu_cores}"
        except Exception:
            pass

        try:
            # GPU: Apple Silicon has unified GPU — get core count from system_profiler
            result = subprocess.run(
                ["system_profiler", "SPDisplaysDataType"],
                capture_output=True, text=True, timeout=10,
            )
            gpu_chip = ""
            gpu_cores = ""
            metal_ver = ""
            for line in result.stdout.split("\n"):
                stripped = line.strip()
                if "Chipset Model" in stripped or "Chip Model" in stripped:
                    gpu_chip = stripped.split(":")[-1].strip()
                elif "Total Number of Cores" in stripped:
                    gpu_cores = stripped.split(":")[-1].strip()
                elif "Metal Support" in stripped:
                    metal_ver = stripped.split(":")[-1].strip()
            if gpu_chip:
                parts = [gpu_chip]
                if gpu_cores:
                    parts.append(f"{gpu_cores} cores")
                if metal_ver:
                    parts.append(metal_ver)
                info["gpu"] = " · ".join(parts)
        except Exception:
            pass

        try:
            result = subprocess.run(
                ["sysctl", "-n", "hw.memsize"],
                capture_output=True, text=True, timeout=5,
            )
            if result.returncode == 0:
                mem_bytes = int(result.stdout.strip())
                info["ram"] = f"{mem_bytes / (1024**3):.0f} GB"
        except Exception:
            pass
    elif sys.platform == "win32":
        # Windows: registry + kernel32 (there is no /proc or sysctl).
        try:
            from wayfinder.utils.windows_sysinfo import system_info
            info.update(system_info())
        except Exception:
            pass
    else:
        # Linux: /proc filesystem
        try:
            with open("/proc/cpuinfo") as f:
                for line in f:
                    if "model name" in line:
                        info["cpu"] = line.split(":")[1].strip()
                        break
        except Exception:
            pass

        try:
            import subprocess
            result = subprocess.run(["lspci"], capture_output=True, text=True, timeout=5)
            for line in result.stdout.split("\n"):
                if "VGA" in line or "3D controller" in line:
                    if ":" in line:
                        gpu_part = line.split(":")[-1].strip()
                        for prefix in ["Advanced Micro Devices, Inc. ", "NVIDIA Corporation ", "Intel Corporation "]:
                            gpu_part = gpu_part.replace(prefix, "")
                        info["gpu"] = gpu_part[:60]
                    break
        except Exception:
            pass

        try:
            with open("/proc/meminfo") as f:
                for line in f:
                    if "MemTotal" in line:
                        mem_kb = int(line.split()[1])
                        info["ram"] = f"{mem_kb / 1024 / 1024:.0f} GB"
                        break
        except Exception:
            pass

    return info
