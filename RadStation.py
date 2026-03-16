import os
import time
import csv
import serial
import schedule
import pandas as pd
import numpy as np
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
import shutil
from scipy import signal
from threading import Thread, Lock
from queue import Queue, Empty
import requests

# --- STABILITY ---
import matplotlib
matplotlib.use('Agg') 
plot_lock = Lock()

# --- CONFIGURATION ---
TOKEN = "6832213326:AAF5bhYx--H-eEYXo2IqR0341JBunHrzrWE"
CHAT_IDS = ["508873529"]
LOG_PATH = '/home/navenger/radstat/'
ARCHIVE_PATH = os.path.join(LOG_PATH, 'archive/')
SERIAL_PORT = '/dev/serial0'
BAUD_RATE = 19200

ALERT_THRESHOLD = 50.0       
INTERESTING_THRESHOLD = 28.0 
CLEANUP_DAYS = 30            

# --- GLOBAL STATE ---
ram_buffer = []             
ram_lock = Lock()           
start_time = time.time()
last_received_time = 0.0    
last_loop_heartbeat = time.time()
last_reader_heartbeat = time.time()

connection_error_sent = False
first_data_received = False
system_is_in_high_state = False
consistent_alert_sent = False
high_timer_start = None     
normal_timer_start = None   
current_avg_cached = 0.0    

# --- CORE CLASSES ---

class SerialReader:
    def __init__(self, port, rate):
        self.port = port
        self.rate = rate
        self.buffer = Queue(maxsize=5000)
        try:
            self.ser = serial.Serial(port, baudrate=rate, timeout=0.5)
        except:
            self.ser = None
        Thread(target=self.reader_routine, daemon=True).start()
        
    def verify_checksum(self, data_str, checksum_str):
        try:
            return sum(ord(c) for c in data_str.strip()) % 256 == int(checksum_str)
        except: return False

    def reader_routine(self):
        global last_reader_heartbeat
        while True:
            last_reader_heartbeat = time.time()
            if self.ser and self.ser.is_open:
                try:
                    if self.ser.in_waiting > 0:
                        line = self.ser.readline().decode('utf-8', errors='ignore').strip()
                        if "*" in line:
                            parts = line.split("*")
                            if len(parts) == 2 and self.verify_checksum(parts[0], parts[1]):
                                self.buffer.put_nowait(parts[0].strip())
                except: time.sleep(1)
            else: time.sleep(2)

    def read(self):
        try: return self.buffer.get_nowait()
        except: return None

class LogWriter:
    def __init__(self, filepath):
        self.filepath = filepath
        os.makedirs(ARCHIVE_PATH, exist_ok=True)
        self.write_queue = Queue()
        Thread(target=self.logger_routine, daemon=True).start()
        
    def get_filename(self):
        return os.path.join(self.filepath, pd.Timestamp.now().strftime('Stat-%d-%m-%Y.csv'))

    def batch_log(self, data_list):
        if data_list: self.write_queue.put(data_list)
        
    def logger_routine(self):
        while True:
            batch = self.write_queue.get()
            fname = self.get_filename()
            try:
                with open(fname, mode='a', newline='') as f:
                    csv.writer(f).writerows(batch)
            except: pass

# ... [Imports and other classes remain identical to v2.0.7] ...

