import os
import time
import threading
import collections
import logging
import pandas as pd
import numpy as np
import atexit
from datetime import datetime, timedelta, date
import pytz

try:
    import board
    import busio
    import adafruit_bme680
    from pms5003 import PMS5003, ReadTimeoutError as PMSTimeoutError
    from serial import SerialException
    import RPi.GPIO as GPIO
    HAVE_SENSORS = True
except ImportError:
    HAVE_SENSORS = False
    logging.error("CRITICAL: One or more hardware libraries are not installed.")
except RuntimeError:
    HAVE_SENSORS = False
    logging.error("CRITICAL: Could not initialize GPIO. Please run as root/sudo.")

LOG_DIR = "historical_logs"
SENSOR_READ_INTERVAL = 2
HISTORY_BUFFER_SIZE = 43200 
CSV_WRITE_INTERVAL = 60 
BUZZER_PIN = 18
ROLLING_WINDOW_SIZE = 5 
TEMP_SPIKE_THRESHOLD = 3.0 
TEMP_SPIKE_WINDOW = 5 

data_lock = threading.Lock()
latest_sensor_data = {}
history_buffer = collections.deque(maxlen=HISTORY_BUFFER_SIZE)
system_health = {
    "status": "Initializing...", 
    "model_version": "Atmos-HFS-v1.1", 
    "sensor_status": "Unknown",
    "last_auto_cleanup": "Never"
}
user_settings = {
    "enableSmokeDetection": True,
    "emergencyAlert": False, 
    "useFahrenheit": False, 
    "alertThreshold": 150,
    "logRetentionPeriod": "90d",
    "insightMinDurationMinutesTotal": 0 
}
alert_active = False
alert_state = "SAFE" 
telegram_bot_instance = None 

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(threadName)s - %(levelname)s - %(message)s')

class Buzzer:
    def __init__(self, pin):
        self.pin = pin
        self.is_setup = False
        self.stop_flag = threading.Event()
        self.siren_thread = None
        if HAVE_SENSORS:
            try:
                GPIO.setwarnings(False)
                GPIO.setmode(GPIO.BCM)
                GPIO.setup(self.pin, GPIO.OUT)
                self.pwm = GPIO.PWM(self.pin, 100) 
                self.is_setup = True
            except Exception as e:
                logging.error(f"Buzzer initialization failed: {e}")

    def _siren(self):
        if not self.is_setup: return
        
        # Reverted to the original siren sound
        self.pwm.start(50)
        while not self.stop_flag.is_set():
            for freq in range(600, 1200, 20):
                if self.stop_flag.is_set(): break
                self.pwm.ChangeFrequency(freq); time.sleep(0.01)
            for freq in range(1200, 600, -20):
                if self.stop_flag.is_set(): break
                self.pwm.ChangeFrequency(freq); time.sleep(0.01)
                
        self.pwm.stop() 
        logging.info("Buzzer alert stopped.")

    def start_alert(self):
        if not self.is_setup:
            logging.warning("Cannot start buzzer alert, not set up.")
            return
        self.stop_flag.clear()
        if not self.siren_thread or not self.siren_thread.is_alive():
            self.siren_thread = threading.Thread(target=self._siren, daemon=True, name="BuzzerSiren")
            self.siren_thread.start()
            logging.info("Buzzer siren started.")

    def stop_alert(self):
        if not self.is_setup: return
        self.stop_flag.set()

    def test(self):
        if not self.is_setup:
            logging.warning("Cannot test buzzer, not set up.")
            return
        
        def _test_buzz():
            logging.info("Buzzer test running...")
            self.pwm.start(50)
            self.pwm.ChangeFrequency(1000)
            time.sleep(0.5)
            self.pwm.stop()
            logging.info("Buzzer test finished.")
            
        threading.Thread(target=_test_buzz, daemon=True).start()


    def cleanup(self):
        if self.is_setup:
            self.stop_flag.set()
            if self.pwm: self.pwm.stop()
            GPIO.cleanup(self.pin)
            logging.info("Buzzer GPIO cleaned up.")

