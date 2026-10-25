#!/usr/bin/env python3
"""Desktop window for HWiNFO logs - built on hwinfo_reader's parsing and analysis.

    python hwinfo_gui.py                  opens every .csv in the current folder
    python hwinfo_gui.py log21.CSV        opens specific logs

Standard library only (tkinter).
"""

import bisect
import glob
import math
import os
import sys
import tkinter as tk
from collections import namedtuple
from datetime import timedelta
from tkinter import filedialog, messagebox, ttk
from tkinter import font as tkfont

import hwinfo_reader as hr

BG = "#14171c"
PANEL = "#1c2027"
RAISED = "#262b34"      # headings, buttons, inactive tabs
HOVER = "#30363f"
SELECT = "#2a3d5c"
BORDER = "#2e343e"
TEXT = "#e4e8ef"
MUTED = "#8b93a3"
GRID = "#262b33"
OK = "#3ccf91"
INFO = "#6ea8fe"
WARN = "#f0b429"
BAD = "#ff6b6b"
LANE = "#252a32"
BANNER = {"ok": "#14342a", "warn": "#3a2f12", "bad": "#401d20"}
PALETTE = ["#6ea8fe", "#ff7b72", "#3ccf91", "#f0b429", "#b392f0", "#ff9e4a", "#39c5cf", "#f778ba"]
LEVEL_STYLE = {"ok": ("✔", OK), "info": ("•", INFO), "warn": ("!", WARN), "bad": ("✖", BAD)}

FONT = ("Segoe UI", 10)
FONT_BOLD = ("Segoe UI Semibold", 10)
FONT_SMALL = ("Segoe UI", 9)
FONT_BIG = ("Segoe UI Semibold", 20)
FONT_TITLE = ("Segoe UI Semibold", 13)

SCALE = 1.0  # screen DPI / 96, set once the window exists


def px(n):
    return int(round(n * SCALE))


Series = namedtuple("Series", "label unit values color")

CARDS = [   # each card shows the first sensor in its list that the log has (AMD, Intel, NVIDIA, Radeon, Arc)
    ("CPU temperature", [(l, "°C") for l in hr.MAIN_CPU_TEMPS]),
    ("GPU temperature", [(l, "°C") for l in hr.MAIN_GPU_TEMPS]),
    ("GPU hot spot", [("GPU Hot Spot Temperature", "°C"), ("GPU Hotspot Temperature", "°C"),
                      ("GPU Junction Temperature", "°C")]),
    ("CPU power", [("CPU Package Power", "W"), ("CPU PPT", "W"), ("CPU Power", "W")]),
    ("GPU power", [("GPU Power", "W"), ("Total Board Power (TBP)", "W"), ("GPU ASIC Power", "W"),
                   ("Total Graphics Power (TGP)", "W"), ("GPU PPT", "W")]),
    ("RAM used", [("Physical Memory Load", "%")]),
]


# ------------------------------------------------------------------ helpers

def find_exact(log, options):
    for label, unit in options:
        c = log.column(label, unit)
        if c and c.label == label and c.stats:
            return c
    return None


def glitch_sets(log):
    """{device: {sample indices}} where HWiNFO misread that device's sensors."""
    out = {}
    for device, i in hr.glitch_moments(log):
        out.setdefault(device, set()).add(i)
    return out


def clean_values(col, glitches):
    bad = glitches.get(col.device)
    if not bad:
        return col.values
    return [None if i in bad else v for i, v in enumerate(col.values)]


def nice_ticks(lo, hi, count=5):
    span = hi - lo
    raw = span / count
    mag = 10 ** math.floor(math.log10(raw))
    step = next(m * mag for m in (1, 2, 2.5, 5, 10) if m * mag >= raw)
    ticks, v = [], math.ceil(lo / step) * step
    while v <= hi + step * 1e-6:
        ticks.append(v)
        v += step
    decimals = next(d for d in range(7) if abs(round(step, d) - step) < step * 1e-6)
    return ticks, decimals


def time_ticks(t0, span, width):
    """[(seconds from start, 'HH:MM')] aligned to round clock times."""
    choices = (10, 30, 60, 120, 300, 600, 900, 1800, 3600, 7200, 14400, 28800, 86400)
    step = next((s for s in choices if span / s <= max(1, width / px(90))), 86400)
    base = t0.replace(hour=0, minute=0, second=0, microsecond=0)
    off0 = (t0 - base).total_seconds()
    out, k = [], math.ceil(off0 / step)
    while k * step - off0 <= span:
        when = base + timedelta(seconds=k * step)
        out.append((k * step - off0, when.strftime("%H:%M" if step >= 60 else "%H:%M:%S")))
        k += 1
    return out


def make_tree(parent, columns, tree_heading=None, tree_width=280, height=10):
    """Treeview + scrollbar in a frame. columns: [(id, heading, width, anchor)]."""
    frame = ttk.Frame(parent)
    tree = ttk.Treeview(frame, columns=[c[0] for c in columns], height=height,
                        show="tree headings" if tree_heading else "headings", selectmode="extended")
    if tree_heading:
        tree.heading("#0", text=tree_heading, anchor="w")
        tree.column("#0", width=px(tree_width), stretch=False)
    for cid, heading, width, anchor in columns:
        tree.heading(cid, text=heading, anchor=anchor)
        tree.column(cid, width=px(width), anchor=anchor, stretch=cid == columns[-1][0])
    sb = ttk.Scrollbar(frame, orient="vertical", command=tree.yview)
    tree.configure(yscrollcommand=sb.set)
    tree.pack(side="left", fill="both", expand=True)
    sb.pack(side="right", fill="y")
    tree.tag_configure("bad", foreground=BAD)
    tree.tag_configure("warn", foreground=WARN)
    tree.tag_configure("ok", foreground=OK)
    tree.tag_configure("muted", foreground=MUTED)
    tree.tag_configure("info", foreground=INFO)
    tree.tag_configure("category", font=FONT_BOLD)
    return frame, tree


# ------------------------------------------------------------------ chart

