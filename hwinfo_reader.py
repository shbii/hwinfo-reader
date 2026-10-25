#!/usr/bin/env python3
"""Read HWiNFO sensor logs (CSV) and show a readable health report.

    python hwinfo_reader.py                     open the window with every .csv in the current folder
    python hwinfo_reader.py log21.CSV           open specific logs
    python hwinfo_reader.py --text              print the report in the terminal instead
    python hwinfo_reader.py --export cleaned    terminal report + cleaned UTF-8 CSVs
    python hwinfo_reader.py --text --clean      terminal report with glitched readings repaired

Can also be imported:

    from hwinfo_reader import load_log
    log = load_log("log21.CSV")
    gpu = log.column("GPU Temperature")
    print(gpu.stats["max"], len(gpu.values))

Standard library only - no pandas needed.
"""

import argparse
import bisect
import collections
import csv
import glob
import io
import os
import re
import shutil
import sys
import textwrap
from dataclasses import dataclass
from datetime import datetime, timedelta
from functools import cached_property

# ------------------------------------------------------------------ parsing

UNIT_RE = re.compile(r"^(.*?)\s*\[([^\[\]]*)\]$")
GROUP_RE = re.compile(r"^([^:\[]+?)\s*(?:\[([^\]]*)\])?\s*(?::\s*(.*))?$")


def parse_group(group):
    """'GPU [#0]: NVIDIA GeForce RTX 4070: MSI ...' -> ('GPU', 'NVIDIA GeForce RTX 4070: MSI ...')"""
    m = GROUP_RE.match(group or "")
    if not m:
        return group or "", ""
    name, tag, rest = m.group(1).strip(), m.group(2), m.group(3)
    device = name
    if tag and tag.startswith("#") and tag != "#0":
        device += f" {tag}"
    desc = rest or (tag if tag and not tag.startswith("#") else "")
    return device, desc


def parse_timestamp(date, time, dayfirst=None):
    """HWiNFO writes the PC's locale date (25.10.2026, 10/25/2026, 25/10/2026, 2026-10-25)
    and unpadded times (2:6:37.350), sometimes with AM/PM."""
    parts = re.split(r"[./-]", date.strip())
    a, b, c = (int(p) for p in parts)
    if len(parts[0]) == 4:
        y, mo, d = a, b, c
    elif "/" in date and not dayfirst:
        mo, d, y = a, b, c
    else:
        d, mo, y = a, b, c
    t = time.strip().upper()
    pm, am = t.endswith("PM"), t.endswith("AM")
    h, mi, s = t.rstrip("APM ").split(":")
    h = int(h) % 12 + (12 if pm else 0) if (am or pm) else int(h)
    return datetime(y, mo, d, h, int(mi)) + timedelta(seconds=float(s))


def _dayfirst(dates):
    """For d/m vs m/d dates: day-first if any first number is above 12."""
    for date in dates:
        parts = re.split(r"[./-]", date.strip())
        if "/" in date and len(parts) == 3 and parts[0].isdigit() and int(parts[0]) > 12:
            return True
    return False


def _to_float(text):
    try:
        return float(text.replace(",", "."))
    except ValueError:
        return None


def compute_stats(values):
    v = sorted(x for x in values if x is not None)
    if not v:
        return None
    n = len(v)

    def pct(p):
        return v[min(n - 1, int(round(p / 100 * (n - 1))))]

    s = {"n": n, "min": v[0], "avg": sum(v) / n, "p1": pct(1), "p50": pct(50), "p99": pct(99), "max": v[-1]}
    # One-off readings far above the 99th percentile are almost always sensor glitches
    s["spiky"] = s["p99"] > 0 and s["max"] > 1.5 * s["p99"]
    s["fixed"] = s["max"] - s["min"] <= 1e-9
    return s


@dataclass
class Column:
    name: str       # unique column name, e.g. "GPU Temperature [°C]"
    label: str      # name without unit, e.g. "GPU Temperature"
    unit: str       # "°C", "W", "Yes/No", ...
    group: str      # HWiNFO sensor group, e.g. "CPU [#0]: AMD Ryzen 7 9800X3D"
    device: str     # short device name, e.g. "CPU"
    is_flag: bool   # Yes/No column (values are bools)
    values: list    # floats or bools, None where missing

    @cached_property
    def stats(self):
        if self.is_flag:
            return None
        return compute_stats(self.values)