buzzer = Buzzer(BUZZER_PIN)

class Sensor:
    def __init__(self):
        self.bme_sensor, self.pms_sensor = None, None
        if not HAVE_SENSORS:
            system_health['sensor_status'] = "Disabled (No Sensors)"; return
        try:
            i2c = busio.I2C(board.SCL, board.SDA)
            self.bme_sensor = adafruit_bme680.Adafruit_BME680_I2C(i2c, address=0x76)
            logging.info("BME688 initialized.")
        except Exception as e:
            logging.error(f"BME688 init failed: {e}"); system_health['sensor_status'] = "Error: BME688"
        try:
            self.pms_sensor = PMS5003(device="/dev/serial0", baudrate=9600, pin_enable=22, pin_reset=27)
            logging.info("PMSA003 initialized.")
        except Exception as e:
            logging.error(f"PMSA003 init failed: {e}")
            if "BME688" not in system_health['sensor_status']:
                system_health['sensor_status'] = "Error: PMSA003"
        
        if self.bme_sensor and self.pms_sensor: system_health['sensor_status'] = "OK"

    def get_data(self):
        data = {}
        try:
            if self.bme_sensor:
                data.update({
                    'temperature': self.bme_sensor.temperature,
                    'humidity': self.bme_sensor.relative_humidity,
                    'pressure': self.bme_sensor.pressure,
                    'gas_resistance': self.bme_sensor.gas
                })
            if self.pms_sensor:
                pms_data = self.pms_sensor.read()
                data.update({
                    'pm1': pms_data.pm_ug_per_m3(1.0),
                    'pm25': pms_data.pm_ug_per_m3(2.5),
                    'pm10': pms_data.pm_ug_per_m3(10.0)
                })
            return data
        except (PMSTimeoutError, SerialException, OSError) as e:
            logging.warning(f"A recoverable sensor error occurred: {e}")
            system_health['sensor_status'] = "Recovering..."
            return None
        except Exception as e:
            logging.error(f"An unexpected sensor error occurred: {e}", exc_info=True)
            system_health['sensor_status'] = "Sensor Fault"
            return None

class IAQProcessor:
    def __init__(self):
        self.ewma_baseline = None
        self.alpha = 0.2
        self.gas_window = collections.deque(maxlen=ROLLING_WINDOW_SIZE)

    def process(self, raw_data):
        gas_raw = raw_data.get('gas_resistance')
        pm25 = raw_data.get('pm25')
        temp = raw_data.get('temperature')
        humidity = raw_data.get('humidity')

        if any(v is None for v in [gas_raw, pm25, temp, humidity]): return {}

        self.gas_window.append(np.clip(gas_raw, 10000, 500000))
        smoothed_gas = np.mean(self.gas_window)

        if self.ewma_baseline is None: self.ewma_baseline = smoothed_gas
        else: self.ewma_baseline = self.alpha * smoothed_gas + (1 - self.alpha) * self.ewma_baseline
        
        deviation = max(0, self.ewma_baseline - smoothed_gas)
        voc_index = min(500, (deviation / self.ewma_baseline) * 500 if self.ewma_baseline > 0 else 0)
        pm25_aqi = _pm25_to_aqi(pm25)
        humidity_penalty = max(0, humidity - 70) * 2
        temp_penalty = 0
        if temp < 18: temp_penalty = (18 - temp) * 2
        elif temp > 26: temp_penalty = (temp - 26) * 2

        fused_iaq = (voc_index * 0.5) + (pm25_aqi * 0.5) + humidity_penalty + temp_penalty
        fused_iaq = min(500, round(fused_iaq))

        voc_ppb = (deviation / self.ewma_baseline)**1.5 * 1500 if self.ewma_baseline > 0 else 0
        co2_equivalent = 400 + voc_ppb * 0.8 + pm25 * 1.5

        return {
            'iaq': fused_iaq, 'aqi': pm25_aqi,
            'voc_index': round(voc_index),
            'co2_equivalent': round(co2_equivalent)
        }
        