class Chart(tk.Canvas):
    """Line chart of sensor values over time, with hover readout."""

    processes = None  # ProcessLog of the log on screen - set by App.show_log; adds programs to the hover readout

    def __init__(self, master, empty_text="Select sensors to plot", **kw):
        super().__init__(master, bg=PANEL, highlightthickness=1, highlightbackground=BORDER, **kw)
        self.empty_text = empty_text
        self.series, self.times, self.secs = [], [], []
        self.limit, self.title, self.map = None, "", None
        self.font = tkfont.Font(font=FONT_SMALL)
        self.bind("<Configure>", lambda e: self.redraw())
        self.bind("<Motion>", self.on_motion)
        self.bind("<Leave>", lambda e: self.delete("hover"))

    def show(self, times, series, limit=None, title=""):
        self.times = times
        self.secs = [(t - times[0]).total_seconds() for t in times] if times else []
        self.series, self.limit, self.title = series, limit, title
        self.redraw()

    def redraw(self):
        self.delete("all")
        self.map = None
        w, h = self.winfo_width(), self.winfo_height()
        if w < 80 or h < 80:
            return
        vals = [v for s in self.series for v in s.values if v is not None]
        if not self.series or not vals:
            self.create_text(w / 2, h / 2, text=self.empty_text, fill=MUTED, font=FONT)
            return
        L, R, T, B = px(64), px(18), px(40), px(30)
        x0, y0, x1, y1 = L, T, w - R, h - B
        span = self.secs[-1] or 1.0
        lo, hi = min(vals), max(vals)
        if self.limit is not None:
            hi = max(hi, self.limit)
        pad = (hi - lo) * 0.08 or max(1.0, abs(hi) * 0.05)
        lo, hi = lo - pad, hi + pad
        self.map = (x0, y0, x1, y1, lo, hi, span)

        ticks, dec = nice_ticks(lo, hi)
        for t in ticks:
            y = self.yf(t)
            self.create_line(x0, y, x1, y, fill=GRID)
            self.create_text(x0 - px(8), y, text=f"{t:,.{dec}f}", anchor="e", fill=MUTED, font=FONT_SMALL)
        for s, label in time_ticks(self.times[0], span, x1 - x0):
            x = self.xf(s)
            self.create_line(x, y0, x, y1, fill=GRID)
            self.create_text(x, y1 + px(6), text=label, anchor="n", fill=MUTED, font=FONT_SMALL)
        self.create_rectangle(x0, y0, x1, y1, outline=BORDER)

        if self.limit is not None:
            y = self.yf(self.limit)
            self.create_line(x0, y, x1, y, fill=BAD, dash=(6, 4), width=px(1.5))
            unit = self.series[0].unit
            self.create_text(x1 - px(4), y - px(3), text=f"limit {hr.fmt(self.limit, unit)} {unit}",
                             anchor="se", fill=BAD, font=FONT_SMALL)

        for s in self.series:
            for seg in self.segments(s.values):
                self.create_line(*seg, fill=s.color, width=px(1.6))

        # title + legend
        x = x0
        if self.title:
            self.create_text(x, px(14), text=self.title, anchor="w", fill=TEXT, font=FONT_BOLD)
            x += tkfont.Font(font=FONT_BOLD).measure(self.title) + px(16)
        for s in self.series:
            text = f"{s.label} [{s.unit}]" if s.unit else s.label
            if x + px(14) + self.font.measure(text) > x1:
                break
            self.create_rectangle(x, px(9), x + px(10), px(19), fill=s.color, outline="")
            self.create_text(x + px(14), px(14), text=text, anchor="w", fill=TEXT, font=FONT_SMALL)
            x += px(14) + self.font.measure(text) + px(14)

    def xf(self, s):
        x0, _, x1, _, _, _, span = self.map
        return x0 + s / span * (x1 - x0)

    def yf(self, v):
        _, y0, _, y1, lo, hi, _ = self.map
        return y1 - (v - lo) / (hi - lo) * (y1 - y0)

    def segments(self, values):
        """Polylines with one min/max pair per pixel column, broken at missing values."""
        segs, cur, i, n = [], [], 0, len(values)

        def close():
            if len(cur) >= 4:
                segs.append(cur)
            elif len(cur) == 2:
                segs.append(cur + [cur[0] + 1, cur[1]])

        while i < n:
            v = values[i]
            if v is None:
                close()
                cur = []
                i += 1
                continue
            col = int(self.xf(self.secs[i]))
            mn = mx = v
            imn = imx = i
            j = i + 1
            while j < n and values[j] is not None and int(self.xf(self.secs[j])) == col:
                if values[j] < mn:
                    mn, imn = values[j], j
                if values[j] > mx:
                    mx, imx = values[j], j
                j += 1
            if mn == mx:
                cur += [col, self.yf(mn)]
            elif imn < imx:
                cur += [col, self.yf(mn), col, self.yf(mx)]
            else:
                cur += [col, self.yf(mx), col, self.yf(mn)]
            i = j
        close()
        return segs

    def on_motion(self, e):
        self.delete("hover")
        if not self.map:
            return
        x0, y0, x1, y1, _, _, span = self.map
        if not (x0 <= e.x <= x1 and y0 <= e.y <= y1):
            return
        s = (e.x - x0) / (x1 - x0) * span
        i = bisect.bisect_left(self.secs, s)
        if i >= len(self.secs) or (i > 0 and s - self.secs[i - 1] < self.secs[i] - s):
            i -= 1
        x = self.xf(self.secs[i])
        self.create_line(x, y0, x, y1, fill=MUTED, dash=(2, 3), tags="hover")

        lines = [(self.times[i].strftime("%H:%M:%S"), TEXT, None)]
        for ser in self.series:
            v = ser.values[i]
            if v is not None:
                y = self.yf(v)
                self.create_oval(x - px(3), y - px(3), x + px(3), y + px(3), fill=ser.color,
                                 outline=PANEL, tags="hover")
            shown = "-" if v is None else f"{hr.fmt(v, ser.unit)} {ser.unit}"
            lines.append((f"{ser.label}: {shown}", TEXT, ser.color))
        if Chart.processes:
            for p in Chart.processes.at(self.times[i], "activity", 3):
                lines.append(("  " + hr.describe_procs([p]), MUTED, None))

        lh = px(17)
        tw = max(self.font.measure(t) for t, _, _ in lines) + px(30)
        bh = lh * len(lines) + px(10)
        bx = x + px(12) if x + px(12) + tw < x1 else x - px(12) - tw
        by = min(max(y0, e.y - bh / 2), y1 - bh)
        self.create_rectangle(bx, by, bx + tw, by + bh, fill=PANEL, outline=BORDER, tags="hover")
        for k, (text, fg, swatch) in enumerate(lines):
            ty = by + px(5) + k * lh + lh / 2
            if swatch:
                self.create_rectangle(bx + px(8), ty - px(4), bx + px(16), ty + px(4), fill=swatch,
                                      outline="", tags="hover")
            self.create_text(bx + px(22) if swatch else bx + px(8), ty, text=text, anchor="w", fill=fg,
                             font=FONT_BOLD if k == 0 else FONT_SMALL, tags="hover")


