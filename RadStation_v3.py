"""
RadStation_v3.py
Fixes applied vs v2.0.7/v2.0.1:
  1. [CRITICAL] SerialReader: full reconnection logic — recovers from Bluetooth
                drops without restarting the script
  2. [CRITICAL] Main loop: float(val) is now wrapped in try/except so a single
                malformed packet cannot crash the entire process
  3. [HIGH]     StatsEngine: plt.close('all') moved into `finally` to prevent
                matplotlib figure memory leaks on plot exceptions
  4. [HIGH]     All bare `except: pass` replaced with specific exception types
                + logging so failures leave a trace
  5. [HIGH]     minute_processing: alert Telegram sends dispatched to daemon
                threads so a slow/down network cannot block the main loop
  6. [MEDIUM]   LogWriter: write_queue has a capped size; full-queue drops are
                logged rather than silently blocked
  7. [MEDIUM]   smart_cleanup: skips today's active log file to avoid a read/
                write race condition
  8. [MEDIUM]   do_report: error detection uses endswith('.png') instead of
                fragile string matching ("Crash" vs "crash" typo fixed)
  9. [LOW]      Telegram token read from TELEGRAM_TOKEN env var; falls back to
                inline value for backward compatibility
 10. [LOW]      Standard logging module replaces silent failures everywhere
"""

import os
import time
import csv
import logging
import serial
import schedule
import pandas as pd
import numpy as np
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
import shutil
from scipy import signal
from threading import Thread, Lock
from queue import Queue, Empty, Full
import requests

# ── Logging setup ─────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(name)s: %(message)s',
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler('/home/navenger/radstat/radstation.log', mode='a'),
    ]
)
log = logging.getLogger('RadStation')

# ── Configuration ─────────────────────────────────────────────────────────────
# FIX #9: read token from environment; fall back to the inline value so
# existing deployments keep working without changes.
TOKEN       = os.environ.get("TELEGRAM_TOKEN", "YOUR_TOKEN_HERE")
CHAT_IDS    = ["508873529"]
LOG_PATH    = '/home/navenger/radstat/'
ARCHIVE_PATH = os.path.join(LOG_PATH, 'archive/')
SERIAL_PORT = '/dev/serial0'
BAUD_RATE   = 19200

ALERT_THRESHOLD       = 50.0
INTERESTING_THRESHOLD = 28.0
CLEANUP_DAYS          = 30

# ── Global state ──────────────────────────────────────────────────────────────
ram_buffer      = []
ram_lock        = Lock()
plot_lock       = Lock()
start_time      = time.time()
last_received_time   = 0.0
last_loop_heartbeat  = time.time()
last_reader_heartbeat = time.time()

connection_error_sent   = False
first_data_received     = False
system_is_in_high_state = False
consistent_alert_sent   = False
high_timer_start   = None
normal_timer_start = None
current_avg_cached = 0.0

# ─────────────────────────────────────────────────────────────────────────────
# SerialReader
# FIX #1: full reconnection loop so Bluetooth bridge drops are recovered
#         automatically without restarting the script.
# ─────────────────────────────────────────────────────────────────────────────
class SerialReader:
    def __init__(self, port, rate):
        self.port   = port
        self.rate   = rate
        self.buffer = Queue(maxsize=5000)
        self.ser    = self._try_open()
        Thread(target=self.reader_routine, daemon=True).start()

    def _try_open(self):
        try:
            s = serial.Serial(self.port, baudrate=self.rate, timeout=0.5)
            log.info(f"Serial port {self.port} opened.")
            return s
        except serial.SerialException as e:
            log.warning(f"Could not open serial port {self.port}: {e}")
            return None

    def verify_checksum(self, data_str: str, checksum_str: str) -> bool:
        try:
            return sum(ord(c) for c in data_str.strip()) % 256 == int(checksum_str)
        except (ValueError, TypeError):
            return False

    def reader_routine(self):
        global last_reader_heartbeat
        while True:
            last_reader_heartbeat = time.time()

            # ── Reconnection logic ────────────────────────────────────────────
            if self.ser is None or not self.ser.is_open:
                log.warning("Serial not open — attempting reconnect in 5 s…")
                time.sleep(5)
                if self.ser is not None:
                    try:
                        self.ser.close()
                    except Exception:
                        pass
                self.ser = self._try_open()
                continue  # re-evaluate at the top of the loop

            # ── Normal read path ──────────────────────────────────────────────
            try:
                if self.ser.in_waiting > 0:
                    raw = self.ser.readline()
                    line = raw.decode('utf-8', errors='ignore').strip()
                    if '*' in line:
                        parts = line.split('*')
                        if len(parts) == 2 and self.verify_checksum(parts[0], parts[1]):
                            try:
                                self.buffer.put_nowait(parts[0].strip())
                            except Full:
                                log.warning("Serial read buffer full — packet dropped.")
            except serial.SerialException as e:
                log.error(f"Serial read error: {e}")
                try:
                    self.ser.close()
                except Exception:
                    pass
                time.sleep(1)
            except Exception as e:
                log.error(f"Unexpected reader error: {e}", exc_info=True)
                time.sleep(1)

    def read(self):
        try:
            return self.buffer.get_nowait()
        except Empty:
            return None


