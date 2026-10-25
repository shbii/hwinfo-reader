#!/usr/bin/env python3
"""Record which programs are using the CPU, GPU, RAM and disk while HWiNFO logs sensors.

Run it alongside HWiNFO logging; HWiNFO Log Reader then shows the busiest programs at every
warning, spike and throttling event.

    pip install psutil
    python process_logger.py                    # writes processes_<date>_<time>.csv in the current folder
    python process_logger.py --out "C:\\logs"    # somewhere else (keep it next to the HWiNFO logs)
    python process_logger.py --interval 1 --top 20

Stop with Ctrl+C. Per-program GPU usage uses Windows' "GPU Engine" performance counters
(Windows 10 1709+); on other systems that column is left empty.
"""

import argparse
import csv
import os
import sys
import time
from datetime import datetime

try:
    import psutil
except ImportError:
    sys.exit("process_logger needs psutil:  pip install psutil")

HEADER = ["Timestamp", "PID", "Name", "CPU [%]", "RAM [MB]", "Disk Read [MB/s]", "Disk Write [MB/s]", "GPU [%]"]
SKIP = {0}  # System Idle Process


class GpuCounters:
    """Per-process GPU usage from the Windows 'GPU Engine' performance counters (what Task Manager shows)."""

    PDH_FMT_DOUBLE = 0x00000200
    PDH_MORE_DATA = 0x800007D2

    def __init__(self):
        import ctypes
        from ctypes import wintypes

        class Value(ctypes.Structure):
            _fields_ = [("CStatus", wintypes.DWORD), ("doubleValue", ctypes.c_double)]

        class Item(ctypes.Structure):
            _fields_ = [("szName", ctypes.c_wchar_p), ("FmtValue", Value)]

        self.ctypes, self.Item = ctypes, Item
        self.pdh = ctypes.WinDLL("pdh")
        self.query, self.counter = wintypes.HANDLE(), wintypes.HANDLE()
        if self.pdh.PdhOpenQueryW(None, None, ctypes.byref(self.query)):
            raise OSError("PdhOpenQuery failed")
        if self.pdh.PdhAddEnglishCounterW(self.query, r"\GPU Engine(*)\Utilization Percentage", None,
                                          ctypes.byref(self.counter)):
            raise OSError("GPU Engine counters not available")
        self.pdh.PdhCollectQueryData(self.query)

    def sample(self):
        """{pid: GPU %} - busiest engine type per process, like Task Manager's GPU column."""
        ctypes = self.ctypes
        if self.pdh.PdhCollectQueryData(self.query):
            return {}
        size, count = ctypes.c_ulong(0), ctypes.c_ulong(0)
        status = self.pdh.PdhGetFormattedCounterArrayW(self.counter, self.PDH_FMT_DOUBLE, ctypes.byref(size),
                                                       ctypes.byref(count), None)
        if status & 0xFFFFFFFF != self.PDH_MORE_DATA or not size.value:
            return {}
        buf = ctypes.create_string_buffer(size.value)
        if self.pdh.PdhGetFormattedCounterArrayW(self.counter, self.PDH_FMT_DOUBLE, ctypes.byref(size),
                                                 ctypes.byref(count), buf):
            return {}
        items = ctypes.cast(buf, ctypes.POINTER(self.Item * count.value)).contents
        per_engine = {}
        for item in items:
            if item.FmtValue.CStatus or not item.szName:
                continue
            parts = item.szName.split("_")  # pid_1234_luid_..._eng_0_engtype_3D
            try:
                pid = int(parts[1])
            except (IndexError, ValueError):
                continue
            engine = item.szName.rsplit("engtype_", 1)[-1]
            key = (pid, engine)
            per_engine[key] = per_engine.get(key, 0.0) + item.FmtValue.doubleValue
        out = {}
        for (pid, _), value in per_engine.items():
            out[pid] = min(100.0, max(out.get(pid, 0.0), value))
        return out