class Timeline(tk.Canvas):
    """One lane per Yes/No flag, shaded where the flag was active."""

    ROW = 24

    def __init__(self, master, **kw):
        super().__init__(master, bg=PANEL, highlightthickness=1, highlightbackground=BORDER, **kw)
        self.log, self.flags = None, []
        self.bind("<Configure>", lambda e: self.redraw())

    def show(self, log, flags):
        self.log, self.flags = log, flags
        self.configure(height=px(16) + len(flags) * px(self.ROW) + px(30))
        self.redraw()

    def redraw(self):
        self.delete("all")
        w = self.winfo_width()
        if not self.log or w < 100:
            return
        log = self.log
        f = tkfont.Font(font=FONT_SMALL)
        labels = [f"{c.device}  {hr.short_flag(c)}" for c in self.flags]
        x0 = max(f.measure(t) for t in labels) + px(24)
        x1 = w - px(16)
        span = log.duration or 1.0
        t_first = log.times[0]

        def xf(t):
            return x0 + (t - t_first).total_seconds() / span * (x1 - x0)

        top = px(10)
        for k, (c, label) in enumerate(zip(self.flags, labels)):
            y = top + k * px(self.ROW)
            mid = y + px(self.ROW) / 2
            self.create_text(px(10), mid, text=label, anchor="w", fill=TEXT, font=FONT_SMALL)
            self.create_rectangle(x0, y + px(5), x1, y + px(self.ROW) - px(5), fill=LANE, outline="")
            color = BAD if hr.is_thermal_flag(c) else WARN
            for s, e in hr.flag_runs(c.values):
                a = xf(log.times[s])
                b = max(a + 1, xf(log.times[e] + timedelta(seconds=log.interval)))
                self.create_rectangle(a, y + px(5), min(b, x1), y + px(self.ROW) - px(5), fill=color, outline="")
        bottom = top + len(self.flags) * px(self.ROW)
        for s, label in time_ticks(log.times[0], span, x1 - x0):
            x = x0 + s / span * (x1 - x0)
            self.create_line(x, top, x, bottom + px(3), fill=GRID)
            self.create_text(x, bottom + px(6), text=label, anchor="n", fill=MUTED, font=FONT_SMALL)


# ------------------------------------------------------------------ tabs

class OverviewTab(ttk.Frame):
    def __init__(self, master):
        super().__init__(master, padding=px(14))
        self.header = ttk.Label(self, font=FONT_TITLE)
        self.header.pack(anchor="w")
        self.sub = ttk.Label(self, foreground=MUTED)
        self.sub.pack(anchor="w", pady=(px(2), px(12)))

        self.cards = ttk.Frame(self)
        self.cards.pack(fill="x")

        ttk.Label(self, text="What the log shows", font=FONT_BOLD).pack(anchor="w", pady=(px(14), px(4)))
        box = tk.Frame(self, bg=PANEL, highlightthickness=1, highlightbackground=BORDER)
        box.pack(fill="x")
        self.findings = tk.Text(box, height=10, wrap="word", relief="flat", bg=PANEL, fg=TEXT, font=FONT,
                                padx=px(10), pady=px(8), cursor="arrow", insertbackground=TEXT,
                                selectbackground=SELECT, highlightthickness=0)
        sb = ttk.Scrollbar(box, orient="vertical", command=self.findings.yview)
        self.findings.configure(yscrollcommand=sb.set)
        self.findings.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        for level, (_, color) in LEVEL_STYLE.items():
            self.findings.tag_configure(level, foreground=color, font=FONT_BOLD)
        self.findings.tag_configure("strong", foreground=BAD, font=FONT_BOLD)
        self.findings.tag_configure("warntext", foreground=WARN)
        self.findings.tag_configure("line", spacing1=px(3), spacing3=px(3), lmargin2=px(22))

        charts = ttk.Frame(self)
        charts.pack(fill="both", expand=True, pady=(px(14), 0))
        charts.columnconfigure(0, weight=1, uniform="c")
        charts.columnconfigure(1, weight=1, uniform="c")
        charts.rowconfigure(0, weight=1)
        self.temp_chart = Chart(charts, empty_text="No CPU/GPU temperature sensors in this log")
        self.temp_chart.grid(row=0, column=0, sticky="nsew", padx=(0, px(7)))
        self.power_chart = Chart(charts, empty_text="No power sensors in this log")
        self.power_chart.grid(row=0, column=1, sticky="nsew", padx=(px(7), 0))

    def card(self, column, title, value, sub, color):
        f = tk.Frame(self.cards, bg=PANEL, highlightthickness=1, highlightbackground=BORDER,
                     padx=px(14), pady=px(10))
        f.grid(row=0, column=column, sticky="nsew", padx=(0 if column == 0 else px(10), 0))
        self.cards.columnconfigure(column, weight=1, uniform="card")
        tk.Label(f, text=title.upper(), bg=PANEL, fg=MUTED, font=FONT_SMALL).pack(anchor="w")
        tk.Label(f, text=value, bg=PANEL, fg=color, font=FONT_BIG).pack(anchor="w")
        tk.Label(f, text=sub, bg=PANEL, fg=MUTED, font=FONT_SMALL).pack(anchor="w")

    def show(self, log, ctx):
        hw = log.hardware()
        parts = [hw[d] for d in ("CPU", "GPU", "System") if d in hw]
        parts += [d + ": " + desc for d, desc in hw.items() if d not in ("CPU", "GPU", "System", "PresentMon")]
        self.header.configure(text="  ·  ".join(parts) or os.path.basename(log.path))
        t0, t1 = log.times[0], log.times[-1]
        fps = f"  ·  FPS from {hw['PresentMon']}" if "PresentMon" in hw else ""
        procs = (f"  ·  programs from {', '.join(os.path.basename(p) for p in log.processes.paths)}"
                 if log.processes else "  ·  no process log")
        self.sub.configure(text=f"{os.path.basename(log.path)}  ·  {t0:%d %b %Y %H:%M:%S} → {t1:%H:%M:%S} "
                                f"({hr.fmt_duration(log.duration)})  ·  {len(log.times):,} samples every "
                                f"{log.interval:.1f} s  ·  {len(log.columns)} sensors{fps}{procs}")

        for child in self.cards.winfo_children():
            child.destroy()
        thermal = [c for c in log.columns if hr.is_thermal_flag(c)]
        hit = [c for c in thermal if any(c.values)]
        if not thermal:
            self.card(0, "Thermal throttling", "Unknown", "no throttle flags logged", WARN)
        elif hit:
            on = max(sum(1 for v in c.values if v) for c in hit) / len(log.times)
            self.card(0, "Thermal throttling", "YES", f"{len(hit)} flag(s), up to {on:.1%} of the time", BAD)
        else:
            self.card(0, "Thermal throttling", "None", f"{len(thermal)} flags checked", OK)

        temps_for_chart, power_for_chart = [], []
        for k, (title, options) in enumerate(CARDS, start=1):
            col = find_exact(log, options)
            if not col:
                self.card(k, title, "-", "not in this log", MUTED)
                continue
            s = hr.compute_stats(clean_values(col, ctx["glitches"]))
            color, sub = TEXT, f"avg {hr.fmt(s['avg'], col.unit)} · p99 {hr.fmt(s['p99'], col.unit)}"
            lim = ctx["limits"].get(col.name)
            if lim is not None:
                head = lim - s["max"]
                color = OK if head >= 10 else WARN if head >= 3 else BAD
                sub += f" · {head:.0f}° below limit"
            elif col.unit == "%" and s["max"] >= 90:
                color = WARN
            self.card(k, title, f"{hr.fmt(s['max'], col.unit)} {col.unit}", "peak · " + sub, color)
            if col.unit == "°C":
                temps_for_chart.append(col)
            elif col.unit == "W":
                power_for_chart.append(col)

        self.findings.configure(state="normal")
        self.findings.delete("1.0", "end")
        for level, msg in ctx["findings"]:
            icon, _ = LEVEL_STYLE[level]
            self.findings.insert("end", f"{icon}  ", (level, "line"))
            body = "strong" if level == "bad" else "warntext" if level == "warn" else ()
            self.findings.insert("end", msg + "\n", ("line",) + ((body,) if body else ()))
        self.findings.configure(state="disabled")

        spot = find_exact(log, CARDS[2][1])
        if spot and spot not in temps_for_chart:
            temps_for_chart.append(spot)
        self.temp_chart.show(log.times, [
            Series(c.label, c.unit, clean_values(c, ctx["glitches"]), PALETTE[i])
            for i, c in enumerate(temps_for_chart)], title="Temperatures")
        self.power_chart.show(log.times, [
            Series(c.label, c.unit, clean_values(c, ctx["glitches"]), PALETTE[i])
            for i, c in enumerate(power_for_chart)], title="Power")


