# GeigerStation

A radiation-monitoring station built on an **Arduino Pro Mini** (firmware) and a **Raspberry Pi** (Python backend), connected over a serial Bluetooth bridge (dual HC-06 modules).

---

## Hardware

| Component | Details |
|-----------|---------|
| MCU | Arduino Pro Mini (ATmega328P, 3.3 V / 8 MHz) |
| Tube | SBM-20 (coefficient: 0.00662 CPM → µSv/h) |
| Display | 16×2 I²C LCD (PCF8574 @ 0x27) |
| Link | HC-06 Bluetooth bridge → `/dev/serial0` on Pi |
| Backend | Raspberry Pi (any model), Python 3.9+ |

---

## Repository Structure

```
GeigerStation/
├── GeigerFixed_V3/
│   └── GeigerFixed_V3.ino   # Arduino firmware (V3 — bug-fixed)
├── GeigerOptimized_Gemini_V2/
│   └── GeigerOptimized_Gemini_V2.ino  # Previous firmware (reference)
├── RadStation_v3.py          # Python backend (V3 — bug-fixed)
├── RadStation.py             # Previous backend (reference)
└── README.md
```

---

## Critical Bugs Fixed in V3

### Arduino Firmware
| # | Severity | Fix |
|---|----------|-----|
| 1 | **Critical** | `sendToPi()` — replaced `String` heap allocations with `dtostrf()` into a stack `char[]`. This eliminated the heap fragmentation that caused the **24–48 h freeze**. |
| 2 | High | `totalPulseCount` reads are now atomic (`cli/SREG/sei` guard) to prevent ISR-torn multi-byte reads. |
| 3 | High | `wdt_enable()` moved to the end of `setup()` to prevent an infinite boot-loop if the I²C LCD hangs during initialisation. |
| 4 | Medium | `millis()` rollover-safe comparison using unsigned subtraction (`currentMillis - nextResetTime < 0x80000000`). |
| 5 | Medium | `attachInterrupt` uses `digitalPinToInterrupt(2)` macro instead of the hard-coded `0`. |

### Python Backend
| # | Severity | Fix |
|---|----------|-----|
| 1 | Critical | `SerialReader` — added full reconnection loop; a Bluetooth bridge drop is now recovered automatically. |
| 2 | Critical | `float(val)` in the main loop is wrapped in `try/except ValueError`; a malformed packet no longer crashes the process. |
| 3 | High | All bare `except: pass` blocks replaced with specific exception types + `logging`. |
| 4 | High | `plt.close(fig)` moved to `finally` to prevent matplotlib figure memory leaks. |
| 5 | High | Telegram alert sends dispatched to daemon threads (`send_async`) so a slow network cannot block the main schedule loop. |
| 6 | Medium | `smart_cleanup` skips today's active CSV file to prevent a read/write race condition. |
| 7 | Medium | `do_report` error detection uses `res.endswith('.png')` — fixes a case-mismatch bug in the original. |

---

## Arduino Setup

### Dependencies (install via Arduino Library Manager)
- `EncButton` by AlexGyver
- `GyverFilters` by AlexGyver
- `LiquidCrystal_I2C` by Frank de Brabander

### Wiring
- Geiger tube pulse → **D2** (INT0, FALLING edge)
- Button → **D3**
- LCD SDA → **A4**, SCL → **A5**
- HC-06 TX → **D0 (RX)**, RX → **D1 (TX)**

---

## Python Setup

```bash
pip install pyserial schedule pandas numpy matplotlib scipy requests
```

### Configuration

Edit the top of `RadStation_v3.py` or set the environment variable:

```bash
export TELEGRAM_TOKEN="your_bot_token_here"
```

| Variable | Default | Description |
|----------|---------|-------------|
| `TELEGRAM_TOKEN` | env var | Bot token from @BotFather |
| `CHAT_IDS` | `["508873529"]` | Telegram chat IDs to notify |
| `LOG_PATH` | `/home/navenger/radstat/` | Directory for CSV logs |
| `SERIAL_PORT` | `/dev/serial0` | Serial device |
| `BAUD_RATE` | `19200` | Must match firmware |
| `ALERT_THRESHOLD` | `50.0` | µRh/h — triggers spike alert |

### Running as a systemd service

```ini
[Unit]
Description=RadStation Geiger Monitor
After=network.target

[Service]
ExecStart=/usr/bin/python3 /home/navenger/radstat/RadStation_v3.py
Restart=always
RestartSec=10
Environment="TELEGRAM_TOKEN=your_token_here"
User=navenger

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl enable radstation
sudo systemctl start radstation
```

---

## Telegram Bot Commands

| Command | Description |
|---------|-------------|
| `/status` | Live average reading |
| `/health` | Uptime + stream state |
| `/10mins` | 10-minute high-detail plot |
| `/hourly` | 60-minute trend plot |
| `/daily` | 24-hour macro plot |
| `/reboot` | Safe Pi reboot |
| `/help` | Command reference |

---

## License

MIT