def snapshot(prev_io, interval, cores):
    """[(pid, name, cpu %, ram MB, read MB/s, write MB/s)] for every process; updates prev_io."""
    rows, seen = [], set()
    for p in psutil.process_iter(["name"]):
        pid = p.pid
        if pid in SKIP:
            continue
        seen.add(pid)
        try:
            cpu = p.cpu_percent(None) / cores        # share of the whole CPU, like Task Manager
            ram = p.memory_info().rss / 2 ** 20
            read = write = 0.0
            try:
                io = p.io_counters()
                if pid in prev_io:
                    read = max(0, io.read_bytes - prev_io[pid][0]) / 2 ** 20 / interval
                    write = max(0, io.write_bytes - prev_io[pid][1]) / 2 ** 20 / interval
                prev_io[pid] = (io.read_bytes, io.write_bytes)
            except (psutil.AccessDenied, AttributeError):
                pass
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            continue
        rows.append((pid, p.info["name"] or f"pid {pid}", cpu, ram, read, write))
    for pid in list(prev_io):
        if pid not in seen:
            del prev_io[pid]
    return rows


def busiest(rows, gpu, top):
    """The top processes by CPU, GPU, disk and RAM - a program busy in any one of them is kept."""
    keep = set()
    for key in (lambda r: r[2], lambda r: gpu.get(r[0], 0.0), lambda r: r[4] + r[5], lambda r: r[3]):
        keep.update(r[0] for r in sorted(rows, key=key, reverse=True)[:top])
    return [r for r in rows if r[0] in keep]


def main(argv=None):
    p = argparse.ArgumentParser(description="Log per-program CPU/GPU/RAM/disk use next to HWiNFO logs.")
    p.add_argument("--out", default=".", help="folder for the CSV (default: current folder)")
    p.add_argument("--interval", type=float, default=2.0, help="seconds between samples (default 2, like HWiNFO)")
    p.add_argument("--top", type=int, default=15, help="programs kept per sample for each of CPU/GPU/disk/RAM")
    p.add_argument("--duration", type=float, default=0, help="stop after this many seconds (default: run until Ctrl+C)")
    args = p.parse_args(argv)

    os.makedirs(args.out, exist_ok=True)
    path = os.path.join(args.out, f"processes_{datetime.now():%Y-%m-%d_%H-%M-%S}.csv")
    try:
        gpu_counters = GpuCounters() if os.name == "nt" else None
    except (OSError, AttributeError) as e:
        print(f"Per-program GPU usage unavailable ({e}); logging CPU/RAM/disk only.")
        gpu_counters = None

    cores = psutil.cpu_count() or 1
    prev_io = {}
    snapshot(prev_io, args.interval, cores)  # first call primes cpu_percent and disk counters
    print(f"Logging programs every {args.interval:g} s to {path}  (Ctrl+C to stop)")
    started = time.monotonic()
    samples = 0
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(HEADER)
        next_tick = time.monotonic() + args.interval
        try:
            while not args.duration or time.monotonic() - started < args.duration:
                time.sleep(max(0.0, next_tick - time.monotonic()))
                next_tick += args.interval
                now = datetime.now().isoformat(sep=" ", timespec="milliseconds")
                gpu = gpu_counters.sample() if gpu_counters else {}
                for pid, name, cpu, ram, read, write in busiest(snapshot(prev_io, args.interval, cores), gpu, args.top):
                    g = gpu.get(pid)
                    w.writerow([now, pid, name, f"{cpu:.1f}", f"{ram:.0f}", f"{read:.2f}", f"{write:.2f}",
                                "" if g is None and not gpu_counters else f"{g or 0:.1f}"])
                f.flush()
                samples += 1
        except KeyboardInterrupt:
            pass
    print(f"Stopped after {samples} samples. Open the HWiNFO log in HWiNFO Log Reader - it finds {os.path.basename(path)} "
          f"automatically when it's in the same folder.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