STATUS_TEXT = {"bad": "✖ Critical", "warn": "! Warning", "info": "• Note", "ok": "✔ Normal", "none": "-"}
STATUS_TAG = {"bad": ("bad",), "warn": ("warn",), "info": ("info",), "ok": (), "none": ("muted",)}


class HealthTab(ttk.Frame):
    def __init__(self, master):
        super().__init__(master, padding=px(14))
        self.banner = tk.Label(self, font=FONT_TITLE, anchor="w", padx=px(14), pady=px(10))
        self.banner.pack(fill="x")
        ttk.Label(self, foreground=MUTED, wraplength=px(1200), justify="left", text=(
            "Every sensor is compared with what's normal for that kind of component: CPU, GPU, SSD, memory, "
            "motherboard, power rails, fans, RAM use and hardware errors. Checks use glitch-cleaned readings, so "
            "HWiNFO misreads can't trigger them. Click a sensor to chart it against its limit.")).pack(
            anchor="w", pady=(px(8), px(10)))
        pane = ttk.Panedwindow(self, orient="vertical")
        pane.pack(fill="both", expand=True)
        cols = [("device", "Device", 90, "w"), ("status", "Status", 100, "w"), ("value", "Reading", 210, "w"),
                ("normal", "Normal for this component", 200, "w"), ("when", "When", 70, "w"),
                ("programs", "Busiest programs at the time", 330, "w"), ("details", "Details", 600, "w")]
        frame, self.tree = make_tree(pane, cols, tree_heading="Sensor", tree_width=280, height=14)
        self.tree.bind("<<TreeviewSelect>>", lambda e: self.plot())
        pane.add(frame, weight=3)
        self.chart = Chart(pane, height=px(260), empty_text="Select a sensor to see its readings against its limit")
        pane.add(self.chart, weight=2)
        self.log, self.checks = None, {}

    def show(self, log, ctx):
        self.log, self.checks = log, {}
        self.tree.delete(*self.tree.get_children())
        checks = log.checks
        groups = [("bad", "Critical"), ("warn", "Warnings"), ("info", "Notes"), ("ok", "Normal"),
                  ("none", "No fixed limit - checked for glitches and spikes only")]
        for level, title in groups:
            items = [ch for ch in checks if ch.level == level]
            if not items:
                continue
            parent = self.tree.insert("", "end", iid=f"grp:{level}", text=f"{title}  ({len(items)})",
                                      open=level != "none", tags=("category",))
            for ch in items:
                self.checks[ch.col.name] = ch
                when = f"{log.times[ch.at]:%H:%M:%S}" if ch.at is not None else ""
                programs = ch.programs or ("no process log" if ch.at is not None and not log.processes else "")
                self.tree.insert(parent, "end", iid=ch.col.name, text=ch.col.label, tags=STATUS_TAG[level],
                                 values=(ch.col.device, STATUS_TEXT[level], ch.value, ch.normal, when, programs,
                                         ch.message))
        bad = sum(1 for ch in checks if ch.level == "bad")
        warn = sum(1 for ch in checks if ch.level == "warn")
        judged = sum(1 for ch in checks if ch.level != "none")
        if bad:
            self.banner.configure(text=f"✖  {bad} critical, {warn} warning(s) - see below", bg=BANNER["bad"], fg=BAD)
        elif warn:
            self.banner.configure(text=f"!  {warn} sensor(s) outside the normal range", bg=BANNER["warn"], fg=WARN)
        else:
            self.banner.configure(text=f"✔  All {judged} sensors with known limits are in the normal range",
                                  bg=BANNER["ok"], fg=OK)
        self.chart.show(log.times, [])
        return bad, warn

    def plot(self):
        chosen = [self.checks[i] for i in self.tree.selection() if i in self.checks][:len(PALETTE)]
        series = [Series(hr.who(ch.col), ch.col.unit, ch.col.values, PALETTE[k]) for k, ch in enumerate(chosen)]
        limit = chosen[0].line if len(chosen) == 1 else None
        self.chart.show(self.log.times, series, limit=limit)