class SmokeDetector:
    def __init__(self, bot_instance):
        self.bot = bot_instance
        self.temp_history = collections.deque(maxlen=TEMP_SPIKE_WINDOW)
        self.gas_history = collections.deque(maxlen=TEMP_SPIKE_WINDOW)
        self.last_alert_time = 0
        self.alert_cooldown = 300 
        self.test_alert_active = False
        logging.info("SmokeDetector instance created.")
        if self.bot:
             self.bot.queue_message("AtmosEye system online. Smart smoke detector is ACTIVE.", "info")
             logging.info("SmokeDetector linked with TelegramBot.")
        else:
             logging.warning("SmokeDetector created WITHOUT TelegramBot instance.")

    def check_conditions(self, data):
        global alert_active, alert_state
        if not user_settings.get('enableSmokeDetection'):
            if alert_active and alert_state == 'SMOKE':
                self.clear_alert("Smoke detection disabled by user.")
            return

        now = time.time()
        
        if self.test_alert_active:
             alert_active = True
             alert_state = 'TEST'
             return

        current_temp = data.get('temperature', 0)
        current_gas = data.get('gas_resistance', 1e6)
        self.temp_history.append(current_temp)
        self.gas_history.append(current_gas)

        if len(self.temp_history) < TEMP_SPIKE_WINDOW:
            return

        triggers = []
        
        pm_val = data.get('pm25', 0)
        if pm_val > 100:
            triggers.append(f"PM2.5: {pm_val:.0f}")

        voc_val = data.get('voc_index', 0)
        gas_change = np.mean(list(self.gas_history)[:2]) - np.mean(list(self.gas_history)[-2:])
        if voc_val > 250 or gas_change > 25000:
            triggers.append(f"VOC Index: {voc_val:.0f}")
            triggers.append(f"Gas Drop: {gas_change:.0f}")

        temp_spike = self.temp_history[-1] - self.temp_history[0]
        if temp_spike > TEMP_SPIKE_THRESHOLD:
            triggers.append(f"Temp Spike: {temp_spike:.1f}°C")

        trigger_count = len(triggers)
        details_str = ", ".join(triggers)
        
        if trigger_count >= 2 and (now - self.last_alert_time > self.alert_cooldown):
            if not alert_active:
                self.trigger_alert("SMOKE", f"Smart smoke alert triggered! Details: {details_str}")
                self.last_alert_time = now
        elif trigger_count == 0 and alert_active:
            if alert_state == 'SMOKE':
                self.clear_alert(f"Conditions returned to normal. (Last: {details_str})")
            elif alert_state == 'WARNING':
                self.clear_alert(f"IAQ returned to normal. (Last: {details_str})")
        
        if not alert_active and data.get('iaq', 0) > user_settings.get('alertThreshold', 150):
            if alert_state != 'WARNING':
                logging.warning(f"High IAQ Warning: {data.get('iaq', 0)}")
                alert_state = 'WARNING'
        elif not alert_active and alert_state == 'WARNING' and data.get('iaq', 0) < user_settings.get('alertThreshold', 150):
             alert_state = 'SAFE'


    def trigger_alert(self, state, message):
        global alert_active, alert_state
        logging.warning(message)
        alert_active = True
        alert_state = state
        if self.bot:
            self.bot.queue_message(f"🚨 {message}", "alert")
        
        if user_settings.get('emergencyAlert') or state == 'TEST':
            buzzer.start_alert()

    def clear_alert(self, message):
        global alert_active, alert_state
        logging.info(f"Alert conditions cleared. Sending recovery message.")
        alert_active = False
        alert_state = "SAFE"
        if self.bot:
            self.bot.queue_message(f"✅ {message}", "recovery")
        buzzer.stop_alert()

    def dismiss_alert(self):
        if not alert_active:
            return {"status": "info", "message": "No active alert to dismiss."}
        
        logging.info("Smoke alert dismissed by user.")
        self.last_alert_time = time.time() 
        self.clear_alert("Alert manually dismissed by user.")
        return {"status": "success", "message": "Alert dismissed."}

    def toggle_test(self):
        if self.test_alert_active:
            self.test_alert_active = False
            self.clear_alert("Test alert stopped by user.")
            return {"status": "success", "message": "Test alert stopped."}
        else:
            self.test_alert_active = True
            self.trigger_alert("TEST", "This is a test of the alert system.")
            return {"status": "success", "message": "Test alert started."}