@dataclass
class Log:
    path: str
    times: list
    columns: list
    groups: list
    skipped_rows: int = 0
    cleaned: dict = None   # {column name: samples fixed} once clean_log() has run
    processes: object = None   # ProcessLog recorded by process_logger.py over the same period, if found

    def column(self, query, unit=None):
        """Find a column by exact name, then exact label, then substring of the label."""
        cols = [c for c in self.columns if unit is None or c.unit == unit]
        for test in (lambda c: c.name == query,
                     lambda c: c.label == query,
                     lambda c: query.lower() in c.label.lower()):
            for c in cols:
                if test(c):
                    return c
        return None

    @cached_property
    def checks(self):
        """Health check per sensor (see check_sensors)."""
        return check_sensors(self)

    @cached_property
    def interval(self):
        diffs = sorted((b - a).total_seconds() for a, b in zip(self.times, self.times[1:]))
        return diffs[len(diffs) // 2] if diffs else 0.0

    @property
    def duration(self):
        return (self.times[-1] - self.times[0]).total_seconds() if self.times else 0.0

    def hardware(self):
        """{device: description} taken from the sensor-group footer row."""
        hw = {}
        for g in self.groups:
            device, desc = parse_group(g)
            if device and desc and (device not in hw or len(desc) < len(hw[device])):
                hw[device] = desc
        return hw


def load_log(path, processes=True):
    """Parse an HWiNFO CSV log. With processes=True, a matching process_logger.py log in the
    same folder is attached as log.processes."""
    log = _load_hwinfo(path)
    if processes:
        log.processes = find_process_log(log)
    return log


def _load_hwinfo(path):
    with open(path, "rb") as f:
        raw = f.read()
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = raw.decode("latin-1")  # HWiNFO normally writes the ANSI code page
    first = text.split("\n", 1)[0]
    delim = ";" if first.count(";") > first.count(",") else ","
    rows = list(csv.reader(io.StringIO(text, newline=""), delimiter=delim))
    if not rows or rows[0][:2] != ["Date", "Time"]:
        raise ValueError("doesn't look like an HWiNFO log (first row should start with Date,Time)")

    header = rows[0]
    width = len(header)
    # Footer: the header repeated, then a row naming each column's sensor group
    end, groups = len(rows), [""] * width
    for i in range(1, len(rows)):
        if rows[i][:2] == ["Date", "Time"]:
            end = i
            if i + 1 < len(rows):
                groups = (rows[i + 1] + [""] * width)[:width]
            break

    needed = max(i for i, h in enumerate(header) if h.strip()) + 1
    body = rows[1:end]
    dayfirst = _dayfirst(r[0] for r in body if r)
    times, data = [], []
    for r in body:
        if len(r) < needed:
            continue  # truncated line, e.g. HWiNFO still writing
        try:
            times.append(parse_timestamp(r[0], r[1], dayfirst))
        except (ValueError, IndexError):
            continue
        data.append(r)
    if not data:
        raise ValueError("no data rows")

    seen, columns = {}, []
    for i in range(2, needed):
        name = header[i].strip()
        if not name:
            continue
        seen[name] = seen.get(name, 0) + 1
        m = UNIT_RE.match(name)
        label, unit = (m.group(1), m.group(2)) if m else (name, "")
        unique = name
        if seen[name] > 1:
            label = f"{label} #{seen[name]}"
            unique = f"{label} [{unit}]" if m else label
        raw_vals = [r[i].strip() for r in data]
        nonblank = {v for v in raw_vals if v}
        is_flag = unit == "Yes/No" or (bool(nonblank) and nonblank <= {"Yes", "No"})
        if is_flag:
            values = [None if not v else v == "Yes" for v in raw_vals]
        else:
            values = [_to_float(v) if v else None for v in raw_vals]
            if unit in ("°F", "ºF"):  # HWiNFO set to Fahrenheit - work in °C so the limits apply
                values = [None if v is None else (v - 32) / 1.8 for v in values]
                unit = "°C"
                unique = f"{label} [°C]"
        device, _ = parse_group(groups[i])
        columns.append(Column(unique, label, unit, groups[i], device, is_flag, values))

    unique_groups = list(dict.fromkeys(g for g in groups[2:needed] if g))
    return Log(path, times, columns, unique_groups, len(body) - len(data))


def find_irregular(log):
    """{column name: sorted sample indices} of readings that are clearly bad.

    Two kinds: HWiNFO misreads (many sensors of one device jumping at the same instant,
    usually to exactly double) and isolated 1-2 sample spikes far above a sensor's normal range.
    """
    bad = {}
    by_device = {}
    for c in log.columns:
        if not c.is_flag:
            by_device.setdefault(c.device, []).append(c)
    n = len(log.times)

    # Misreads: many sensors of one device jump above both neighbours at the same sample, and
    # at least one of them is normally fixed (e.g. FCLK) - a fixed clock can't really spike.
    # Seen in practice as x2, x1.5 and x1.33 readings, so the bar is deliberately low.
    for device, cols in by_device.items():
        jumps = {}
        for c in cols:
            if not c.stats:
                continue
            vals = c.values
            ordered = sorted(v for v in vals if v is not None)
            m = len(ordered)
            # normally fixed = the same value for at least 90% of the log (misreads may be a few % of a short log)
            steady = ordered[int(0.05 * (m - 1))] == ordered[int(0.95 * (m - 1))]
            for i in range(1, n - 1):
                v, a, b = vals[i], vals[i - 1], vals[i + 1]
                if v is None or a is None or b is None:
                    continue
                ref = max(a, b)
                if ref > 0 and v > (1.1 if steady else 1.15) * ref:
                    jumps.setdefault(i, []).append((c, steady))
        for i, hits in jumps.items():
            if len(hits) >= 5 and any(steady for _, steady in hits):
                for c, _ in hits:
                    bad.setdefault(c.name, set()).add(i)

    for c in log.columns:
        idx = spike_indices(c)
        if not idx:
            continue
        p99 = c.stats["p99"]
        hit = set(idx)
        for s, e in flag_runs([i in hit for i in range(n)]):
            if e - s > 1:
                continue  # 3+ samples in a row could be a real event
            before = c.values[s - 1] if s > 0 else None
            after = c.values[e + 1] if e + 1 < n else None
            if all(x is None or x <= p99 for x in (before, after)):
                bad.setdefault(c.name, set()).update(range(s, e + 1))
    return {name: sorted(ix) for name, ix in bad.items()}


def clean_log(log):
    """Copy of the log with irregular readings replaced by a straight line between
    the good samples either side. Returns (cleaned log, {column name: samples fixed})."""
    bad = find_irregular(log)
    columns = []
    for c in log.columns:
        fix = set(bad.get(c.name, ()))
        values = list(c.values)
        if fix:
            good = [i for i, v in enumerate(values) if v is not None and i not in fix]
            for i in sorted(fix):
                k = bisect.bisect_left(good, i)
                left = good[k - 1] if k > 0 else None
                right = good[k] if k < len(good) else None
                if left is not None and right is not None:
                    w = (i - left) / (right - left)
                    values[i] = values[left] + w * (values[right] - values[left])
                elif left is not None or right is not None:
                    values[i] = values[left if left is not None else right]
                else:
                    values[i] = None
        columns.append(Column(c.name, c.label, c.unit, c.group, c.device, c.is_flag, values))
    report = {name: len(ix) for name, ix in bad.items()}
    return Log(log.path, log.times, columns, log.groups, log.skipped_rows, report, log.processes), report


# ------------------------------------------------------------------ process logs (process_logger.py)

Proc = collections.namedtuple("Proc", "name pid cpu ram read write gpu")
PROCESS_FILE_RE = re.compile(r"processes_(\d{4}-\d{2}-\d{2})_(\d{2})-(\d{2})-(\d{2})\.csv$", re.IGNORECASE)


@dataclass
class ProcessLog:
    paths: list
    times: list       # sample timestamps, sorted
    samples: list     # [Proc, ...] for each timestamp

    @cached_property
    def interval(self):
        diffs = sorted((b - a).total_seconds() for a, b in zip(self.times, self.times[1:]))
        return diffs[len(diffs) // 2] if diffs else 2.0

    def at(self, when, metric="activity", n=3):
        """Busiest programs in the sample nearest to `when` (none if no sample within ~2 intervals)."""
        if not self.times:
            return []
        k = bisect.bisect_left(self.times, when)
        near = [i for i in (k - 1, k) if 0 <= i < len(self.times)]
        i = min(near, key=lambda j: abs((self.times[j] - when).total_seconds()))
        if abs((self.times[i] - when).total_seconds()) > max(3.0, 2 * self.interval):
            return []
        key = PROCESS_METRICS[metric]
        return [p for p in sorted(self.samples[i], key=key, reverse=True)[:n] if key(p) > 0.05]


PROCESS_METRICS = {
    "cpu": lambda p: p.cpu,
    "gpu": lambda p: p.gpu or 0.0,
    "ram": lambda p: p.ram,
    "disk": lambda p: p.read + p.write,
    "activity": lambda p: p.cpu + (p.gpu or 0.0) + 2 * (p.read + p.write),
}


def load_process_log(path):
    times, samples = [], []
    with open(path, encoding="utf-8-sig", newline="") as f:
        reader = csv.reader(f)
        next(reader, None)
        for r in reader:
            if len(r) < 8:
                continue  # partly written last line
            try:
                t = datetime.fromisoformat(r[0])
                proc = Proc(r[2], int(r[1]), float(r[3]), float(r[4]), float(r[5]), float(r[6]),
                            float(r[7]) if r[7] else None)
            except ValueError:
                continue
            if not times or t != times[-1]:
                times.append(t)
                samples.append([])
            samples[-1].append(proc)
    return ProcessLog([path], times, samples)


def find_process_log(log, folders=None):
    """Merge every processes_*.csv (from process_logger.py) that overlaps the log's time span."""
    if not log.times:
        return None
    start, end = log.times[0], log.times[-1]
    folders = folders or [os.path.dirname(os.path.abspath(log.path))]
    parts = []
    for folder in folders:
        for path in sorted(glob.glob(os.path.join(folder, "processes_*.csv"))):
            m = PROCESS_FILE_RE.search(os.path.basename(path))
            if m:
                began = datetime.fromisoformat(f"{m.group(1)} {m.group(2)}:{m.group(3)}:{m.group(4)}")
                if began > end:
                    continue
            try:
                p = load_process_log(path)
            except (OSError, UnicodeDecodeError):
                continue
            if p.times and p.times[0] <= end and p.times[-1] >= start:
                parts.append(p)
    if not parts:
        return None
    merged = sorted(((t, s) for p in parts for t, s in zip(p.times, p.samples)), key=lambda ts: ts[0])
    return ProcessLog([x for p in parts for x in p.paths], [t for t, _ in merged], [s for _, s in merged])


def process_metric(col):
    """Which kind of program activity explains this sensor best."""
    l, g = col.label.lower(), col.group.lower()
    if _dev(col, "GPU") or "presentmon" in g or col.unit in ("FPS", "ms"):
        return "gpu"
    if col.unit == "MB" or _has(l, "memory load", "page file", "virtual memory", "physical memory"):
        return "ram"
    if _is_drive(g):
        return "disk"
    if _dev(col, "CPU"):
        return "cpu"
    return "activity"


def describe_procs(procs, metric="activity"):
    """'game.exe (GPU 97%, CPU 12%), obs64.exe (CPU 8%)'"""
    out = []
    for p in procs:
        bits = []
        if p.gpu and p.gpu >= 1:
            bits.append(f"GPU {p.gpu:.0f}%")
        if p.cpu >= 1:
            bits.append(f"CPU {p.cpu:.0f}%")
        if p.read + p.write >= 1:
            bits.append(f"disk {p.read + p.write:.0f} MB/s")
        if metric == "ram" or not bits:
            bits.append(f"RAM {p.ram / 1024:.1f} GB" if p.ram >= 1024 else f"RAM {p.ram:.0f} MB")
        out.append(f"{p.name} ({', '.join(bits)})")
    return ", ".join(out)


def programs_at(log, index, col=None, n=3):
    """Text naming the busiest programs at sample `index` of the log, or '' without a process log."""
    if not log.processes or index is None:
        return ""
    metric = process_metric(col) if col is not None else "activity"
    return describe_procs(log.processes.at(log.times[index], metric, n), metric)


def export_csv(log, path):
    """Cleaned copy: UTF-8, one timestamp column, unique names, flags as 1/0, no footer."""
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["Timestamp"] + [c.name for c in log.columns])
        for i, t in enumerate(log.times):
            row = [t.isoformat(sep=" ", timespec="milliseconds")]
            for c in log.columns:
                v = c.values[i]
                row.append("" if v is None else int(v) if c.is_flag else v)
            w.writerow(row)


# ------------------------------------------------------------------ analysis

THERMAL_WORDS = ("thermal", "prochot", "htc", "critical temperature")
NOT_THROTTLING = ("velocity boost",)   # Intel Thermal Velocity Boost is a boost feature, not a slowdown

LIMIT_NOTES = {
    "reliability voltage": "normal: the GPU is boosting as far as its voltage allows",
    "max operating voltage": "GPU sat at its maximum voltage",
    "power": "GPU hit its power limit and held clocks back",
    "utilization": "GPU was under-used (waiting on the CPU or a frame cap)",
    "sli gpuboost sync": "SLI boost sync",
}

AMD_LIMITS = {
    "CPU PPT Limit": "package power limit (PPT)",
    "CPU TDC Limit": "sustained current limit (TDC)",
    "CPU EDC Limit": "peak current limit (EDC)",
    "Thermal Limit": "temperature limit",
}

KEY_METRICS = [
    ("CPU (Tctl/Tdie)", "°C"), ("CPU Package", "°C"), ("Core Max", "°C"), ("GPU Temperature", "°C"),
    ("GPU Hot Spot Temperature", "°C"), ("GPU Memory Junction Temperature", "°C"), ("GPU Memory Temperature", "°C"),
    ("CPU Package Power", "W"), ("GPU Power", "W"), ("Total Board Power (TBP)", "W"), ("GPU ASIC Power", "W"),
    ("Total CPU Usage", "%"), ("GPU Core Load", "%"), ("GPU Utilization", "%"), ("GPU Clock", "MHz"),
    ("Physical Memory Load", "%"),
]


# ------------------------------------------------------------------ health checks

@dataclass
class Check:
    col: Column
    level: str              # ok / info / warn / bad, or "none" when no limit is known for this sensor
    value: str = ""         # the reading the verdict is based on
    normal: str = ""        # what's normal for this component
    message: str = ""       # explanation when not ok
    line: float = None      # threshold to draw on a chart
    at: int = None          # sample index of the worst reading
    programs: str = ""      # busiest programs at that moment (needs a process_logger.py log)


def _dev(col, kind):
    """True for 'GPU', 'GPU #1', ... when kind is 'GPU'."""
    return col.device == kind or col.device.startswith(kind + " ")


def cpu_vendor(log):
    text = " ".join(g for g in log.groups if g.lower().startswith("cpu")).lower()
    return "intel" if "intel" in text else "amd" if _has(text, "amd", "ryzen", "threadripper", "epyc") else ""


def cpu_tjmax(log):
    """(max safe CPU temperature in °C, where that number comes from) for the CPU in the log."""
    for g in log.groups:
        gl = g.lower()
        if not gl.startswith("cpu"):
            continue
        m = re.search(r"ryzen \d+ (\d{4})x3d", gl)
        if m:
            model = int(m.group(1))
            if model < 6000:
                return 90.0, "Ryzen 5000X3D Tjmax"
            if model < 8000:
                return 89.0, "Ryzen 7000X3D Tjmax"
            return 95.0, "Ryzen X3D Tjmax"
        if _has(gl, "ryzen", "threadripper", "epyc", "amd"):
            return 95.0, "AMD Tjmax"
        if "intel" in gl:
            return 100.0, "Intel Tjmax"
    return 95.0, "typical Tjmax (CPU not identified)"


def gpu_vendor(col):
    g = col.group.lower()
    return "nvidia" if _has(g, "nvidia", "geforce", "rtx", "quadro") else \
        "amd" if _has(g, "radeon", "amd") else "intel" if _has(g, "intel", "arc") else ""


def gpu_core_limits(log, col):
    """(warn, critical) °C for a GPU's main temperature - from the card's own limit when HWiNFO logs it."""
    for c in log.columns:
        if c.device == col.device and c.unit == "°C" and "thermal limit" in c.label.lower() and c.stats:
            lim = c.stats["max"]
            return lim - 4, lim
    return {"nvidia": (83, 88), "amd": (90, 100), "intel": (90, 100)}.get(gpu_vendor(col), (85, 95))


def _band(nominal, warn_pct=5, bad_pct=8):
    return ("band", (nominal, warn_pct, bad_pct))


def _has(text, *words):
    return any(w in text for w in words)


def _is_drive(g):
    return _has(g, "s.m.a.r.t", "drive", "nvme", "ssd", "disk")


def _is_dimm(l, g):
    return _has(g, "dimm", "ddr5", "ddr4", "spd", "pmic") or _has(l, "dimm", "spd hub", "pmic")


def _is_board(c, g):
    """Motherboard sensor chip (Super I/O / embedded controller)."""
    return not _dev(c, "CPU") and not _dev(c, "GPU") and _has(
        g, "ite ", "it86", "it87", "nuvoton", "nct", "fintek", "super i/o", "lpc", "embedded controller", "ec:")


def _cpu_temp(log, c):
    tj, _ = cpu_tjmax(log)
    return tj - 10, tj


def _cpu_vcore(log, c):
    # Intel boosts at much higher voltage than AMD; above ~1.55 V Intel chips risk degradation
    return (1.50, 1.55) if cpu_vendor(log) == "intel" else (1.40, 1.50)


# (matcher(col, label, group) -> bool, (kind, params), what the component is)   first match wins.
# params may be a function (log, col) -> params, for limits that depend on the hardware in the log.
RULES = [
    # temperatures
    (lambda c, l, g: c.unit == "°C" and "limit" in l, ("none", None), "a limit setting, not a temperature"),
    (lambda c, l, g: c.unit == "°C" and _has(l, "hot spot", "hotspot", "junction temp") and _dev(c, "GPU"),
     ("high", lambda log, c: (100, 110) if gpu_vendor(c) == "amd" else (95, 105)), "GPU hot spot"),
    (lambda c, l, g: c.unit == "°C" and _dev(c, "GPU") and _has(l, "memory junction", "memory temp", "vram"),
     ("high", (95, 105)), "GPU memory"),
    (lambda c, l, g: c.unit == "°C" and _dev(c, "GPU") and l in ("gpu temperature", "gpu edge temperature",
                                                                 "gpu core temperature"),
     ("high", gpu_core_limits), "GPU core"),
    (lambda c, l, g: c.unit == "°C" and _dev(c, "GPU"), ("high", (90, 100)), "GPU sensor"),
    (lambda c, l, g: c.unit == "°C" and _has(l, "vrm", "mos"), ("high", (100, 115)), "voltage regulator (VRM)"),
    (lambda c, l, g: c.unit == "°C" and "iod" in l, ("high", (85, 95)), "CPU I/O die"),
    (lambda c, l, g: c.unit == "°C" and "distance to tjmax" in l, ("low", (10, 3)),
     "CPU distance from its max temperature"),
    (lambda c, l, g: c.unit == "°C" and _dev(c, "CPU"), ("high", _cpu_temp), "CPU"),
    (lambda c, l, g: c.unit == "°C" and _is_drive(g) and "2" not in l and "3" not in l, ("high", (70, 80)),
     "SSD (throttles ~80 °C)"),
    (lambda c, l, g: c.unit == "°C" and _is_drive(g), ("high", (80, 90)), "SSD controller"),
    (lambda c, l, g: c.unit == "°C" and _is_dimm(l, g), ("high", (65, 80)), "DDR5 memory stick"),
    (lambda c, l, g: c.unit == "°C" and _has(l, "chipset", "pch"), ("high", (85, 95)), "chipset"),
    (lambda c, l, g: c.unit == "°C" and _is_board(c, g) and "cpu" in l, ("high", _cpu_temp),
     "CPU (motherboard sensor)"),
    (lambda c, l, g: c.unit == "°C" and _is_board(c, g), ("high", (70, 85)), "motherboard area"),
    (lambda c, l, g: c.unit == "°C", ("high", (90, 100)), "component"),
    # voltages
    (lambda c, l, g: c.unit == "V" and _dev(c, "GPU") and _has(l, "input voltage", "12v"), _band(12),
     "GPU 12 V input"),
    (lambda c, l, g: c.unit == "V" and "12v" in l.replace(" ", ""), _band(12), "+12 V rail (ATX ±5%)"),
    (lambda c, l, g: c.unit == "V" and _has(l.replace(" ", ""), "+5v", "5vsb", "5vstandby", "vinbulk"),
     _band(5), "+5 V rail (ATX ±5%)"),
    (lambda c, l, g: c.unit == "V" and _has(l.replace(" ", ""), "+3.3v", "3vsb", "3.3v", "3vstandby", "+3v"),
     _band(3.3), "+3.3 V rail (ATX ±5%)"),
    (lambda c, l, g: c.unit == "V" and _has(l, "vbat", "battery", "cmos"), ("low", (2.9, 2.7)), "CMOS battery"),
    (lambda c, l, g: c.unit == "V" and "1.8v" in l.replace(" ", ""), _band(1.8), "1.8 V supply"),
    (lambda c, l, g: c.unit == "V" and _dev(c, "GPU") and "core" in l,
     ("high", lambda log, c: (1.15, 1.2) if gpu_vendor(c) == "nvidia" else (1.25, 1.3)), "GPU core voltage"),
    (lambda c, l, g: c.unit == "V" and "soc" in l, ("high", (1.30, 1.35)), "CPU SoC voltage (AMD max 1.3 V)"),
    (lambda c, l, g: c.unit == "V" and "misc" in l, ("high", (1.25, 1.35)), "CPU MISC voltage"),
    (lambda c, l, g: c.unit == "V" and _has(l, "vddio", "vddq", "dram", "vdd_mem", "memory", "swa", "swb")
     or c.unit == "V" and _is_dimm(l, g) and not _has(l, "swc", "vpp", "1.0v"), ("high", (1.46, 1.56)),
     "memory (DRAM) voltage"),
    (lambda c, l, g: c.unit == "V" and _has(l, "vpp", "swc"), ("high", (1.95, 2.1)), "DDR5 VPP voltage"),
    (lambda c, l, g: c.unit == "V" and _has(l, "vddcr_vdd", "vcore", "cpu core voltage", "core vid", "cpu core")
     or c.unit == "V" and _dev(c, "CPU") and re.match(r"(p-|e-)?core ?\d+ v", l), ("high", _cpu_vcore),
     "CPU core voltage"),
    # fans
    (lambda c, l, g: c.unit == "RPM", ("fan", None), "fan"),
    # usage
    (lambda c, l, g: c.unit == "%" and l == "physical memory load", ("high", (90, 97)), "RAM"),
    (lambda c, l, g: c.unit == "%" and l == "virtual memory load", ("high", (90, 97)), "RAM + page file"),
    (lambda c, l, g: c.unit == "%" and l == "page file usage", ("high", (80, 95)), "page file"),
    (lambda c, l, g: c.unit == "%" and l == "gpu memory usage", ("high", (95, 99)), "VRAM"),
    (lambda c, l, g: c.unit == "%" and "remaining life" in l, ("low", (20, 10)), "SSD wear"),
    (lambda c, l, g: c.unit == "%" and "available spare" in l, ("low", (20, 10)), "SSD spare blocks"),
    (lambda c, l, g: c.unit == "%" and l in ("cpu ppt limit", "cpu tdc limit", "cpu edc limit", "thermal limit"),
     ("high", (95, 100)), "CPU limit (100% = limit reached)"),
    # errors, flags, links
    (lambda c, l, g: "total errors" in l or "whea" in g or "hardware errors" in g, ("zero", None),
     "Windows hardware errors (WHEA)"),
    (lambda c, l, g: c.is_flag and is_thermal_flag(c), ("never", None), "thermal throttling flag"),
    (lambda c, l, g: c.is_flag and _has(l, "failure", "warning", "error"), ("never", None), "health flag"),
    (lambda c, l, g: c.unit == "GT/s" and _dev(c, "GPU"), ("pcie", 8.0), "GPU PCIe link"),
]


def _check_one(log, c, kind, params, what):
    """Judge one sensor, then note when it was at its worst (for matching up running programs)."""
    ch = _judge(log, c, kind, params, what)
    if ch.level in ("ok", "none"):
        return ch
    present = [i for i, v in enumerate(c.values) if v is not None]
    if not present:
        return ch
    lowest = min(present, key=lambda i: c.values[i])
    highest = max(present, key=lambda i: c.values[i])
    if kind == "low" or (kind == "band" and "dropped" in ch.message):
        ch.at = lowest
    elif kind == "fan":
        ch.at = next((i for i in present if c.values[i] == 0), None)
    elif kind == "never":
        ch.at = next((i for i in present if c.values[i]), None)
    elif kind == "zero":
        first = c.values[present[0]]
        ch.at = next((i for i in present if c.values[i] > first), None)  # errors that happened during the log
    else:
        ch.at = highest
    return ch


def _judge(log, c, kind, params, what):
    n = len(log.times)
    vals = [v for v in c.values if v is not None]
    if not vals:
        return Check(c, "none", "no data")
    u = c.unit
    s = c.stats
    if params is _cpu_temp:
        what = f"{what}, {cpu_tjmax(log)[1]} {cpu_tjmax(log)[0]:.0f} °C"
    if callable(params):
        params = params(log, c)

    def dur(k):
        return fmt_duration(k * log.interval)

    def high(warn, bad):
        over_w = sum(1 for v in vals if v >= warn)
        over_b = sum(1 for v in vals if v >= bad)
        value = f"peak {fmt(s['max'], u)} {u} (p99 {fmt(s['p99'], u)})"
        normal = f"below {fmt(warn, u)} {u}"
        if s["p99"] >= bad or over_b >= 3:
            return Check(c, "bad", value, normal, f"{who(c)} reached {fmt(s['max'], u)} {u}, at or above the "
                         f"critical {fmt(bad, u)} {u} for {dur(over_b)} ({what})", bad)
        if s["p99"] >= warn or over_b or over_w >= 3:
            return Check(c, "warn", value, normal, f"{who(c)} reached {fmt(s['max'], u)} {u}, above the normal "
                         f"{fmt(warn, u)} {u} for {dur(over_w)} ({what})", warn)
        return Check(c, "ok", value, normal, line=warn)

    def low(warn, bad):
        under_w = sum(1 for v in vals if v <= warn)
        under_b = sum(1 for v in vals if v <= bad)
        value = f"lowest {fmt(s['min'], u)} {u} (p1 {fmt(s['p1'], u)})"
        normal = f"above {fmt(warn, u)} {u}"
        if s["p1"] <= bad or under_b >= 3:
            return Check(c, "bad", value, normal, f"{who(c)} dropped to {fmt(s['min'], u)} {u}, at or below the "
                         f"critical {fmt(bad, u)} {u} for {dur(under_b)} ({what})", bad)
        if s["p1"] <= warn or under_b or under_w >= 3:
            return Check(c, "warn", value, normal, f"{who(c)} dropped to {fmt(s['min'], u)} {u}, below the normal "
                         f"{fmt(warn, u)} {u} for {dur(under_w)} ({what})", warn)
        return Check(c, "ok", value, normal, line=warn)

    if kind == "high":
        return high(*params)
    if kind == "low":
        return low(*params)
    if kind == "band":
        nom, wp, bp = params
        lo, hi = low(nom * (1 - wp / 100), nom * (1 - bp / 100)), high(nom * (1 + wp / 100), nom * (1 + bp / 100))
        if s["max"] < nom * 0.5:
            return Check(c, "info", f"{fmt(s['avg'], u)} {u}", f"~{nom} {u}",
                         f"{who(c)} reads {fmt(s['avg'], u)} {u}, far from {nom} {u} - probably not this rail or "
                         f"not connected")
        worst = max((lo, hi), key=lambda k: ("ok", "warn", "bad").index(k.level))
        worst.value = f"{fmt(s['min'], u)}–{fmt(s['max'], u)} {u}"
        worst.normal = f"{fmt(nom * (1 - wp / 100), u)}–{fmt(nom * (1 + wp / 100), u)} {u} (±{wp}%)"
        return worst
    if kind == "fan":
        zero = sum(1 for v in vals if v == 0)
        value = f"{s['min']:,.0f}–{s['max']:,.0f} RPM"
        if s["max"] == 0:
            return Check(c, "info", "0 RPM", "spinning", f"{who(c)} read 0 RPM the whole log - nothing connected "
                                                         f"to that header, or the fan is stopped")
        if not zero or _dev(c, "GPU"):
            return Check(c, "ok", value, "spinning" + (" (0 RPM at idle is normal)" if _dev(c, "GPU") else ""))
        level = "bad" if "cpu" in c.label.lower() or "pump" in c.label.lower() else "warn"
        return Check(c, level, value, "spinning", f"{who(c)} stopped (0 RPM) for {dur(zero)} while it spun the "
                                                  f"rest of the time - check the fan/cable or fan curve")
    if kind == "zero":
        if s["max"] > 0:
            during = s["max"] - s["min"]
            extra = f", {during:.0f} of them during this log" if during else " (all from before this log started)"
            return Check(c, "bad", f"{s['max']:.0f} errors", "0", f"Windows logged {s['max']:.0f} hardware "
                         f"error(s){extra} - usually an unstable overclock, EXPO/memory setting or Curve Optimizer")
        return Check(c, "ok", "0 errors", "0")
    if kind == "never":
        on = sum(1 for v in c.values if v)
        if on:
            return Check(c, "bad", f"active {on / n:.1%} of the time", "never active",
                         f"{who(c)} was active for {dur(on)} ({what})")
        return Check(c, "ok", "never active", "never active")
    if kind == "pcie":
        # Cards drop to 2.5 GT/s at idle; never going above PCIe 2.0 speed (5 GT/s) means a slot/riser problem.
        # (A card's own maximum - 8/16/32 GT/s for PCIe 3/4/5 - isn't in the log, so faster links can't be judged.)
        value = f"max {s['max']:g} GT/s"
        if s["max"] < params:
            return Check(c, "warn", value, f"at least {params:g} GT/s under load",
                         f"{who(c)} never went above {s['max']:g} GT/s (PCIe 1.0/2.0 speed) - check the GPU is in "
                         f"the main x16 slot, the riser cable, or the PCIe setting in BIOS")
        return Check(c, "ok", value, f"at least {params:g} GT/s under load")
    return Check(c, "none")


def check_sensors(log):
    """One Check per sensor, judged on glitch-cleaned readings."""
    clean = log if log.cleaned is not None else clean_log(log)[0]
    out = []
    for c in clean.columns:
        l, g = c.label.lower(), c.group.lower()
        s = c.stats
        if c.unit == "°C" and s and (s["p99"] > 150 or s["p1"] < -40 or (s["fixed"] and s["max"] in (-55, 127, 128, 255))):
            out.append(Check(c, "info", f"{fmt(s['avg'], c.unit)} {c.unit}", "a real temperature",
                             f"{who(c)} reads {fmt(s['avg'], c.unit)} °C - impossible, so that probe isn't connected "
                             f"or the sensor isn't real; ignore it"))
            continue
        for match, (kind, params), what in RULES:
            try:
                hit = match(c, l, g)
            except (TypeError, AttributeError):
                hit = False
            if hit:
                out.append(_check_one(clean, c, kind, params, what))
                break
        else:
            out.append(Check(c, "none"))
    for ch in out:
        if ch.at is not None:
            ch.programs = programs_at(clean, ch.at, ch.col)
    return out


def is_thermal_flag(col):
    label = col.label.lower()
    return col.is_flag and _has(label, *THERMAL_WORDS) and not _has(label, *NOT_THROTTLING)


def flag_runs(values):
    """[(first_index, last_index), ...] for each stretch where the flag is Yes."""
    runs, start = [], None
    for i, v in enumerate(list(values) + [False]):
        if v and start is None:
            start = i
        elif not v and start is not None:
            runs.append((start, i - 1))
            start = None
    return runs


def spike_indices(col):
    s = col.stats
    if not s or not s["spiky"]:
        return []
    cut = 1.5 * s["p99"]
    return [i for i, v in enumerate(col.values) if v is not None and v > cut]


def glitch_moments(log, min_sensors=5):
    """Sample indices where many sensors of one device spike at once - HWiNFO misreads,
    typically every value of the CPU's SMU table doubling for a single sample."""
    hits = {}
    for c in log.columns:
        for i in spike_indices(c):
            hits.setdefault((c.device, i), []).append(c)
    return {key: cols for key, cols in sorted(hits.items(), key=lambda kv: kv[0][1]) if len(cols) >= min_sensors}


def who(col):
    """'GPU Fan1' rather than 'GPU GPU Fan1'."""
    return col.label if col.label.startswith(col.device) else f"{col.device} {col.label}"


def run_seconds(log, run):
    s, e = run
    return (log.times[e] - log.times[s]).total_seconds() + log.interval


MAIN_CPU_TEMPS = ["CPU (Tctl/Tdie)", "CPU Package", "Core Max", "CPU Die (average)", "CPU"]
MAIN_GPU_TEMPS = ["GPU Temperature", "GPU Edge Temperature", "GPU Core Temperature"]


def main_column(log, labels, unit, device=None):
    """First column whose label exactly matches one of labels (in order of preference)."""
    for label in labels:
        for c in log.columns:
            if c.label == label and c.unit == unit and c.stats and (device is None or _dev(c, device)):
                return c
    return None


def temp_limits(log):
    """[(column, limit °C, where the limit comes from)] for the main CPU/GPU temperatures."""
    out = []
    cpu = main_column(log, MAIN_CPU_TEMPS, "°C", "CPU") or main_column(log, MAIN_CPU_TEMPS, "°C")
    if cpu:
        tj, source = cpu_tjmax(log)
        out.append((cpu, tj, source))
    gpu = main_column(log, MAIN_GPU_TEMPS, "°C", "GPU")
    if gpu:
        has_own = any(c.device == gpu.device and "thermal limit" in c.label.lower() for c in log.columns)
        vendor = gpu_vendor(gpu).upper() or "GPU"
        out.append((gpu, gpu_core_limits(log, gpu)[1], "reported by GPU" if has_own else f"typical {vendor} limit"))
    return out


def category(col):
    u = col.unit
    if "fan" in col.label.lower() or u == "RPM":
        return "Fans"
    return {
        "°C": "Temperatures", "W": "Power", "MHz": "Clocks", "%": "Load & usage",
        "V": "Voltages", "A": "Currents", "MB": "Memory", "FPS": "Frame rate",
        "ms": "Frame times", "GB/s": "Bandwidth & bus", "GT/s": "Bandwidth & bus",
    }.get(u, "Other")


CATEGORY_ORDER = ["Temperatures", "Power", "Clocks", "Load & usage", "Fans", "Voltages",
                  "Currents", "Memory", "Frame rate", "Frame times", "Bandwidth & bus", "Other"]


def analyse(log):
    """Plain-language findings: [(level, message)], level in ok / info / warn / bad."""
    found = []
    n = len(log.times)
    flags = [c for c in log.columns if c.is_flag]

    thermal = [c for c in flags if is_thermal_flag(c)]
    hot = [c for c in thermal if any(c.values)]
    if not thermal:
        found.append(("warn", "This log has no throttling flags, so throttling can't be checked directly"))
    elif not hot:
        found.append(("ok", f"No thermal throttling - all {len(thermal)} thermal flags "
                            f"({', '.join(f'{c.device} {short_flag(c)}' for c in thermal)}) read No for the whole log"))
    for c in hot:
        runs = flag_runs(c.values)
        on = sum(1 for v in c.values if v)
        worst = max(runs, key=lambda r: run_seconds(log, r))
        busy = programs_at(log, worst[0], c)
        found.append(("bad", f"THERMAL THROTTLING: {c.device} {short_flag(c)} active {on / n:.1%} of the time, "
                             f"{len(runs)} events, longest {fmt_duration(run_seconds(log, worst))} "
                             f"from {log.times[worst[0]]:%H:%M:%S}" + (f" - busiest programs: {busy}" if busy else "")))

    for c in flags:
        if c in thermal or not any(c.values):
            continue
        on = sum(1 for v in c.values if v)
        key = short_flag(c).lower()
        note = LIMIT_NOTES.get(key, "limit was active")
        found.append(("info", f"{c.device} limited by {short_flag(c)} {on / n:.1%} of the time - {note}"))

    for col, limit, source in temp_limits(log):
        peak = col.stats["max"]
        headroom = limit - peak
        if headroom < 10:
            continue  # too hot - the health checks below report it
        found.append(("ok", f"{col.device} peaked at {peak:.1f} °C ({col.label}), "
                             f"{headroom:.1f} °C below its {limit:.0f} °C limit ({source})"))

    core, spot = log.column("GPU Temperature", "°C"), log.column("GPU Hot Spot Temperature", "°C")
    if core and spot:
        delta = [b - a for a, b in zip(core.values, spot.values) if a is not None and b is not None]
        ds = compute_stats(delta)
        if ds:
            bad = ds["p99"] > 20
            found.append(("warn" if bad else "ok",
                          f"GPU hot spot runs {ds['avg']:.1f} °C above the core on average (p99 {ds['p99']:.1f} °C) - "
                          + ("high, can point to poor paste/mount" if bad else "normal (under 20 °C)")))

    checks = log.checks
    for level in ("bad", "warn"):
        for ch in checks:
            if ch.level == level and not (ch.col.is_flag and is_thermal_flag(ch.col)):  # throttling reported above
                when = f" - at {log.times[ch.at]:%H:%M:%S}" if ch.at is not None else ""
                busy = f", busiest programs: {ch.programs}" if ch.programs else ""
                found.append((level, ch.message + when + busy))
    if log.processes:
        found.append(("info", f"Program activity matched from {', '.join(os.path.basename(p) for p in log.processes.paths)}"
                              f" - warnings, spikes and throttling events show the busiest programs at the time"))
    else:
        found.append(("info", "No process log found - run process_logger.py while HWiNFO logs (save it in the same "
                              "folder) to see which programs were running at each warning or spike"))
    judged = [ch for ch in checks if ch.level != "none"]
    ok = sum(1 for ch in judged if ch.level == "ok")
    found.append(("ok" if ok == len(judged) else "info",
                  f"Health checks: {ok} of {len(judged)} sensors with known limits are in the normal range for their "
                  f"component" + (" - all good" if ok == len(judged) else "")
                  + f"; {len(checks) - len(judged)} have no fixed limit (checked for glitches/spikes only)"))
    for ch in checks:
        if ch.level == "info":
            found.append(("info", ch.message))

    tctl = log.column("CPU (Tctl/Tdie)", "°C")
    for c in log.columns:
        if c.unit != "%" or c.label not in AMD_LIMITS or not c.stats:
            continue
        over = [i for i, v in enumerate(c.values) if v is not None and v >= 100]
        if not over:
            continue
        runs = flag_runs([v is not None and v >= 100 for v in c.values])
        if all(s == e for s, e in runs) and len(over) <= 0.005 * n:
            temp = ""
            if tctl:
                ts = [tctl.values[i] for i in over if tctl.values[i] is not None]
                if ts:
                    temp = f" while the CPU was only ~{sum(ts) / len(ts):.0f} °C"
            found.append(("info", f"CPU {c.label} briefly read {c.stats['max']:.1f}% on {len(over)} isolated "
                                  f"sample(s){temp} - sensor glitch, not a real {AMD_LIMITS[c.label]} hit"))

    for c in log.columns:
        if c.unit == "RPM" and c.stats:
            s = c.stats
            if s["max"] == 0:
                found.append(("info", f"{who(c)} read 0 RPM the whole log (stopped / zero-RPM mode)"))
            elif s["max"] - s["min"] <= 0.02 * s["max"]:
                found.append(("info", f"{who(c)} held ~{s['avg']:,.0f} RPM the whole log - "
                                      f"looks like a fixed speed rather than a fan curve"))

    gaps = [(b - a).total_seconds() for a, b in zip(log.times, log.times[1:])
            if (b - a).total_seconds() > 2.5 * log.interval]
    if gaps:
        found.append(("warn", f"{len(gaps)} gap(s) in logging, longest {fmt_duration(max(gaps))}"))
    if log.skipped_rows:
        found.append(("warn", f"{log.skipped_rows} unreadable row(s) skipped"))

    if log.cleaned:
        fixed = sum(log.cleaned.values())
        found.append(("ok", f"Cleaned: {fixed} glitched/irregular reading(s) across {len(log.cleaned)} sensor(s) "
                            f"were replaced with values interpolated from the samples either side"))

    glitches = glitch_moments(log)
    glitched = set()
    for (device, _), cols in glitches.items():
        glitched.update(c.name for c in cols)
    for device in dict.fromkeys(d for d, _ in glitches):
        moments = [i for d, i in glitches if d == device]
        cols = {c.name: c for (d, _), cs in glitches.items() if d == device for c in cs}
        examples = []
        clock = next((c for c in cols.values() if c.unit == "MHz"), None)
        if clock:
            examples.append(f"{clock.label} {fmt(clock.stats['p50'], 'MHz')} → {fmt(clock.stats['max'], 'MHz')} MHz")
        temps = [c for c in cols.values() if c.unit == "°C"]
        if temps:
            t = max(temps, key=lambda c: c.stats["max"])
            examples.append(f"{t.label} showing {t.stats['max']:.1f} °C")
        when = ", ".join(f"{log.times[i]:%H:%M:%S}" for i in moments[:5]) + (" …" if len(moments) > 5 else "")
        found.append(("info", f"HWiNFO misread the {device} sensors at {len(moments)} moment(s) ({when}): "
                              f"{len(cols)} readings jumped at the same instant"
                              + (f" (e.g. {', '.join(examples)})" if examples else "")
                              + " - a logging glitch, not real; ignore those maxima"))

    others = [c for c in log.columns if c.stats and c.stats["spiky"] and c.name not in glitched]
    if others:
        found.append(("info", f"{len(others)} other sensor(s) have short spikes (mostly "
                              f"{collections.Counter(c.device for c in others).most_common(1)[0][0]}) - "
                              f"see 'Spikes' below; P99 is the more reliable peak for those"))
    return found


def short_flag(col):
    return re.sub(r"^Performance Limit - ", "", col.label)


# ------------------------------------------------------------------ output

class Style:
    enabled = False

    @classmethod
    def apply(cls, code, text):
        return f"\033[{code}m{text}\033[0m" if cls.enabled else text


def bold(t): return Style.apply("1", t)
def dim(t): return Style.apply("2", t)
def red(t): return Style.apply("31;1", t)
def green(t): return Style.apply("32", t)
def yellow(t): return Style.apply("33", t)
def cyan(t): return Style.apply("36", t)


ICONS = {"ok": ("✔", green), "info": ("•", cyan), "warn": ("!", yellow), "bad": ("✖", red)}
CHECK_CELL = {"ok": ("ok", green), "info": ("note", cyan), "warn": ("WARN", yellow), "bad": ("CRITICAL", red),
              "none": ("-", dim)}
DECIMALS = {"V": 3, "MHz": 0, "RPM": 0, "MB": 0, "ms": 2, "GB/s": 2, "": 2}
BARS = "▁▂▃▄▅▆▇█"


def fmt(v, unit):
    if v is None:
        return "-"
    return f"{v:,.{DECIMALS.get(unit, 1)}f}"


def fmt_duration(sec):
    sec = int(round(sec))
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}h {m:02d}m {s:02d}s"
    if m:
        return f"{m}m {s:02d}s"
    return f"{s}s"