class ThrottleTab(ttk.Frame):
    def __init__(self, master):
        super().__init__(master, padding=px(14))
        self.banner = tk.Label(self, font=FONT_TITLE, anchor="w", padx=px(14), pady=px(10))
        self.banner.pack(fill="x")
        self.explain = ttk.Label(self, foreground=MUTED, wraplength=px(1100), justify="left")
        self.explain.pack(anchor="w", pady=(px(8), px(10)))

        cols = [("device", "Device", 80, "w"), ("active", "Active", 80, "e"), ("time", "Time", 100, "e"),
                ("events", "Events", 70, "e"), ("longest", "Longest", 100, "e"), ("meaning", "What it means", 420, "w")]
        frame, self.tree = make_tree(self, cols, tree_heading="Flag", tree_width=260, height=9)
        frame.pack(fill="x")

        ttk.Label(self, text="Timeline (shaded = flag active)", font=FONT_BOLD).pack(anchor="w", pady=(px(14), px(4)))
        self.timeline = Timeline(self)
        self.timeline.pack(fill="x")

        ttk.Label(self, text="Events", font=FONT_BOLD).pack(anchor="w", pady=(px(14), px(4)))
        frame, self.events = make_tree(self, [("flag", "Flag", 260, "w"), ("start", "Start", 100, "w"),
                                              ("end", "End", 100, "w"), ("dur", "Duration", 100, "w"),
                                              ("programs", "Busiest programs at the start", 500, "w")], height=8)
        frame.pack(fill="both", expand=True)

    def show(self, log, ctx):
        flags = [c for c in log.columns if c.is_flag]
        thermal = [c for c in flags if hr.is_thermal_flag(c)]
        hit = [c for c in thermal if any(c.values)]
        if not thermal:
            self.banner.configure(text="!  No throttling flags in this log", bg=BANNER["warn"], fg=WARN)
        elif hit:
            self.banner.configure(text=f"✖  Thermal throttling detected ({len(hit)} flag(s))",
                                  bg=BANNER["bad"], fg=BAD)
        else:
            self.banner.configure(text=f"✔  No thermal throttling - all {len(thermal)} thermal flags stayed No",
                                  bg=BANNER["ok"], fg=OK)
        self.explain.configure(text="Thermal flags (red) mean a chip slowed down because of heat. The GPU's other "
                                    "\"Performance Limit\" flags (amber) say what capped its boost clock at that "
                                    "moment. Some limit is always active on a GPU that's boosting, so those are "
                                    "normal.")

        self.tree.delete(*self.tree.get_children())
        self.events.delete(*self.events.get_children())
        n = len(log.times)
        for c in flags:
            on = sum(1 for v in c.values if v)
            runs = hr.flag_runs(c.values)
            is_th = hr.is_thermal_flag(c)
            longest = max((hr.run_seconds(log, r) for r in runs), default=0)
            meaning = ("chip slowed down due to heat" if is_th
                       else hr.LIMIT_NOTES.get(hr.short_flag(c).lower(), ""))
            tag = ("bad" if is_th else "warn") if on else "muted"
            self.tree.insert("", "end", text=hr.short_flag(c), tags=(tag,), values=(
                c.device, f"{on / n:.1%}", hr.fmt_duration(on * log.interval) if on else "-",
                len(runs) or "-", hr.fmt_duration(longest) if runs else "-", meaning))
            for s, e in runs[:500]:
                self.events.insert("", "end", tags=("bad" if is_th else (),), values=(
                    f"{c.device} {hr.short_flag(c)}", f"{log.times[s]:%H:%M:%S}", f"{log.times[e]:%H:%M:%S}",
                    hr.fmt_duration(hr.run_seconds(log, (s, e))), hr.programs_at(log, s, c)))
        if not self.events.get_children():
            self.events.insert("", "end", values=("No flag was ever active", "", "", "", ""), tags=("muted",))
        self.timeline.show(log, flags)