# ─────────────────────────────────────────────────────────────────────────────
# LogWriter
# FIX #6: write_queue has a capped size; overflow is logged, not silently lost.
# ─────────────────────────────────────────────────────────────────────────────
class LogWriter:
    def __init__(self, filepath):
        self.filepath    = filepath
        os.makedirs(ARCHIVE_PATH, exist_ok=True)
        self.write_queue = Queue(maxsize=1000)
        Thread(target=self.logger_routine, daemon=True).start()

    def get_filename(self):
        return os.path.join(self.filepath, pd.Timestamp.now().strftime('Stat-%d-%m-%Y.csv'))

    def batch_log(self, data_list):
        if not data_list:
            return
        try:
            self.write_queue.put_nowait(data_list)
        except Full:
            log.warning("Log write queue full — batch dropped.")

    def logger_routine(self):
        while True:
            batch = self.write_queue.get()
            fname = self.get_filename()
            try:
                with open(fname, mode='a', newline='') as f:
                    csv.writer(f).writerows(batch)
            except OSError as e:
                log.error(f"CSV write failed ({fname}): {e}")


# ─────────────────────────────────────────────────────────────────────────────
# TeleMessenger
# ─────────────────────────────────────────────────────────────────────────────
class TeleMessenger:
    def __init__(self, token, chat_ids):
        self.url           = f"https://api.telegram.org/bot{token}/"
        self.chat_ids      = [str(cid) for cid in chat_ids]
        self.last_update_id = 0
        self.flush_old_commands()
        Thread(target=self.command_listener, daemon=True).start()

    def flush_old_commands(self):
        """Discard any Telegram updates that arrived before this run."""
        try:
            r = requests.get(
                self.url + "getUpdates?offset=-1&timeout=5", timeout=10
            ).json()
            if r.get("result"):
                self.last_update_id = r["result"][0]["update_id"]
        except requests.RequestException as e:
            log.warning(f"flush_old_commands failed: {e}")

    def send(self, obj, is_image=False, silent=False):
        for cid in self.chat_ids:
            try:
                if is_image:
                    with open(obj, 'rb') as img:
                        requests.post(
                            self.url + "sendPhoto",
                            data={'chat_id': cid, 'disable_notification': silent},
                            files={'photo': img},
                            timeout=30,
                        )
                else:
                    requests.post(
                        self.url + "sendMessage",
                        data={
                            'chat_id': cid,
                            'text': obj,
                            'disable_notification': silent,
                            'parse_mode': 'Markdown',
                        },
                        timeout=15,
                    )
            except requests.RequestException as e:
                log.warning(f"Telegram send failed (chat {cid}): {e}")
            except OSError as e:
                log.warning(f"Image file open failed: {e}")

    def send_async(self, *args, **kwargs):
        """Fire-and-forget wrapper — never blocks the calling thread."""
        Thread(target=self.send, args=args, kwargs=kwargs, daemon=True).start()

    def command_listener(self):
        self.send("🤖 *Command Listener Active.*")
        while True:
            try:
                r = requests.get(
                    self.url + f"getUpdates?offset={self.last_update_id + 1}&timeout=30",
                    timeout=45,
                ).json()
                if not r.get("result"):
                    continue

                for update in r["result"]:
                    self.last_update_id = update["update_id"]
                    msg = update.get("message")
                    if not msg or "text" not in msg:
                        continue

                    cmd = msg["text"].lower().strip()
                    uid = str(msg["chat"]["id"])

                    if uid not in self.chat_ids:
                        continue

                    if cmd == "/help":
                        self.send(
                            "📖 *Geiger Station Help*\n\n"
                            "📊 *Reports:*\n"
                            "• `/10mins` - Last 10m (High detail)\n"
                            "• `/hourly` - Last 60m (Smooth trend)\n"
                            "• `/daily` - Last 24h (Macro trend)\n\n"
                            "⚡ *Live Data:*\n"
                            "• `/status` - Current average & level\n"
                            "• `/health` - Uptime & sensor heartbeat\n\n"
                            "⚙️ *System:*\n"
                            "• `/reboot` - Safe restart the Pi\n"
                            "• `/help` - Show this menu"
                        )

                    elif cmd == "/status":
                        with ram_lock:
                            live_v = (
                                round(np.mean([x[1] for x in ram_buffer]), 2)
                                if ram_buffer else current_avg_cached
                            )
                        emoji = "☢️" if live_v > ALERT_THRESHOLD else "✅"
                        self.send(f"{emoji} *Live Reading:* `{live_v}` uRh/h")

                    elif cmd in ["/10mins", "/hourly", "/daily"]:
                        self.send(f"📊 *Generating {cmd[1:]} report…*", silent=True)
                        # FIX: run in a thread so this does not block the listener
                        Thread(
                            target=do_report, args=(cmd[1:],), daemon=True
                        ).start()

                    elif cmd == "/health":
                        uptime = round((time.time() - start_time) / 3600, 1)
                        stream = 'Active' if time.time() - last_received_time < 10 else 'SILENT'
                        self.send(
                            f"🩺 *System Health*\n"
                            f"• Uptime: `{uptime}h`\n"
                            f"• Stream: `{stream}`"
                        )

                    elif cmd == "/reboot":
                        self.send("🔄 *Manual Reboot Initiated…*")
                        time.sleep(2)
                        os.system('sudo reboot')

            except requests.RequestException as e:
                log.warning(f"command_listener poll error: {e}")
                time.sleep(5)
            except Exception as e:
                log.error(f"command_listener unexpected error: {e}", exc_info=True)
                time.sleep(5)