class TeleMessenger:
    def __init__(self, token, chat_ids):
        self.url = f"https://api.telegram.org/bot{token}/"
        self.chat_ids = [str(cid) for cid in chat_ids]
        self.last_update_id = 0
        
        # Clear 'Ghost' commands from before startup
        self.flush_old_commands()
        
        Thread(target=self.command_listener, daemon=True).start()

    def flush_old_commands(self):
        """Skip all pending messages from before the script started."""
        try:
            r = requests.get(self.url + "getUpdates?offset=-1&timeout=5", timeout=10).json()
            if r.get("result"):
                self.last_update_id = r["result"][0]["update_id"]
        except: pass

    def send(self, obj, is_image=False, silent=False):
        for cid in self.chat_ids:
            try:
                if is_image:
                    with open(obj, 'rb') as img:
                        requests.post(self.url + "sendPhoto", data={'chat_id': cid, 'disable_notification': silent}, files={'photo': img}, timeout=30)
                else:
                    requests.post(self.url + "sendMessage", data={'chat_id': cid, 'text': obj, 'disable_notification': silent, 'parse_mode': 'Markdown'}, timeout=15)
            except: pass

    def command_listener(self):
        # Notify user that bot is ready
        self.send("🤖 *Command Listener Active.*")
        while True:
            try:
                r = requests.get(self.url + f"getUpdates?offset={self.last_update_id + 1}&timeout=30", timeout=45).json()
                if not r.get("result"): continue

                for update in r["result"]:
                    self.last_update_id = update["update_id"]
                    msg = update.get("message")
                    if not msg or "text" not in msg: continue
                    
                    cmd = msg["text"].lower().strip()
                    uid = str(msg["chat"]["id"])
                    
                    if uid in self.chat_ids:
                        if cmd == "/help":
                            help_text = (
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
                            self.send(help_text)
                            
                        elif cmd == "/status":
                            with ram_lock:
                                live_v = round(np.mean([x[1] for x in ram_buffer]), 2) if ram_buffer else current_avg_cached
                            emoji = "☢️" if live_v > ALERT_THRESHOLD else "✅"
                            self.send(f"{emoji} *Live Reading:* `{live_v}` uRh/h")
                            
                        elif cmd in ["/10mins", "/hourly", "/daily"]:
                            self.send(f"📊 *Generating {cmd[1:]} report...*", silent=True)
                            do_report(cmd[1:])
                            
                        elif cmd == "/health":
                            uptime = round((time.time() - start_time) / 3600, 1)
                            self.send(f"🩺 *System Health*\n• Uptime: `{uptime}h` \n• Stream: `{'Active' if time.time()-last_received_time < 10 else 'SILENT'}`")
                            
                        elif cmd == "/reboot":
                            self.send("🔄 *Manual Reboot Initiated...*")
                            time.sleep(2)
                            os.system('sudo reboot')
            except: time.sleep(5)

# ... [The rest of the StatsEngine, maintenance functions, and main loop remain identical to v2.0.7] ...


class StatsEngine:
    def generate_plot(self, source_file, mode='hourly'):
        temp_file = f"{LOG_PATH}temp_plot.csv"
        with plot_lock:
            try:
                df_list = []
                if os.path.exists(source_file):
                    shutil.copy2(source_file, temp_file)
                    df_file = pd.read_csv(temp_file, names=['Date', 'Value'], header=None)
                    df_list.append(df_file)
                with ram_lock:
                    if ram_buffer:
                        df_list.append(pd.DataFrame(ram_buffer, columns=['Date', 'Value']))
                
                if not df_list: return "Error: No data."
                df = pd.concat(df_list)
                df['Date'] = pd.to_datetime(df['Date'], format='ISO8601', errors='coerce').dt.tz_localize(None)
                df = df.dropna().sort_values('Date')
                
                now_ts = pd.Timestamp.now().replace(tzinfo=None)
                delta = {'10mins': pd.Timedelta(minutes=10), 'hourly': pd.Timedelta(minutes=60), 'daily': pd.Timedelta(days=1)}
                df_filtered = df[df['Date'] > (now_ts - delta.get(mode, delta['hourly']))]
                
                count = len(df_filtered)
                if count < 10: return f"Error: Only {count} points."

                # --- YOUR HAND-TUNED SMOOTHING RATIOS ---
                if mode == '10mins':
                    win = count // 4  
                elif mode == 'hourly':
                    win = int(count * 0.5) 
                else: # daily
                    win = int(count * 0.35) 
                
                if win < 11: win = 11
                if win % 2 == 0: win += 1
                if win >= count: win = count - 1 if (count - 1) % 2 != 0 else count - 2

                # --- VIVID PLOT STYLING ---
                fig = plt.figure(figsize=(10, 6), facecolor='#0f0f0f')
                ax = plt.gca()
                ax.set_facecolor('#0f0f0f')
                
                # Plot Data
                plt.plot(df_filtered['Date'], df_filtered['Value'], color='#007bff', lw=1.2, alpha=0.5, label="Raw, uRh/h")
                smooth_val = signal.savgol_filter(df_filtered['Value'], win, 3)
                plt.plot(df_filtered['Date'], smooth_val, color='#ff0000', lw=2.5, label="Trend")
                
                # Median
                med = round(df_filtered['Value'].median(), 2)
                plt.axhline(y=med, color='#28a745', ls='--', alpha=0.7, label=f"Median: {med}")

                # Grids
                plt.grid(True, which='both', linestyle='--', alpha=0.4, color='white')
                
                # --- UPDATED TITLE WITH DATE (DD:MM:YY) ---
                formatted_date = now_ts.strftime('%d.%m.%y')
                plt.title(f"{formatted_date} Radiation {mode.upper()} (Peak: {df_filtered['Value'].max()})", 
                          color='white', fontsize=14, pad=15)
                
                # Legend
                leg = plt.legend(loc='upper right', facecolor='#0f0f0f', edgecolor='gray')
                for text in leg.get_texts(): text.set_color('white')

                # Axis Formatting
                if mode == '10mins':
                    plt.gca().xaxis.set_major_formatter(mdates.DateFormatter('%H:%M:%S'))
                else:
                    plt.gca().xaxis.set_major_formatter(mdates.DateFormatter('%H:%M'))
                
                plt.xticks(color='white')
                plt.yticks(color='white')

                # Date/Time Caption (Bot Right)
                timestamp_str = f"Generated: {now_ts.strftime('%d-%m-%Y %H:%M:%S')}"
                plt.figtext(0.95, 0.02, timestamp_str, color='gray', ha='right', fontsize=9, style='italic')
                
                plt.tight_layout(rect=[0, 0.03, 1, 0.95])
                out = f"{LOG_PATH}{mode}_report.png"
                plt.savefig(out, dpi=120, facecolor='#0f0f0f', bbox_inches='tight', pad_inches=0.1)
                plt.close('all') 
                return out
            except Exception as e: return f"Plotting crash: {str(e)}"
            finally:
                if os.path.exists(temp_file): os.remove(temp_file)

# ... [Rest of the code stays identical] ...

def smart_cleanup():
    now = time.time()
    for f in os.listdir(LOG_PATH):
        if f.endswith('.csv') and os.stat(os.path.join(LOG_PATH, f)).st_mtime < now - (CLEANUP_DAYS * 86400):
            try:
                f_path = os.path.join(LOG_PATH, f)
                df = pd.read_csv(f_path, names=['Date', 'Value'])
                if df['Value'].max() > INTERESTING_THRESHOLD:
                    shutil.move(f_path, os.path.join(ARCHIVE_PATH, f))
                else:
                    os.remove(f_path)
            except: pass

def minute_processing():
    global ram_buffer, current_avg_cached, system_is_in_high_state, consistent_alert_sent, high_timer_start, normal_timer_start
    with ram_lock:
        if not ram_buffer: return
        vals = [v[1] for v in ram_buffer]
        current_avg_cached = round(float(np.mean(vals)), 2)
        logger.batch_log(list(ram_buffer))
        ram_buffer = []

    # Radiation Logic
    now = time.time()
    if current_avg_cached > ALERT_THRESHOLD:
        if not system_is_in_high_state:
            messenger.send(f"☢️ *SPIKE ALERT:* {current_avg_cached} uRh/h!")
            system_is_in_high_state, high_timer_start = True, now
        elif high_timer_start and (now - high_timer_start > 600) and not consistent_alert_sent:
            messenger.send(f"☢️☢️ *CONSISTENT HIGH:* {current_avg_cached} uRh/h!")
            consistent_alert_sent = True
    elif system_is_in_high_state:
        if not normal_timer_start: normal_timer_start = now
        if now - normal_timer_start > 1200:
            messenger.send(f"✅ *Back to Normal:* {current_avg_cached} uRh/h")
            system_is_in_high_state = consistent_alert_sent = False
            normal_timer_start = None

def watchdog():
    global connection_error_sent
    now = time.time()
    if now - last_received_time > 80 and first_data_received:
        if not connection_error_sent:
            messenger.send("🚨 *GEIGER LOST!*")
            connection_error_sent = True
    elif connection_error_sent and now - last_received_time < 80:
        messenger.send("✅ *GEIGER RESTORED!*")
        connection_error_sent = False

def do_report(mode):
    res = engine.generate_plot(logger.get_filename(), mode)
    if "Error" in res or "Crash" in res: messenger.send(f"❌ {res}")
    else: messenger.send(res, is_image=True)

# --- INIT & START ---
messenger = TeleMessenger(TOKEN, CHAT_IDS)
reader = SerialReader(SERIAL_PORT, BAUD_RATE)
logger = LogWriter(LOG_PATH)
engine = StatsEngine()

schedule.every(1).minutes.do(minute_processing)
schedule.every(1).minutes.do(watchdog)
schedule.every(24).hours.do(smart_cleanup)
schedule.every().hour.at(":58").do(lambda: do_report('hourly'))
schedule.every().day.at("23:55").do(lambda: do_report('daily'))

messenger.send("🚀 *System v2.0.1 Online.*")

# --- MAIN LOOP ---
while True:
    last_loop_heartbeat = time.time()
    schedule.run_pending()
    data_found = False
    while True:
        val = reader.read()
        if val is None: break
        data_found = True
        ts = pd.Timestamp.now().isoformat()
        with ram_lock:
            ram_buffer.append((ts, float(val)))
    if data_found:
        if not first_data_received:
            messenger.send("🛰️ *Stream Confirmed!*")
            first_data_received = True
        last_received_time = time.time()
    time.sleep(0.05)