class SensorsTab(ttk.Frame):
    def __init__(self, master):
        super().__init__(master, padding=px(14))
        bar = ttk.Frame(self)
        bar.pack(fill="x", pady=(0, px(8)))
        ttk.Label(bar, text="Search").pack(side="left")
        self.query = tk.StringVar()
        entry = ttk.Entry(bar, textvariable=self.query, width=30)
        entry.pack(side="left", padx=(px(6), px(16)))
        self.query.trace_add("write", lambda *a: self.fill())
        self.hide = tk.BooleanVar(value=True)
        ttk.Checkbutton(bar, text="Hide logging glitches in chart", variable=self.hide,
                        command=self.plot).pack(side="left")
        ttk.Label(bar, text="Click a sensor to chart it · Ctrl/Shift-click to compare several",
                  foreground=MUTED).pack(side="right")

        pane = ttk.Panedwindow(self, orient="vertical")
        pane.pack(fill="both", expand=True)
        cols = [("device", "Device", 90, "w"), ("min", "Min", 80, "e"), ("avg", "Avg", 80, "e"),
                ("p99", "P99", 80, "e"), ("max", "Max", 90, "e"), ("unit", "Unit", 60, "w"),
                ("status", "Status", 100, "w"), ("note", "Notes", 320, "w")]
        frame, self.tree = make_tree(pane, cols, tree_heading="Sensor", tree_width=320, height=14)
        self.tree.bind("<<TreeviewSelect>>", lambda e: self.plot())
        pane.add(frame, weight=3)
        self.chart = Chart(pane, height=px(300))
        pane.add(self.chart, weight=2)
        self.log, self.ctx, self.cols = None, None, {}

    def show(self, log, ctx):
        self.log, self.ctx = log, ctx
        self.cols = {c.name: c for c in log.columns if not c.is_flag}
        self.fill()
        default = [c.name for c in (find_exact(log, CARDS[0][1]), find_exact(log, CARDS[1][1])) if c]
        if default:
            self.tree.selection_set(default)
            self.tree.see(default[0])
        else:
            self.plot()

    def note(self, c):
        s = c.stats
        notes = []
        ch = self.ctx["checks"].get(c.name)
        if ch and ch.level in ("bad", "warn", "info"):
            notes.append(ch.message)
        elif ch and ch.level == "ok":
            notes.append(f"normal: {ch.normal}")
        lim = self.ctx["limits"].get(c.name)
        if lim is not None:
            notes.append(f"limit {lim:.0f} °C, {lim - s['max']:.0f} °C headroom")
        if s["spiky"]:
            notes.append("max is a logging glitch" if c.name in self.ctx["glitched"] else "short spikes - see P99")
        return "; ".join(notes)

    def fill(self):
        if not self.log:
            return
        keep = set(self.tree.selection())
        self.tree.delete(*self.tree.get_children())
        q = self.query.get().strip().lower()
        groups = {}
        for c in self.cols.values():
            if q and q not in c.label.lower() and q not in c.device.lower():
                continue
            if not c.stats:
                groups.setdefault("No readings", []).append(c)
            elif c.stats["fixed"]:
                groups.setdefault("Constant", []).append(c)
            else:
                groups.setdefault(hr.category(c), []).append(c)
        for cat in hr.CATEGORY_ORDER + ["Constant", "No readings"]:
            if cat not in groups:
                continue
            parent = self.tree.insert("", "end", iid=f"cat:{cat}", text=f"{cat}  ({len(groups[cat])})",
                                      open=cat not in ("Constant", "No readings") or bool(q), tags=("category",))
            for c in groups[cat]:
                s = c.stats
                ch = self.ctx["checks"].get(c.name)
                level = ch.level if ch else "none"
                if s:
                    vals = (c.device, hr.fmt(s["min"], c.unit), hr.fmt(s["avg"], c.unit), hr.fmt(s["p99"], c.unit),
                            hr.fmt(s["max"], c.unit) + (" *" if s["spiky"] else ""), c.unit, STATUS_TEXT[level],
                            self.note(c))
                else:
                    vals = (c.device, "", "", "", "", c.unit, STATUS_TEXT[level], "")
                tag = STATUS_TAG[level] if level in ("bad", "warn", "info") else ()
                self.tree.insert(parent, "end", iid=c.name, text=c.label, values=vals, tags=tag)
        still = [i for i in keep if self.tree.exists(i)]
        if still:
            self.tree.selection_set(still)

    def plot(self):
        if not self.log:
            return
        chosen = [self.cols[i] for i in self.tree.selection() if i in self.cols and self.cols[i].stats]
        chosen = chosen[:len(PALETTE)]
        glitches = self.ctx["glitches"] if self.hide.get() else {}
        series = [Series(f"{c.device} {c.label}" if not c.label.startswith(c.device) else c.label,
                         c.unit, clean_values(c, glitches), PALETTE[i]) for i, c in enumerate(chosen)]
        limit = self.ctx["limits"].get(chosen[0].name) if len(chosen) == 1 else None
        units = {c.unit for c in chosen}
        title = "Mixed units - compare shapes, not values" if len(units) > 1 else ""
        self.chart.show(self.log.times, series, limit=limit, title=title)


class SpikesTab(ttk.Frame):
    def __init__(self, master):
        super().__init__(master, padding=px(14))
        ttk.Label(self, wraplength=px(1100), justify="left", foreground=MUTED, text=(
            "Readings more than 1.5× above a sensor's 99th percentile. \"Logging glitch\" means many sensors on "
            "the same device jumped at the same instant: HWiNFO misread them (e.g. clocks and voltages doubling), "
            "so those values aren't real. \"Spike\" means a short burst that could be real, often frame-rate "
            "readings while windows change.")).pack(anchor="w", pady=(0, px(10)))
        pane = ttk.Panedwindow(self, orient="vertical")
        pane.pack(fill="both", expand=True)
        cols = [("device", "Device", 90, "w"), ("p99", "P99", 90, "e"), ("max", "Max", 90, "e"),
                ("unit", "Unit", 60, "w"), ("count", "Count", 60, "e"), ("cause", "Cause", 120, "w"),
                ("when", "When", 300, "w"), ("programs", "Busiest programs at first spike", 400, "w")]
        frame, self.tree = make_tree(pane, cols, tree_heading="Sensor", tree_width=300, height=12)
        self.tree.bind("<<TreeviewSelect>>", lambda e: self.plot())
        pane.add(frame, weight=3)
        self.chart = Chart(pane, height=px(260), empty_text="Select a row to see where the spikes are")
        pane.add(self.chart, weight=2)
        self.log, self.cols = None, {}

    def show(self, log, ctx):
        self.log = log
        self.tree.delete(*self.tree.get_children())
        self.cols = {}
        glitch_idx = set(hr.glitch_moments(log))
        rows = []
        for c in log.columns:
            idx = hr.spike_indices(c)
            if not idx:
                continue
            in_glitch = sum(1 for i in idx if (c.device, i) in glitch_idx)
            glitch = in_glitch * 2 >= len(idx)
            rows.append((glitch, c, idx))
        rows.sort(key=lambda r: r[0])
        for glitch, c, idx in rows:
            self.cols[c.name] = c
            when = ", ".join(f"{log.times[i]:%H:%M:%S}" for i in idx[:6]) + (" …" if len(idx) > 6 else "")
            self.tree.insert("", "end", iid=c.name, text=c.label, tags=("muted",) if glitch else ("warn",), values=(
                c.device, hr.fmt(c.stats["p99"], c.unit), hr.fmt(c.stats["max"], c.unit), c.unit, len(idx),
                "logging glitch" if glitch else "spike", when, "" if glitch else hr.programs_at(log, idx[0], c)))
        if not rows:
            self.tree.insert("", "end", text="No spikes found", tags=("muted",))
        self.chart.show(log.times, [])

    def plot(self):
        chosen = [self.cols[i] for i in self.tree.selection() if i in self.cols][:len(PALETTE)]
        self.chart.show(self.log.times, [Series(c.label, c.unit, c.values, PALETTE[k])
                                         for k, c in enumerate(chosen)])