def _pm25_to_aqi(pm):
    if pm is None or pm < 0: return 0
    c = float(pm)
    breakpoints = [(0.0, 12.0, 0, 50), (12.1, 35.4, 51, 100), (35.5, 55.4, 101, 150),
                   (55.5, 150.4, 151, 200), (150.5, 250.4, 201, 300), (250.5, 500.4, 301, 500)]
    for low_c, high_c, low_i, high_i in breakpoints:
        if low_c <= c <= high_c:
            return round(((high_i - low_i) / (high_c - low_c)) * (c - low_c) + low_i)
    return 500 

def get_iaq_level(score):
    if score is None: return "Calculating..."
    if score <= 50: return "Good"
    if score <= 100: return "Moderate"
    if score <= 150: return "Unhealthy (SG)"
    if score <= 200: return "Unhealthy"
    if score <= 300: return "Very Unhealthy"
    return "Hazardous"

def set_telegram_bot_instance(bot_instance):
    global telegram_bot_instance
    if bot_instance:
        telegram_bot_instance = bot_instance
        logging.info("TelegramBot instance passed to iaqcalc.")
    else:
        logging.warning("iaqcalc received an empty TelegramBot instance.")

smoke_detector_instance = None

def continuous_sensor_reading():
    global alert_active, alert_state, latest_sensor_data, smoke_detector_instance
    
    if not HAVE_SENSORS:
        logging.error("SensorReader thread exiting: HAVE_SENSORS is False. No sensors detected.")
        system_health['sensor_status'] = "Disabled (No Sensors)"
        return

    sensor = Sensor()
    processor = IAQProcessor()
    
    while not telegram_bot_instance and HAVE_SENSORS:
        logging.warning("SensorReader waiting for TelegramBot instance...")
        time.sleep(1)
    
    smoke_detector_instance = SmokeDetector(telegram_bot_instance)
    
    last_read_time = 0
    while True:
        try:
            now = time.time()
            if (now - last_read_time) < SENSOR_READ_INTERVAL:
                time.sleep(0.1)
                continue
            last_read_time = now

            raw_data = sensor.get_data()

            if raw_data:
                ts = time.time()
                calculated_data = processor.process(raw_data)
                
                final_data_point = {**raw_data, **calculated_data, 'timestamp': ts}
                
                smoke_detector_instance.check_conditions(final_data_point)
                
                final_data_point['alert_active'] = alert_active
                final_data_point['state'] = alert_state
                final_data_point['iaq_level'] = get_iaq_level(final_data_point.get('iaq'))
                final_data_point['pm25_level'] = get_iaq_level(final_data_point.get('aqi'))


                with data_lock:
                    latest_sensor_data = final_data_point
                    history_buffer.append(final_data_point)
            
            else:
                 logging.warning("Sensor read returned no data.")
                 if HAVE_SENSORS:
                     system_health['sensor_status'] = "Error: No Data"
                     
        except Exception as e:
            logging.error(f"Sensor thread encountered a critical error: {e}", exc_info=True)
            system_health['sensor_status'] = "Error: Thread Crash"
            time.sleep(10)