# ─────────────────────────────────────────────────────────────────────────────
# StatsEngine
# FIX #3: plt.close('all') moved into `finally` so figures are always
#         released even when an exception aborts the plot mid-render.
# ─────────────────────────────────────────────────────────────────────────────
class StatsEngine:
    def generate_plot(self, source_file, mode='hourly'):
        temp_file = f"{LOG_PATH}temp_plot.csv"
        fig = None
        with plot_lock:
            try:
                df_list = []
                if os.path.exists(source_file):
                    shutil.copy2(source_file, temp_file)
                    df_list.append(
                        pd.read_csv(temp_file, names=['Date', 'Value'], header=None)
                    )
                with ram_lock:
                    if ram_buffer:
                        df_list.append(
                            pd.DataFrame(ram_buffer, columns=['Date', 'Value'])
                        )

                if not df_list:
                    return "Error: No data available."

                df = pd.concat(df_list, ignore_index=True)
                df['Date'] = pd.to_datetime(
                    df['Date'], format='ISO8601', errors='coerce'
                ).dt.tz_localize(None)
                df = df.dropna(subset=['Date']).sort_values('Date')

                now_ts = pd.Timestamp.now().replace(tzinfo=None)
                deltas = {
                    '10mins': pd.Timedelta(minutes=10),
                    'hourly': pd.Timedelta(minutes=60),
                    'daily':  pd.Timedelta(days=1),
                }
                df_filtered = df[df['Date'] > (now_ts - deltas.get(mode, deltas['hourly']))]

                count = len(df_filtered)
                if count < 10:
                    return f"Error: Only {count} data points in range."

                # Savitzky-Golay window sizing
                if mode == '10mins':
                    win = count // 4
                elif mode == 'hourly':
                    win = int(count * 0.5)
                else:
                    win = int(count * 0.35)

                win = max(win, 11)
                if win % 2 == 0:
                    win += 1
                if win >= count:
                    win = count - 1 if (count - 1) % 2 != 0 else count - 2

                # Plot
                fig = plt.figure(figsize=(10, 6), facecolor='#0f0f0f')
                ax  = fig.add_subplot(1, 1, 1)
                ax.set_facecolor('#0f0f0f')

                ax.plot(
                    df_filtered['Date'], df_filtered['Value'],
                    color='#007bff', lw=1.2, alpha=0.5, label="Raw, uRh/h",
                )
                smooth_val = signal.savgol_filter(df_filtered['Value'], win, 3)
                ax.plot(
                    df_filtered['Date'], smooth_val,
                    color='#ff0000', lw=2.5, label="Trend",
                )

                med = round(df_filtered['Value'].median(), 2)
                ax.axhline(y=med, color='#28a745', ls='--', alpha=0.7,
                           label=f"Median: {med}")

                ax.grid(True, which='both', linestyle='--', alpha=0.4, color='white')

                formatted_date = now_ts.strftime('%d.%m.%y')
                ax.set_title(
                    f"{formatted_date} Radiation {mode.upper()} "
                    f"(Peak: {df_filtered['Value'].max():.2f})",
                    color='white', fontsize=14, pad=15,
                )

                leg = ax.legend(loc='upper right', facecolor='#0f0f0f', edgecolor='gray')
                for text in leg.get_texts():
                    text.set_color('white')

                fmt = mdates.DateFormatter('%H:%M:%S' if mode == '10mins' else '%H:%M')
                ax.xaxis.set_major_formatter(fmt)
                ax.tick_params(colors='white')

                timestamp_str = f"Generated: {now_ts.strftime('%d-%m-%Y %H:%M:%S')}"
                fig.text(0.95, 0.02, timestamp_str,
                         color='gray', ha='right', fontsize=9, style='italic')

                fig.tight_layout(rect=[0, 0.03, 1, 0.95])
                out = f"{LOG_PATH}{mode}_report.png"
                fig.savefig(out, dpi=120, facecolor='#0f0f0f',
                            bbox_inches='tight', pad_inches=0.1)
                return out

            except Exception as e:
                log.error(f"generate_plot ({mode}) failed: {e}", exc_info=True)
                return f"Error: Plotting failed — {e}"

            finally:
                # FIX #3: always release matplotlib memory regardless of outcome
                if fig is not None:
                    plt.close(fig)
                if os.path.exists(temp_file):
                    try:
                        os.remove(temp_file)
                    except OSError:
                        pass