class CompareTab(ttk.Frame):
    def __init__(self, master):
        super().__init__(master, padding=px(14))
        self.hint = ttk.Label(self, foreground=MUTED)
        self.hint.pack(anchor="w", pady=(0, px(10)))
        self.holder = ttk.Frame(self)
        self.holder.pack(fill="both", expand=True)

    def show_all(self, logs):
        for child in self.holder.winfo_children():
            child.destroy()
        self.hint.configure(text="Average / peak of the key sensors in every open log." if len(logs) > 1
                            else "Open more logs (Open logs… button) to compare them side by side.")
        cols = [(f"log{k}", os.path.basename(l.path), 190, "e") for k, l in enumerate(logs)]
        frame, tree = make_tree(self.holder, cols, tree_heading="Metric", tree_width=280, height=16)
        frame.pack(fill="both", expand=True)
        tree.insert("", "end", text="Recorded", values=[f"{l.times[0]:%d %b %H:%M}" for l in logs])
        tree.insert("", "end", text="Duration", values=[hr.fmt_duration(l.duration) for l in logs])
        cells = []
        for l in logs:
            thermal = [c for c in l.columns if hr.is_thermal_flag(c)]
            cells.append("YES" if any(any(c.values) for c in thermal) else "no" if thermal else "n/a")
        tree.insert("", "end", text="Thermal throttling", values=cells, tags=("bad",) if "YES" in cells else ("ok",))
        for label, unit in hr.KEY_METRICS:
            cols_ = [find_exact(l, [(label, unit)]) for l in logs]
            if not any(cols_):
                continue
            tree.insert("", "end", text=f"{label}  (avg / peak)", values=[
                f"{hr.fmt(c.stats['avg'], unit)} / {hr.fmt(c.stats['max'], unit)} {unit}" if c else "-"
                for c in cols_])


# ------------------------------------------------------------------ app