def periodic_csv_writer():
    global_tz = pytz.timezone('Asia/Kolkata')
    os.makedirs(LOG_DIR, exist_ok=True)
    last_check_time = 0
    
    while True:
        now = time.time()
        if (now - last_check_time) < CSV_WRITE_INTERVAL:
            time.sleep(1)
            continue
        last_check_time = now
        
        try:
            with data_lock:
                current_logging_enabled = user_settings.get('loggingEnabled', True)
                
                if not current_logging_enabled or not history_buffer:
                    continue
                
                buffer_copy = list(history_buffer)

            if not buffer_copy: continue

            df = pd.DataFrame(buffer_copy)
            df['datetime_utc'] = pd.to_datetime(df['timestamp'], unit='s', utc=True)
            df['datetime_local'] = df['datetime_utc'].dt.tz_convert(global_tz)
            
            today = datetime.now(global_tz).date()
            df_today = df[df['datetime_local'].dt.date == today].copy()
            
            if df_today.empty:
                logging.debug("No data for today in buffer, skipping log write.")
                continue

            df_today = df_today.set_index('datetime_local')
            
            resample_key = "30s"
            
            downsampled_df = df_today.resample(resample_key).mean(numeric_only=True)
            
            log_year = today.strftime('%Y')
            log_month = today.strftime('%m')
            log_day = today.strftime('%d')
            
            log_dir_path = os.path.join(LOG_DIR, log_year, log_month)
            os.makedirs(log_dir_path, exist_ok=True)
            log_filename = os.path.join(log_dir_path, f"{log_day}.csv")

            params_to_log = ['iaq', 'aqi', 'voc_index', 'co2_equivalent', 'temperature', 'humidity', 'pressure', 'pm1', 'pm25', 'pm10', 'gas_resistance']
            rounding_precision = {
                'iaq': 0, 'aqi': 0, 'voc_index': 0, 'co2_equivalent': 0,
                'temperature': 1, 'humidity': 0, 'pressure': 1,
                'pm1': 0, 'pm25': 0, 'pm10': 0, 'gas_resistance': 0
            }
            
            valid_params = [p for p in params_to_log if p in downsampled_df.columns]
            
            if not valid_params: continue

            final_df = downsampled_df[valid_params].round(rounding_precision)
            final_df.dropna(how='all').to_csv(log_filename)
            logging.info(f"Wrote {len(final_df)} records to {log_filename} (Resampled @ {resample_key})")

        except Exception as e:
            logging.error(f"CSV writer error: {e}", exc_info=True)

def auto_maintenance_worker():
    while True:
        now = datetime.now()
        logging.info(f"Auto-cleanup: Running daily task for {now.date()}.")
        
        with data_lock:
            retention_period = user_settings.get('logRetentionPeriod', '90d')
            run_manual = user_settings.pop('run_manual_cleanup', False) 

        if retention_period == 'forever' and not run_manual:
            logging.info("Auto-cleanup: Skipping, retention is set to forever.")
            time.sleep(3600 * 24) 
            continue
        
        if run_manual:
             logging.info(f"Auto-cleanup: Manual run.")
        
        try:
            days = int(retention_period[:-1])
        except:
            logging.error(f"Auto-cleanup: Invalid retention period '{retention_period}'. Defaulting to 90 days.")
            days = 90
            
        cutoff_date = now - timedelta(days=days)
        deleted_count = 0
        safe_base_path = os.path.abspath(LOG_DIR)
        
        logging.info(f"Auto-cleanup: Deleting logs older than {cutoff_date.date()} ({days} days).")

        for dirpath, dirnames, filenames in os.walk(LOG_DIR, topdown=False):
            for filename in filenames:
                try:
                    day_str = os.path.splitext(filename.replace('.csv.gz','').replace('.csv',''))[0]
                    month_str = os.path.basename(dirpath)
                    year_str = os.path.basename(os.path.dirname(dirpath))
                    
                    file_date = datetime(int(year_str), int(month_str), int(day_str))

                    if file_date.date() < cutoff_date.date():
                        full_path = os.path.join(dirpath, filename)
                        if not os.path.abspath(full_path).startswith(safe_base_path): continue
                        os.remove(full_path)
                        deleted_co