"""Tests for hwinfo_reader using small synthetic HWiNFO logs.

    python -m unittest discover tests
"""

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import hwinfo_reader as hr  # noqa: E402


def write_log(columns, rows, dates=None, times=None, encoding="latin-1", folder=None):
    """columns: [(name, group)], rows: [[value, ...]] -> path of an HWiNFO-style CSV.
    Default timestamps: 25 Oct 2026 01:00:00 + 2 s per row."""
    n = len(rows)
    dates = dates or ["25.10.2026"] * n
    times = times or [f"1:{i // 30}:{(i * 2) % 60}.000" for i in range(n)]
    header = ["Date", "Time"] + [name for name, _ in columns] + [""]
    groups = ["", ""] + [group for _, group in columns] + [""]
    lines = [",".join(f'"{h}"' if h else "" for h in header)]
    for d, t, r in zip(dates, times, rows):
        lines.append(",".join([d, t] + [str(v) for v in r] + [""]))
    lines.append(lines[0])
    lines.append(",".join(groups))
    fd, path = tempfile.mkstemp(suffix=".CSV", dir=folder)
    with os.fdopen(fd, "w", encoding=encoding, newline="") as f:
        f.write("\r\n".join(lines) + "\r\n")
    return path


INTEL = "CPU [#0]: Intel Core i9-14900K: DTS"
NVIDIA = "GPU [#0]: NVIDIA GeForce RTX 4090: ASUS"
RYZEN_7000X3D = "CPU [#0]: AMD Ryzen 7 7800X3D: Enhanced"
RADEON = "GPU [#0]: AMD Radeon RX 7900 XTX: Sapphire"
BOARD = "ASUS ROG STRIX Z790-E (Nuvoton NCT6798D)"


class LogCase(unittest.TestCase):
    def tearDown(self):
        for p in getattr(self, "paths", []):
            os.remove(p)

    def load(self, *args, **kw):
        path = write_log(*args, **kw)
        self.paths = getattr(self, "paths", []) + [path]
        return hr.load_log(path)


class ParsingTests(LogCase):
    def test_basic_layout(self):
        log = self.load([("CPU Package [°C]", INTEL), ("Thermal Throttling [Yes/No]", INTEL)],
                        [[50 + i % 5, "No"] for i in range(40)])
        self.assertEqual(len(log.times), 40)
        temp = log.column("CPU Package")
        self.assertEqual(temp.unit, "°C")
        self.assertEqual(temp.device, "CPU")
        self.assertTrue(log.column("Thermal Throttling").is_flag)

    def test_duplicate_names_and_unpadded_time(self):
        log = self.load([("Power [W]", NVIDIA), ("Power [W]", NVIDIA)], [[1, 2]] * 3,
                        times=["2:6:37.350", "2:6:39.350", "2:6:41.350"])
        self.assertEqual(log.times[0].minute, 6)
        self.assertEqual([c.name for c in log.columns], ["Power [W]", "Power #2 [W]"])

    def test_day_first_slash_dates_and_am_pm(self):
        log = self.load([("CPU Package [°C]", INTEL)], [[50]] * 2,
                        dates=["25/10/2026", "25/10/2026"], times=["1:00:00.000 PM", "1:00:02.000 PM"])
        self.assertEqual((log.times[0].day, log.times[0].month, log.times[0].hour), (25, 10, 13))

    def test_us_dates(self):
        log = self.load([("CPU Package [°C]", INTEL)], [[50]] * 2, dates=["10/25/2026", "10/25/2026"])
        self.assertEqual((log.times[0].day, log.times[0].month), (25, 10))

    def test_fahrenheit_converted(self):
        log = self.load([("CPU Package [°F]", INTEL)], [[212]] * 3)
        col = log.column("CPU Package")
        self.assertEqual(col.unit, "°C")
        self.assertAlmostEqual(col.values[0], 100.0)

    def test_utf8_log(self):
        log = self.load([("CPU Package [°C]", INTEL)], [[50]] * 3, encoding="utf-8")
        self.assertEqual(log.column("CPU Package").unit, "°C")


class HardwareTests(LogCase):
    def test_tjmax_by_cpu(self):
        for group, expected in ((INTEL, 100), (RYZEN_7000X3D, 89), ("CPU [#0]: AMD Ryzen 7 9800X3D", 95),
                                ("CPU [#0]: AMD Ryzen 7 5800X3D", 90), ("CPU [#0]: AMD Ryzen 9 7950X", 95)):
            log = self.load([("CPU Package [°C]", group)], [[50]] * 3)
            self.assertEqual(hr.cpu_tjmax(log)[0], expected, group)

    def test_7800x3d_warns_where_9800x3d_would_not(self):
        log = self.load([("CPU (Tctl/Tdie) [°C]", RYZEN_7000X3D)], [[86]] * 50)
        check = next(ch for ch in log.checks if ch.col.label == "CPU (Tctl/Tdie)")
        self.assertEqual(check.level, "warn")   # 89 °C limit -> warn from 79 °C

    def test_radeon_hot_spot_uses_amd_limits(self):
        log = self.load([("GPU Hot Spot Temperature [°C]", RADEON)], [[98]] * 50)
        self.assertEqual(next(ch for ch in log.checks).level, "ok")  # 98 is normal for a Radeon junction
        log = self.load([("GPU Hot Spot Temperature [°C]", NVIDIA)], [[98]] * 50)
        self.assertEqual(next(ch for ch in log.checks).level, "warn")

    def test_intel_tvb_is_not_throttling(self):
        log = self.load([("IA: Thermal Velocity Boost [Yes/No]", INTEL), ("IA: PROCHOT [Yes/No]", INTEL)],
                        [["Yes", "No"]] * 5)
        flags = {c.label: hr.is_thermal_flag(c) for c in log.columns}
        self.assertEqual(flags, {"IA: Thermal Velocity Boost": False, "IA: PROCHOT": True})

    def test_rails_and_fans(self):
        rows = [[11.2, 1200 if i % 10 else 0, 3.0] for i in range(50)]
        log = self.load([("+12V [V]", BOARD), ("CPU [RPM]", BOARD), ("VBAT [V]", BOARD)], rows)
        levels = {ch.col.label: ch.level for ch in log.checks}
        self.assertEqual(levels["+12V"], "warn")     # below 11.4 V (ATX -5%)
        self.assertEqual(levels["CPU"], "bad")       # CPU fan stopping is critical
        self.assertEqual(levels["VBAT"], "ok")

    def test_second_gpu_is_checked(self):
        log = self.load([("GPU Temperature [°C]", "GPU [#1]: AMD Radeon Graphics")], [[99]] * 50)
        self.assertEqual(log.columns[0].device, "GPU #1")
        self.assertEqual(next(ch for ch in log.checks).level, "warn")