class App(tk.Tk):
    def __init__(self, files):
        super().__init__()
        global SCALE
        SCALE = max(1.0, self.winfo_fpixels("1i") / 96)
        self.title("HWiNFO Log Reader")
        self.configure(bg=BG)
        self.geometry(f"{px(1360)}x{px(880)}")
        self.minsize(px(1000), px(680))
        try:
            self.state("zoomed")
        except tk.TclError:
            pass
        self._style()
        self._dark_title_bar()

        bar = tk.Frame(self, bg=PANEL, highlightthickness=1, highlightbackground=BORDER, padx=px(14), pady=px(8))
        bar.pack(fill="x")
        tk.Label(bar, text="HWiNFO Log Reader", bg=PANEL, fg=TEXT, font=FONT_TITLE).pack(side="left")
        ttk.Button(bar, text="Open logs…", command=self.open_dialog).pack(side="right")
        ttk.Button(bar, text="Save CSV…", command=self.save_csv).pack(side="right", padx=(0, px(6)))
        self.clean_btn = ttk.Button(bar, text="Clean glitches", style="Accent.TButton", command=self.toggle_clean)
        self.clean_btn.pack(side="right", padx=(0, px(16)))
        self.picker = ttk.Combobox(bar, state="readonly", width=48)
        self.picker.pack(side="right", padx=(0, px(10)))
        self.picker.bind("<<ComboboxSelected>>", lambda e: self.show_current())
        tk.Label(bar, text="Log:", bg=PANEL, fg=MUTED, font=FONT).pack(side="right", padx=(0, px(6)))
        self.status = tk.Label(bar, text="", bg=PANEL, fg=MUTED, font=FONT_SMALL)
        self.status.pack(side="left", padx=(px(20), 0))

        self.tabs = ttk.Notebook(self)
        self.tabs.pack(fill="both", expand=True, padx=px(10), pady=px(10))
        self.overview, self.health, self.throttle = OverviewTab(self.tabs), HealthTab(self.tabs), ThrottleTab(self.tabs)
        self.sensors, self.spikes, self.compare = SensorsTab(self.tabs), SpikesTab(self.tabs), CompareTab(self.tabs)
        for tab, name in ((self.overview, "Overview"), (self.health, "Health checks"), (self.throttle, "Throttling"),
                          (self.sensors, "All sensors"), (self.spikes, "Spikes & glitches"),
                          (self.compare, "Compare logs")):
            self.tabs.add(tab, text=name)

        self.logs = []          # as loaded from disk
        self.cleaned = {}       # id(raw log) -> cleaned copy, built on first use
        self.use_clean = False
        self.after(50, lambda: self.add_files(files) if files else self.open_dialog())

    def _style(self):
        s = ttk.Style(self)
        s.theme_use("clam")
        s.configure(".", background=BG, foreground=TEXT, font=FONT)
        s.configure("TFrame", background=BG)
        s.configure("TLabel", background=BG, foreground=TEXT)
        s.configure("TCheckbutton", background=BG)
        s.configure("TNotebook", background=BG, borderwidth=0, tabmargins=0)
        s.configure(".", bordercolor=BORDER, darkcolor=PANEL, lightcolor=PANEL, troughcolor=BG,
                    selectbackground=SELECT, selectforeground=TEXT, fieldbackground=PANEL, insertcolor=TEXT)
        s.configure("TNotebook.Tab", padding=(px(16), px(7)), background=RAISED, foreground=MUTED, borderwidth=0)
        s.map("TNotebook.Tab", background=[("selected", BG), ("active", HOVER)],
              foreground=[("selected", TEXT), ("active", TEXT)])
        s.configure("Treeview", background=PANEL, fieldbackground=PANEL, foreground=TEXT, rowheight=px(24),
                    borderwidth=0, font=FONT)
        s.map("Treeview", background=[("selected", SELECT)], foreground=[("selected", TEXT)])
        s.configure("Treeview.Heading", background=RAISED, foreground=TEXT, font=FONT_BOLD, relief="flat",
                    padding=(px(6), px(4)))
        s.map("Treeview.Heading", background=[("active", HOVER)])
        s.configure("TButton", padding=(px(12), px(5)), background=RAISED, foreground=TEXT)
        s.map("TButton", background=[("pressed", SELECT), ("active", HOVER)])
        s.configure("Accent.TButton", background="#24477a", foreground=TEXT)
        s.map("Accent.TButton", background=[("pressed", SELECT), ("active", "#2d5795")])
        s.configure("TEntry", fieldbackground=PANEL, foreground=TEXT)
        s.configure("TCombobox", fieldbackground=PANEL, background=RAISED, foreground=TEXT, arrowcolor=TEXT)
        s.map("TCombobox", fieldbackground=[("readonly", PANEL)], foreground=[("readonly", TEXT)],
              selectbackground=[("readonly", PANEL)], selectforeground=[("readonly", TEXT)],
              background=[("active", HOVER)])
        self.option_add("*TCombobox*Listbox.background", PANEL)
        self.option_add("*TCombobox*Listbox.foreground", TEXT)
        self.option_add("*TCombobox*Listbox.selectBackground", SELECT)
        self.option_add("*TCombobox*Listbox.selectForeground", TEXT)
        s.configure("TCheckbutton", background=BG, foreground=TEXT, indicatorbackground=PANEL,
                    indicatorforeground=TEXT)
        s.map("TCheckbutton", background=[("active", BG)], indicatorbackground=[("selected", SELECT)])
        s.configure("Vertical.TScrollbar", background=RAISED, troughcolor=BG, arrowcolor=MUTED, bordercolor=BG)
        s.map("Vertical.TScrollbar", background=[("active", HOVER)])
        s.configure("TPanedwindow", background=BG)
        s.configure("Sash", sashthickness=px(6), gripcount=0, background=BORDER)

    def _dark_title_bar(self):
        if os.name != "nt":
            return
        try:
            import ctypes
            self.update_idletasks()
            hwnd = ctypes.windll.user32.GetParent(self.winfo_id())
            on = ctypes.c_int(1)
            for attr in (20, 19):  # DWMWA_USE_IMMERSIVE_DARK_MODE (newer / older Windows 10 builds)
                if ctypes.windll.dwmapi.DwmSetWindowAttribute(hwnd, attr, ctypes.byref(on), ctypes.sizeof(on)) == 0:
                    break
        except (AttributeError, OSError):
            pass

    def open_dialog(self):
        paths = filedialog.askopenfilenames(title="Open HWiNFO logs", initialdir=os.getcwd(),
                                            filetypes=[("HWiNFO CSV logs", "*.csv *.CSV"), ("All files", "*.*")])
        if paths:
            self.add_files(paths)

    def add_files(self, paths):
        self.configure(cursor="watch")
        self.update_idletasks()
        errors, added = [], None
        for p in paths:
            if any(os.path.samefile(p, l.path) for l in self.logs if os.path.exists(l.path)):
                continue
            try:
                added = hr.load_log(p)
                self.logs.append(added)
            except (OSError, ValueError) as e:
                errors.append(f"{os.path.basename(p)}: {e}")
        self.configure(cursor="")
        if errors:
            messagebox.showwarning("Some files couldn't be read", "\n".join(errors))
        if not self.logs:
            return
        self.logs.sort(key=lambda l: l.times[0])
        self.picker["values"] = [f"{os.path.basename(l.path)}   ·   {l.times[0]:%d %b %H:%M}   ·   "
                                 f"{hr.fmt_duration(l.duration)}" for l in self.logs]
        target = added or self.logs[0]
        self.picker.current(self.logs.index(target))
        self.show_current()

    def view(self, log):
        """The version of a log currently on screen (cleaned or as recorded)."""
        if not self.use_clean:
            return log
        if id(log) not in self.cleaned:
            self.cleaned[id(log)] = hr.clean_log(log)[0]
        return self.cleaned[id(log)]

    def show_current(self):
        if not self.logs:
            return
        self.configure(cursor="watch")
        self.update_idletasks()
        log = self.view(self.logs[self.picker.current()])
        self.compare.show_all([self.view(l) for l in self.logs])
        self.show_log(log)
        if self.use_clean:
            fixed = sum(log.cleaned.values())
            self.status.configure(fg=OK, text=f"✔ Cleaned view: {fixed} glitched reading(s) in {len(log.cleaned)} "
                                              f"sensor(s) repaired" if fixed else "✔ Cleaned view: nothing to repair")
            self.clean_btn.configure(text="Show original")
        else:
            self.status.configure(fg=MUTED, text="Showing readings as recorded")
            self.clean_btn.configure(text="Clean glitches")
        self.configure(cursor="")

    def toggle_clean(self):
        self.use_clean = not self.use_clean
        self.show_current()

    def save_csv(self):
        if not self.logs:
            return
        log = self.view(self.logs[self.picker.current()])
        stem = os.path.splitext(os.path.basename(log.path))[0]
        path = filedialog.asksaveasfilename(
            title="Save log as CSV", defaultextension=".csv", initialdir=os.path.dirname(os.path.abspath(log.path)),
            initialfile=f"{stem}_{'clean' if self.use_clean else 'tidy'}.csv",
            filetypes=[("CSV", "*.csv"), ("All files", "*.*")])
        if not path:
            return
        try:
            hr.export_csv(log, path)
        except OSError as e:
            messagebox.showerror("Couldn't save", str(e))
            return
        what = "cleaned" if self.use_clean else "original (tidied)"
        self.status.configure(fg=OK, text=f"✔ Saved {what} CSV to {os.path.basename(path)}")

    def show_log(self, log):
        glitches = glitch_sets(log)
        ctx = {
            "findings": hr.analyse(log),
            "limits": {col.name: lim for col, lim, _ in hr.temp_limits(log)},
            "glitches": glitches,
            "glitched": {c.name for cols in hr.glitch_moments(log).values() for c in cols},
            "checks": {ch.col.name: ch for ch in log.checks},
        }
        Chart.processes = log.processes
        for tab in (self.overview, self.throttle, self.sensors, self.spikes):
            tab.show(log, ctx)
        bad, warn = self.health.show(log, ctx)
        self.tabs.tab(self.health, text=f"Health checks  ✖{bad} !{warn}" if bad or warn else "Health checks  ✔")
        self.title(f"HWiNFO Log Reader - {os.path.basename(log.path)}")


def default_files():
    return sorted(set(glob.glob("*.csv") + glob.glob("*.CSV")), key=os.path.getmtime)


def run(files=None):
    if os.name == "nt":
        try:
            import ctypes
            ctypes.windll.shcore.SetProcessDpiAwareness(1)  # sharp text on high-DPI screens
        except (AttributeError, OSError):
            pass
    App(files if files else default_files()).mainloop()


if __name__ == "__main__":
    run(sys.argv[1:])
