# hwinfo-reader

A simple Python reader for HWiNFO logs in sensor mode that can pinpoint issues with hardware.

It's a desktop app (and command-line tool) that turns [HWiNFO](https://www.hwinfo.com/) sensor logs into a readable health report: thermal throttling, temperatures vs. limits, power, clocks, fans, voltages, RAM, drives, and every other sensor HWiNFO recorded. With the optional process logger it also shows **which programs were running** at every warning, spike and throttling event.

It works with any PC HWiNFO supports: AMD or Intel CPUs, and NVIDIA, AMD Radeon or Intel Arc GPUs, on desktops or laptops.

- **Overview**: hardware, peak temperature / power / RAM cards, plain-language findings, temperature and power charts
- **Health checks**: every sensor compared with what's normal *for that component* (CPU Tjmax for your exact CPU model, GPU limits by vendor, ATX rail tolerances, SSD wear, fan stops, WHEA errors and more)
- **Throttling**: every limit flag with a timeline and an event list
- **All sensors**: searchable, grouped by type, with min / avg / P99 / max; click to chart and hover for exact values
- **Spikes & glitches**: finds HWiNFO misreads (e.g. a whole group of CPU sensors briefly doubling) and separates them from real spikes
- **Clean glitches**: one button repairs misread samples; **Save CSV…** exports a tidy UTF-8 CSV
- **Compare logs**: several logs side by side
- **Which programs were running**: with `process_logger.py` running alongside HWiNFO, every warning, spike and throttling event names the busiest programs at that moment (CPU, GPU, disk and RAM use per program), and chart hovers show them too
- Dark theme, high-DPI aware. The reader itself is standard library only; the optional process logger needs `psutil`

## Requirements