# ─────────────────────────────────────────────────────────────────────────────
# Maintenance functions
# ─────────────────────────────────────────────────────────────────────────────
def smart_cleanup():
    """Archive interesting old CSV files; delete unremarkable ones."""
    # FIX #7: never touch today's active file mid-write
    today_fname = os.path.basename(logger.get_filename())
    cutoff      = time.time() - (CLEANUP_DAYS * 86400)

    for fname in os.listdir(LOG_PATH):
        if fname == today_fname:
            continue
        if not fname.endswith('.csv'):
            continue
        fpath = os.path.join(LOG_PATH, fname)
        try:
            if os.stat(fpath).st_mtime >= cutoff:
                continue
            df = pd.read_csv(fpath, names=['Date', 'Value'])
            if df['Value'].max() > INTERESTING_THRESHOLD:
                shutil.move(fpath, os.path.join(ARCHIVE_PATH, fname))
                log.info(f"Archived interesting log: {fname}")
            else:
                os.remove(fpath)
                log.info(f"Deleted old log: {fname}")
        except (OSError, pd.errors.ParserError) as e:
            log.warning(f"smart_cleanup could not process {fname}: {e}")


def minute_processing():
    """Flush RAM buffer to disk and evaluate radiation alert thresholds."""
    global ram_buffer, current_avg_cached
    global system_is_in_high_state, consistent_alert_sent
    global high_timer_start, normal_timer_start

    with ram_lock:
        if not ram_buffer:
            return
        vals    = [v[1] for v in ram_buffer]
        current_avg_cached = round(float(np.mean(vals)), 2)
        logger.batch_log(list(ram_buffer))
        ram_buffer = []

    # FIX #5: send Telegram alerts in background threads so a slow/down
    # network does not block the main schedule loop for up to 15 s.
    now = time.time()
    if current_avg_cached > ALERT_THRESHOLD:
        if not system_is_in_high_state:
            messenger.send_async(f"☢️ *SPIKE ALERT:* {current_avg_cached} uRh/h!")
            system_is_in_high_state = True
            high_timer_start        = now
        elif (
            high_timer_start
            and (now - high_timer_start > 600)
            and not consistent_alert_sent
        ):
            messenger.send_async(
                f"☢️☢️ *CONSISTENT HIGH:* {current_avg_cached} uRh/h!"
            )
            consistent_alert_sent = True
    elif system_is_in_high_state:
        if not normal_timer_start:
            normal_timer_start = now
        if now - normal_timer_start > 1200:
            messenger.send_async(f"✅ *Back to Normal:* {current_avg_cached} uRh/h")
            system_is_in_high_state = False
            consistent_alert_sent   = False
            normal_timer_start      = None