class GlitchTests(LogCase):
    def test_doubled_readings_are_cleaned(self):
        group = "CPU [#0]: AMD Ryzen 7 9800X3D: Enhanced"
        cols = [("FCLK [MHz]", group), ("SoC Voltage [V]", group), ("PPT [W]", group), ("Die [°C]", group),
                ("Core Power [W]", group), ("Tctl [°C]", group)]
        rows = []
        for i in range(200):
            row = [2067, 1.25, 80 + i % 7, 55 + i % 3, 40 + i % 5, 60]
            if i in (50, 120):
                row = [v * 2 for v in row[:5]] + [60]   # HWiNFO misread: everything doubles at once
            if i == 160:
                row = [v * 1.5 for v in row[:5]] + [60]
            rows.append(row)
        log = self.load(cols, rows)
        clean, report = hr.clean_log(log)
        self.assertEqual(clean.column("FCLK").stats["max"], 2067)
        self.assertLess(clean.column("Die").stats["max"], 60)
        self.assertNotIn(clean.column("Tctl").name, report)   # untouched sensor stays untouched

    def test_real_load_step_is_not_cleaned(self):
        group = "CPU [#0]: AMD Ryzen 7 9800X3D: Enhanced"
        cols = [(f"Core {k} Power [W]", group) for k in range(6)] + [("FCLK [MHz]", group)]
        rows = [[5 if i < 100 else 15] * 6 + [2067] for i in range(200)]   # sustained load, fixed FCLK
        log = self.load(cols, rows)
        self.assertEqual(hr.find_irregular(log), {})


class ProcessLogTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.mkdtemp()

    def tearDown(self):
        for name in os.listdir(self.folder):
            os.remove(os.path.join(self.folder, name))
        os.rmdir(self.folder)

    def write_processes(self, name, samples):
        """samples: {seconds after 01:00:00: [(name, pid, cpu, ram, read, write, gpu)]}"""
        with open(os.path.join(self.folder, name), "w", encoding="utf-8", newline="") as f:
            f.write("Timestamp,PID,Name,CPU [%],RAM [MB],Disk Read [MB/s],Disk Write [MB/s],GPU [%]\n")
            for sec, procs in sorted(samples.items()):
                stamp = f"2026-10-25 01:{sec // 60:02d}:{sec % 60:02d}.100"
                for pname, pid, cpu, ram, rd, wr, gpu in procs:
                    f.write(f"{stamp},{pid},{pname},{cpu},{ram},{rd},{wr},{gpu}\n")

    def test_warning_names_busiest_program(self):
        temps = [70] * 100
        for i in range(40, 45):
            temps[i] = 99 - (i == 42)   # hot spot peaks at sample 40 (01:01:20)
        log_path = write_log([("GPU Hot Spot Temperature [°C]", NVIDIA)], [[t] for t in temps], folder=self.folder)
        samples = {s: [("explorer.exe", 10, 1.0, 200, 0, 0, 0.5), ("chrome.exe", 20, 3.0, 900, 0, 0, 2.0)]
                   for s in range(0, 200, 2)}
        samples[80] = [("explorer.exe", 10, 1.0, 200, 0, 0, 0.5), ("game.exe", 30, 12.0, 4000, 0, 0, 97.0)]
        self.write_processes("processes_2026-10-25_01-00-00.csv", samples)
        log = hr.load_log(log_path)
        self.assertIsNotNone(log.processes)
        check = next(ch for ch in log.checks if ch.level == "warn")
        self.assertEqual(log.times[check.at].strftime("%H:%M:%S"), "01:01:20")
        self.assertTrue(check.programs.startswith("game.exe (GPU 97%"), check.programs)

    def test_unrelated_process_log_ignored(self):
        log_path = write_log([("CPU Package [°C]", INTEL)], [[50]] * 10, folder=self.folder)
        self.write_processes("processes_2026-10-26_09-00-00.csv", {0: [("x.exe", 1, 1, 1, 0, 0, 0)]})
        self.assertIsNone(hr.load_log(log_path).processes)


if __name__ == "__main__":
    unittest.main()