- Python 3.8 or newer ([python.org](https://www.python.org/downloads/); tick *"Add python.exe to PATH"* on Windows). tkinter is included with the python.org installers.
- HWiNFO (free) to record the logs.
- Optional, for the process logger: `pip install -r requirements.txt` (just `psutil`).

## Getting started

```
git clone https://github.com/shbii/hwinfo-reader.git
cd hwinfo-reader
python hwinfo_reader.py                 # opens the window with every .csv in the current folder
python hwinfo_reader.py path\to\log.CSV # open specific logs
```

On Windows you can also double-click **`HWiNFO Log Reader.pyw`**. It opens the window without a console and loads every `.csv` in its folder; use **Open logs…** for logs stored elsewhere.

Terminal mode:

```
python hwinfo_reader.py --text                  # full report in the terminal
python hwinfo_reader.py --text --clean          # same, with glitched readings repaired
python hwinfo_reader.py --export cleaned        # also write cleaned CSVs into ./cleaned
python hwinfo_reader.py --help
```

## Recording a good log with HWiNFO

1. Start HWiNFO and choose **Sensors-only**.
2. Make sure the sensors you care about are visible. HWiNFO only logs what is shown. Hidden sensors (right-click → *Hide*) and disabled groups are left out of the log. If in doubt, right-click → *Show all*/*Enable* everything; this app sorts it out.
3. Click the **Logging start** button (the green ＋ icon at the bottom of the Sensors window), pick where to save the `.CSV`, then use the PC normally or run your game or stress test.
4. Click the button again to stop, then open the CSV with this app.

### See which programs caused a problem (optional)

HWiNFO doesn't record what was running, so run the process logger at the same time:

```
pip install -r requirements.txt
python process_logger.py --out "C:\path\to\your\hwinfo\logs"
```

It writes `processes_<date>_<time>.csv` every 2 seconds, holding each busy program's CPU %, GPU % (from Windows' GPU counters, like Task Manager), RAM and disk use. Stop it with Ctrl+C when you stop HWiNFO logging. Keep it **in the same folder as the HWiNFO log**: the reader matches the two by time automatically, and then:

- Health checks list the busiest programs at each warning (e.g. *"GPU hot spot reached 98 °C at 21:14:06, busiest programs: game.exe (GPU 97%, CPU 12%)"*)
- Throttling events and spikes show the programs running when they started
- Hovering any chart shows the top programs at that moment

Tips:
- The default 2 second polling interval is fine. Faster polling makes bigger files and more misreads.
- Close other monitoring tools while logging (Ryzen Master, AMD Adrenalin's metrics overlay, OCCT, AIDA64, vendor RGB/fan apps and so on). Several programs reading the same chips is the usual cause of misread "glitch" samples.

## How the health checks work

Checks run on glitch-cleaned data, so a single misread can't raise an alarm. A sensor is a **warning** when its 99th-percentile reading crosses the warning level (or it crosses the critical level briefly), and **critical** when it sits at the critical level. Some examples:

| Component | Normal | Critical | Notes |
|---|---|---|---|
| CPU temperature | below Tjmax − 10 °C | Tjmax | Tjmax picked per model: Ryzen 95 °C, 7000X3D 89 °C, 5000X3D 90 °C, Intel 100 °C |
| GPU core | below the card's limit − 4 °C | its limit | uses the limit HWiNFO reports; otherwise typical NVIDIA / AMD / Intel values |
| GPU hot spot | < 95 °C (Radeon < 100 °C) | 105 °C (Radeon 110 °C) | |
| VRM | < 100 °C | 115 °C | |
| NVMe SSD | < 70 °C | 80 °C | plus remaining life / spare and failure flags |
| DDR5 module | < 65 °C | 80 °C | |
| +12 / +5 / +3.3 V rails | ±5 % (ATX spec) | ±8 % | readings far off nominal are reported as "probably not connected" |
| CMOS battery | > 2.9 V | 2.7 V | |
| CPU core voltage | < 1.40 V (Intel 1.50 V) | 1.50 V (Intel 1.55 V) | after glitch cleaning |
| Fans | spinning | CPU fan / pump stopping | GPU 0 RPM idle mode is treated as normal |
| RAM / VRAM use | < 90 % / < 95 % | 97 % / 99 % | |
| WHEA hardware errors | 0 | any | usually an unstable overclock or memory setting |

Sensors with no meaningful fixed limit (clocks, loads, frame rates and so on) are still checked for impossible values, spikes and misreads.

All rules live in `RULES` in `hwinfo_reader.py`. Each one is a matcher, a kind of check and the limits, so adding or adjusting one is a one-line change. PRs with better numbers for specific hardware are welcome.

## Using it as a library

```python
from hwinfo_reader import load_log, clean_log

log = load_log("log.CSV")
print(log.hardware())                       # {'CPU': 'AMD Ryzen 7 9800X3D', 'GPU': ...}
cpu = log.column("CPU (Tctl/Tdie)")
print(cpu.stats["max"], cpu.stats["p99"])
for check in log.checks:                    # one health check per sensor
    if check.level in ("warn", "bad"):
        print(check.message)
clean, fixed = clean_log(log)               # copy with misreads repaired
```

## Development

```
python -m unittest discover tests
```

The tests build small synthetic HWiNFO logs (Intel, Ryzen X3D, Radeon, °F, day-first dates, misreads), so they need no real log files.

Files:

| File | What it is |
|---|---|
| `hwinfo_reader.py` | parsing, analysis, health checks, glitch cleaning, terminal report, CLI |
| `hwinfo_gui.py` | the tkinter window |
| `process_logger.py` | optional per-program CPU/GPU/RAM/disk logger (needs `psutil`) |
| `HWiNFO Log Reader.pyw` | double-click launcher for Windows |
| `tests/` | unit tests |

## Privacy

HWiNFO logs contain your hardware model names and what you were running (e.g. the PresentMon app name), and process logs list every busy program by name. Think twice before attaching real logs to public issues. The `.gitignore` keeps `.csv` files out of the repository.

## Disclaimer

Not affiliated with or endorsed by HWiNFO or REALiX. "HWiNFO" is used only to describe the log format this tool reads. The health limits are general guidance, not manufacturer specifications. When in doubt, check your component's datasheet.

## License

GPL-3.0, see [LICENSE](LICENSE).