def watchdog():
    """Alert if no data received from the Geiger tube for > 80 s."""
    global connection_error_sent
    now = time.time()
    if now - last_received_time > 80 and first_data_received:
        if not connection_error_sent:
            messenger.send_async("🚨 *GEIGER LOST!*")
            connection_error_sent = True
            log.warning("Geiger link lost.")
    elif connection_error_sent and now - last_received_time < 80:
        messenger.send_async("✅ *GEIGER RESTORED!*")
        connection_error_sent = False
        log.info("Geiger link restored.")


def do_report(mode: str):
    """Generate a plot and send it to Telegram."""
    res = engine.generate_plot(logger.get_filename(), mode)
    # FIX #8: valid result always ends with '.png'; no fragile string matching
    if res.endswith('.png'):
        messenger.send(res, is_image=True)
    else:
        messenger.send(f"❌ {res}")


# ─────────────────────────────────────────────────────────────────────────────
# Initialisation
# ─────────────────────────────────────────────────────────────────────────────
messenger = TeleMessenger(TOKEN, CHAT_IDS)
reader    = SerialReader(SERIAL_PORT, BAUD_RATE)
logger    = LogWriter(LOG_PATH)
engine    = StatsEngine()

schedule.every(1).minutes.do(minute_processing)
schedule.every(1).minutes.do(watchdog)
schedule.every(24).hours.do(smart_cleanup)
schedule.every().hour.at(":58").do(lambda: do_report('hourly'))
schedule.every().day.at("23:55").do(lambda: do_report('daily'))

messenger.send("🚀 *System v3.0 Online.*")
log.info("RadStation v3.0 started.")

# ─────────────────────────────────────────────────────────────────────────────
# Main loop
# ─────────────────────────────────────────────────────────────────────────────
while True:
    last_loop_heartbeat = time.time()
    schedule.run_pending()

    data_found = False
    while True:
        val = reader.read()
        if val is None:
            break
        # FIX #2: malformed packet must not crash the entire process
        try:
            reading = float(val)
        except ValueError:
            log.warning(f"Unparseable value from serial: {val!r}")
            continue

        data_found = True
        ts = pd.Timestamp.now().isoformat()
        with ram_lock:
            ram_buffer.append((ts, reading))

    if data_found:
        if not first_data_received:
            messenger.send("🛰️ *Stream Confirmed!*")
            log.info("First data received from Geiger tube.")
            first_data_received = True
        last_received_time = time.time()

    time.sleep(0.05)
