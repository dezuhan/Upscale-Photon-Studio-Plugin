"""System report: OS, CPU, memory. GPU data comes from the upscaler (Vulkan devices).

Every probe is defensive: on an unusual machine a missing value becomes "unknown"
instead of breaking the service.
"""

from __future__ import annotations

import ctypes
import os
import platform
import subprocess
import sys


def _windows_build() -> str:
    try:
        return platform.version()
    except Exception:
        return ""


def os_info() -> dict:
    name = platform.system()
    release = platform.release()
    version = platform.version()
    if name == "Windows":
        try:
            build = int(version.split(".")[-1])
        except (ValueError, IndexError):
            build = 0
        label = f"Windows {11 if build >= 22000 else 10}"
        pretty = f"{label} (build {build})" if build else label
    elif name == "Darwin":
        pretty = f"macOS {platform.mac_ver()[0] or release}"
    else:
        pretty = f"{name} {release}"
    return {
        "name": name,
        "pretty": pretty,
        "release": release,
        "version": version,
        "arch": platform.machine(),
    }


def cpu_name() -> str:
    try:
        if os.name == "nt":
            import winreg

            with winreg.OpenKey(
                winreg.HKEY_LOCAL_MACHINE,
                r"HARDWARE\DESCRIPTION\System\CentralProcessor\0",
            ) as key:
                return str(winreg.QueryValueEx(key, "ProcessorNameString")[0]).strip()
        if sys.platform == "darwin":
            out = subprocess.run(
                ["sysctl", "-n", "machdep.cpu.brand_string"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            return out.stdout.strip() or platform.processor() or "unknown"
        with open("/proc/cpuinfo", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                if line.lower().startswith("model name"):
                    return line.split(":", 1)[1].strip()
    except Exception:
        pass
    return platform.processor() or "unknown"


def memory_gb() -> float | None:
    try:
        if os.name == "nt":
            class MemoryStatus(ctypes.Structure):
                _fields_ = [
                    ("dwLength", ctypes.c_ulong),
                    ("dwMemoryLoad", ctypes.c_ulong),
                    ("ullTotalPhys", ctypes.c_ulonglong),
                    ("ullAvailPhys", ctypes.c_ulonglong),
                    ("ullTotalPageFile", ctypes.c_ulonglong),
                    ("ullAvailPageFile", ctypes.c_ulonglong),
                    ("ullTotalVirtual", ctypes.c_ulonglong),
                    ("ullAvailVirtual", ctypes.c_ulonglong),
                    ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
                ]

            status = MemoryStatus()
            status.dwLength = ctypes.sizeof(MemoryStatus)
            if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
                return round(status.ullTotalPhys / (1024**3), 1)
            return None
        if sys.platform == "darwin":
            out = subprocess.run(
                ["sysctl", "-n", "hw.memsize"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            return round(int(out.stdout.strip()) / (1024**3), 1)
        with open("/proc/meminfo", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                if line.startswith("MemTotal:"):
                    return round(int(line.split()[1]) / (1024**2), 1)
    except Exception:
        pass
    return None


def report() -> dict:
    return {
        "os": os_info(),
        "cpu": {"name": cpu_name(), "cores": os.cpu_count() or 0},
        "memoryGb": memory_gb(),
        "python": sys.version.split()[0],
        "executable": sys.executable,
    }