def _cell_text(c):
    return c[0] if isinstance(c, tuple) else c


def table_width(headers, rows):
    widths = [max(len(_cell_text(c)) for c in col) for col in zip(headers, *rows)]
    return sum(widths) + 2 * (len(widths) - 1) + 2


def print_table(headers, rows, align=None):
    """Cells are plain text or (text, style_function)."""
    widths = [max(len(_cell_text(c)) for c in col) for col in zip(headers, *rows)]
    align = align or ["<"] + [">"] * (len(headers) - 1)

    def line(cells, head=False):
        out = []
        for k, (c, w, a) in enumerate(zip(cells, widths, align)):
            last = k == len(cells) - 1 and a == "<"
            padded = _cell_text(c) if last else f"{_cell_text(c):{a}{w}}"
            if head:
                padded = bold(padded)
            elif isinstance(c, tuple) and c[1]:
                padded = c[1](padded)
            out.append(padded)
        return "  " + "  ".join(out)

    print(line(headers, head=True))
    print("  " + dim("─" * (sum(widths) + 2 * (len(widths) - 1))))
    for r in rows:
        print(line(r))


def sparkline(values, width, lo, hi, peak=False):
    n = len(values)
    out = []
    for b in range(width):
        chunk = [v for v in values[b * n // width:(b + 1) * n // width] if v is not None]
        if not chunk:
            out.append(" ")
            continue
        v = max(chunk) if peak else sum(chunk) / len(chunk)
        frac = 0.0 if hi <= lo else min(1.0, max(0.0, (v - lo) / (hi - lo)))
        out.append(BARS[round(frac * (len(BARS) - 1))])
    return "".join(out)


def flag_strip(values, width):
    n = len(values)
    return "".join("█" if any(values[b * n // width:(b + 1) * n // width]) else "·" for b in range(width))


def section(title, term):
    print()
    print(bold(cyan(f"── {title} " + "─" * max(3, min(term, 110) - len(title) - 4))))


def fit_width(base, term, most=40):
    """How many characters a trend column can use without wrapping the terminal."""
    w = min(most, term - base - 3)
    return w if w >= 8 else 0


def report(log, term):
    n = len(log.times)
    t0, t1 = log.times[0], log.times[-1]
    bar = "═" * min(term, 110)
    print()
    print(bold(bar))
    print(bold(f"  HWiNFO log: {os.path.basename(log.path)}"))
    print(bold(bar))
    span = f"{t0:%d %b %Y %H:%M:%S} → {t1:%H:%M:%S}" if t0.date() == t1.date() else f"{t0:%d %b %Y %H:%M} → {t1:%d %b %Y %H:%M}"
    print(f"  {'Recorded':<12}{span}  ({fmt_duration(log.duration)})")
    print(f"  {'Samples':<12}{n:,}, one every ~{log.interval:.1f} s, {len(log.columns)} sensors")
    hw = log.hardware()
    first = True
    for device, desc in hw.items():
        label = "Hardware" if first else ""
        dev = "FPS from" if device == "PresentMon" else device
        print(f"  {label:<12}{dev:<11}{desc}")
        first = False

    section("Summary", term)
    for level, msg in analyse(log):
        icon, style = ICONS[level]
        lines = textwrap.wrap(msg, max(40, min(term, 110) - 6))
        print(f"  {style(icon)} {style(lines[0]) if level in ('bad', 'warn') else lines[0]}")
        for extra in lines[1:]:
            print(f"    {extra}")

    flags = [c for c in log.columns if c.is_flag]
    if flags:
        section("Throttling & performance limits", term)
        rows, details = [], []
        for c in flags:
            on = sum(1 for v in c.values if v)
            runs = flag_runs(c.values)
            thermal = is_thermal_flag(c)
            active_style = (red if thermal else yellow) if on else dim
            longest = max((run_seconds(log, r) for r in runs), default=0)
            rows.append([
                ("! " if thermal and on else "") + short_flag(c), c.device,
                (f"{on / n:.1%}", active_style),
                (fmt_duration(on * log.interval) if on else "-", active_style),
                str(len(runs)) if runs else "-",
                fmt_duration(longest) if runs else "-",
            ])
            if runs and on / n < 0.5:
                top = sorted(sorted(runs, key=lambda r: -run_seconds(log, r))[:5])
                details.append(f"{short_flag(c)}: " + ", ".join(
                    f"{log.times[s]:%H:%M:%S} ({fmt_duration(run_seconds(log, (s, e)))})" for s, e in top))
        headers = ["Flag", "Device", "Active", "Time", "Events", "Longest"]
        w = fit_width(table_width(headers, rows), term)
        if w:
            headers.append(f"Timeline {t0:%H:%M} → {t1:%H:%M}")
            for r, c in zip(rows, flags):
                strip = flag_strip(c.values, w)
                r.append((strip, (red if is_thermal_flag(c) else yellow) if "█" in strip else dim))
        print_table(headers, rows, ["<", "<", ">", ">", ">", ">", "<"])
        if details:
            print()
            print(dim("  Longest events (start time, duration):"))
            for d in details:
                for i, part in enumerate(textwrap.wrap(d, max(40, min(term, 110) - 6))):
                    print(("    " if i == 0 else "      ") + part)

    checks = {ch.col.name: ch for ch in log.checks}
    section("Health checks (every sensor vs. normal for its component)", term)
    issues = [ch for ch in log.checks if ch.level in ("bad", "warn", "info")]
    issues.sort(key=lambda ch: ("bad", "warn", "info").index(ch.level))
    judged = [ch for ch in log.checks if ch.level != "none"]
    if issues:
        headers = ["Sensor", "Device", "Status", "Reading", "Normal", "When"]
        rows = [[ch.col.label[:40], ch.col.device, CHECK_CELL[ch.level], ch.value, ch.normal,
                 f"{log.times[ch.at]:%H:%M:%S}" if ch.at is not None else ""] for ch in issues]
        if log.processes:
            headers.append("Busiest programs then")
            for row, ch in zip(rows, issues):
                row.append(ch.programs[:60])
        print_table(headers, rows, ["<"] * len(headers))
        print()
    ok = sum(1 for ch in judged if ch.level == "ok")
    print(f"  {green('✔')} {ok} of {len(judged)} sensors with known limits are normal · "
          f"{len(log.checks) - len(judged)} sensors have no fixed limit (checked for glitches/spikes only)")

    limits = {col.name: limit for col, limit, _ in temp_limits(log)}
    by_cat = {}
    fixed, empty, spikes = [], [], []
    for c in log.columns:
        if c.is_flag:
            continue
        if not c.stats:
            empty.append(c)
        elif c.stats["fixed"]:
            fixed.append(c)
        else:
            by_cat.setdefault(category(c), []).append(c)
            if c.stats["spiky"]:
                spikes.append(c)

    for cat in CATEGORY_ORDER:
        cols = by_cat.get(cat)
        if not cols:
            continue
        section(cat, term)
        temps = cat == "Temperatures"
        headers = ["Sensor", "Device", "Min", "Avg", "P99", "Max", "Unit"] + (["Limit"] if temps else [])
        rows = []
        for c in cols:
            s = c.stats
            max_cell = (fmt(s["max"], c.unit) + ("*" if s["spiky"] else " "), yellow if s["spiky"] else None)
            row = [c.label[:44], c.device, fmt(s["min"], c.unit), fmt(s["avg"], c.unit),
                   fmt(s["p99"], c.unit), max_cell, c.unit]
            if temps:
                lim = limits.get(c.name)
                if lim is None:
                    row.append("")
                else:
                    head = lim - s["max"]
                    row.append((f"{lim:.0f} (-{head:.0f})", green if head >= 10 else yellow if head >= 3 else red))
            ch = checks.get(c.name)
            row.append(CHECK_CELL[ch.level] if ch else "")
            rows.append(row)
        headers.append("Status")
        w = fit_width(table_width(headers, rows), term, most=30)
        if w:
            headers.append("Trend" + (" (peaks)" if temps else ""))
            for r, c in zip(rows, cols):
                s = c.stats
                hi = s["p99"] if s["spiky"] else s["max"]
                r.append((sparkline(c.values, w, s["min"], hi, peak=temps), cyan))
        align = ["<", "<", ">", ">", ">", ">", "<"] + ([">"] if temps else []) + ["<", "<"]
        print_table(headers, rows, align)
        if any(c.stats["spiky"] for c in cols):
            print(dim("  * max is a one-off spike far above P99 - likely a sensor glitch"))

    if fixed:
        section("Constant for the whole log", term)
        items = [f"{c.label} = {fmt(c.stats['max'], c.unit)} {c.unit}".rstrip() for c in fixed]
        for line in textwrap.wrap(" · ".join(items), max(40, min(term, 110) - 4)):
            print("  " + line)
    if empty:
        section("No readings", term)
        for line in textwrap.wrap(", ".join(c.label for c in empty), max(40, min(term, 110) - 4)):
            print("  " + line)

    if spikes:
        section("Spikes (readings over 1.5× P99)", term)
        glitch_idx = {(d, i) for d, i in glitch_moments(log)}
        rows = []
        for c in spikes:
            s = c.stats
            idx = spike_indices(c)
            in_glitch = sum(1 for i in idx if (c.device, i) in glitch_idx)
            cause = ("read glitch", dim) if in_glitch * 2 >= len(idx) else ("spike", yellow)
            when = ", ".join(f"{log.times[i]:%H:%M:%S}" for i in idx[:3]) + (" …" if len(idx) > 3 else "")
            row = [c.label[:44], c.device, fmt(s["p99"], c.unit), (fmt(s["max"], c.unit), yellow),
                   c.unit, str(len(idx)), cause, when]
            if log.processes:
                row.append("" if cause[0] == "read glitch" else programs_at(log, idx[0], c, n=2)[:50])
            rows.append(row)
        rows.sort(key=lambda r: r[6][0] == "read glitch")
        headers = ["Sensor", "Device", "P99", "Max", "Unit", "Count", "Cause", "First seen"]
        if log.processes:
            headers.append("Busiest programs at first spike")
        print_table(headers, rows, ["<", "<", ">", ">", "<", ">", "<", "<", "<"])


def compare(logs, term):
    section("Comparison (avg / max)", term)
    names = [os.path.basename(l.path) for l in logs]
    rows = [["Duration"] + [fmt_duration(l.duration) for l in logs]]
    throttled = []
    for l in logs:
        thermal = [c for c in l.columns if is_thermal_flag(c)]
        hit = any(any(c.values) for c in thermal)
        throttled.append(("YES", red) if hit else ("no", green) if thermal else ("n/a", dim))
    rows.append(["Thermal throttling"] + throttled)
    for label, unit in KEY_METRICS:
        cols = [l.column(label, unit) for l in logs]
        if not any(c and c.label == label and c.stats for c in cols):
            continue
        cells = []
        for c in cols:
            if c and c.label == label and c.stats:
                cells.append(f"{fmt(c.stats['avg'], unit)} / {fmt(c.stats['max'], unit)} {unit}")
            else:
                cells.append("-")
        rows.append([label] + cells)
    print_table(["Metric"] + names, rows)


def main(argv=None):
    p = argparse.ArgumentParser(description="Readable report for HWiNFO CSV sensor logs.")
    p.add_argument("files", nargs="*", help="log files (default: every .csv in the current folder)")
    p.add_argument("--export", metavar="DIR", help="also write a cleaned UTF-8 CSV for each log into DIR")
    p.add_argument("--no-color", action="store_true", help="plain text output")
    p.add_argument("--text", action="store_true", help="print the report in the terminal instead of opening the window")
    p.add_argument("--clean", action="store_true", help="replace glitched/irregular readings before reporting/exporting")
    args = p.parse_args(argv)

    if not args.text and not args.export:
        try:
            import hwinfo_gui
        except ImportError as e:  # no tkinter - fall back to the terminal report
            print(f"Window unavailable ({e}), printing the report instead.\n", file=sys.stderr)
        else:
            hwinfo_gui.run(args.files)
            return 0

    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass
    Style.enabled = not args.no_color and sys.stdout.isatty() and "NO_COLOR" not in os.environ
    if Style.enabled and os.name == "nt":
        os.system("")  # turns on ANSI colours in the Windows console
    term = shutil.get_terminal_size((120, 40)).columns

    files = args.files or sorted(set(glob.glob("*.csv") + glob.glob("*.CSV")), key=os.path.getmtime)
    if not files:
        p.error("no .csv files found - pass the log path(s)")

    logs = []
    for path in files:
        try:
            log = load_log(path)
        except (OSError, ValueError) as e:
            print(red(f"Skipping {path}: {e}"), file=sys.stderr)
            continue
        if args.clean:
            log, _ = clean_log(log)
        logs.append(log)
        report(log, term)
        if args.export:
            os.makedirs(args.export, exist_ok=True)
            out = os.path.join(args.export, os.path.splitext(os.path.basename(path))[0] + "_clean.csv")
            export_csv(log, out)
            print(dim(f"\n  Cleaned CSV written to {out}"))

    if len(logs) > 1:
        compare(logs, term)
    print()
    return 0 if logs else 1


if __name__ == "__main__":
    sys.exit(main())
