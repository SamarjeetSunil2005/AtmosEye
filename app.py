import os
import logging
import time
from datetime import datetime, timedelta, date
import psutil
import pandas as pd
import atexit
from flask import Flask, jsonify, render_template, request, send_file, abort, Response
import zipfile
import shutil
import io
import threading
import ssl
import sys

import iaqcalc
import prediction
import atmosinsight
import wifi_manager
import telegram_settings
from telegram_bot import TelegramBot

app = Flask(__name__)
boot_time = psutil.boot_time()
LOG_DIR = "historical_logs"
os.makedirs(LOG_DIR, exist_ok=True)

bot = TelegramBot()

@app.route('/')
def index():
    return render_template('dashboard.html')

@app.route('/api/live')
def get_live_data():
    return jsonify(iaqcalc.get_latest_data())

def _get_health_data():
    health = iaqcalc.get_system_health()
    
    accurate_cpu = psutil.cpu_percent(interval=0.1)
    
    instant_cpu = psutil.cpu_percent(interval=0) 
    
    health.update({
        "cpu_usage": f"{accurate_cpu:.1f}%",
        "telegram_cpu_usage": f"{instant_cpu:.1f}%",
        "memory_usage": f"{psutil.virtual_memory().percent}%",
        "uptime": str(timedelta(seconds=int(time.time() - boot_time)))
    })
    return health

@app.route('/api/health')
def get_health_status():
    return jsonify(_get_health_data())

@app.route('/api/predict')
def get_prediction():
    history_data = iaqcalc.get_history_buffer()
    return jsonify(prediction.get_full_prediction(history_data))

@app.route('/api/insight')
def get_insight():
    tone = request.args.get('tone', 'professional')
    current_settings = iaqcalc.get_current_settings()
    min_duration = current_settings.get('insightMinDurationMinutesTotal', 0)
    return jsonify(atmosinsight.generate_insight(tone=tone, min_duration_setting=min_duration))

@app.route('/api/settings', methods=['POST'])
def update_settings():
    settings_data = request.json
    if settings_data:
        iaqcalc.update_settings(settings_data)
        return jsonify({"status": "success", "message": "Settings updated."})
    return jsonify({"status": "error", "message": "No settings data provided."}), 400

@app.route('/api/buzzer/test', methods=['POST'])
def test_buzzer_endpoint():
    iaqcalc.trigger_buzzer_test()
    return jsonify({"status": "success", "message": "Buzzer test initiated."})

@app.route('/api/alert/test', methods=['POST'])
def toggle_alert_endpoint():
    result = iaqcalc.toggle_test_alert()
    return jsonify(result)

@app.route('/api/alert/smoke_dismiss', methods=['POST'])
def dismiss_smoke_alert_endpoint():
    result = iaqcalc.dismiss_smoke_alert()
    return jsonify(result)

@app.route('/api/storage_status')
def get_storage_status():
    try:
        total, used, free = shutil.disk_usage("/")
        log_dir_size = 0
        for dirpath, _, filenames in os.walk(LOG_DIR):
            for f in filenames:
                fp = os.path.join(dirpath, f)
                log_dir_size += os.path.getsize(fp)
        
        return jsonify({
            "total_gb": round(total / (1024**3), 2),
            "used_gb": round(used / (1024**3), 2),
            "free_gb": round(free / (1024**3), 2),
            "percent_used": round((used / total) * 100),
            "log_dir_size_mb": round(log_dir_size / (1024**2), 2)
        })
    except Exception as e:
        logging.error(f"Could not get storage status: {e}")
        return jsonify({"error": "Could not retrieve storage status."}), 500

@app.route('/api/logs/structure')
def get_log_structure():
    structure = {}
    try:
        for year in sorted(os.listdir(LOG_DIR), reverse=True):
            year_path = os.path.join(LOG_DIR, year)
            if os.path.isdir(year_path):
                structure[year] = {}
                for month in sorted(os.listdir(year_path), reverse=True):
                    month_path = os.path.join(year_path, month)
                    if os.path.isdir(month_path):
                        structure[year][month] = []
    except FileNotFoundError:
        return jsonify({})
    return jsonify(structure)

@app.route('/api/logs/list')
def list_log_files():
    year = request.args.get('year')
    month = request.args.get('month')
    if not year or not month:
        return jsonify({"error": "Year and month parameters are required"}), 400
    
    files = []
    month_path = os.path.join(LOG_DIR, year, month)
    if not os.path.isdir(month_path):
        return jsonify(files)

    for f in sorted(os.listdir(month_path), reverse=True):
        if f.endswith(('.csv', '.csv.gz')):
            file_path = os.path.join(month_path, f)
            size_kb = round(os.path.getsize(file_path) / 1024, 2)
            if size_kb > 0:
                files.append({'name': f, 'size_kb': size_kb})
    return jsonify(files)

@app.route('/api/logs/view')
def view_log_file():
    file_path_str = request.args.get('file')
    if not file_path_str:
        return jsonify({"error": "File path is required"}), 400

    safe_base_path = os.path.abspath(LOG_DIR)
    full_path = os.path.abspath(os.path.join(LOG_DIR, file_path_str.replace('../', '')))
    
    if not full_path.startswith(safe_base_path):
        return jsonify({"error": "Forbidden path"}), 403

    try:
        if not os.path.exists(full_path):
            return jsonify({"error": "File not found"}), 404
        
        read_func = pd.read_csv
        if full_path.endswith('.gz'):
            read_func = lambda f: pd.read_csv(f, compression='gzip')

        df = read_func(full_path)
        
        preview_df = df.head(100).round(2)
        preview_df = preview_df.fillna('N/A')

        preview_json = preview_df.to_dict(orient='records')
        headers = list(preview_df.columns)
        
        return jsonify({"headers": headers, "rows": preview_json})
    except Exception as e:
        logging.error(f"Error viewing log file {full_path}: {e}", exc_info=True)
        return jsonify({"error": f"INTERNAL SERVER ERROR"}), 500

@app.route('/api/logs/download', methods=['POST'])
def download_logs():
    data = request.json
    files_to_download = data.get('files', [])
    if not files_to_download:
        return jsonify({"error": "No files specified"}), 400
    
    safe_base_path = os.path.abspath(LOG_DIR)
    memory_file = io.BytesIO()

    if len(files_to_download) == 1:
        file_path_str = files_to_download[0]
        full_path = os.path.abspath(os.path.join(LOG_DIR, file_path_str.replace('../', '')))
        if not full_path.startswith(safe_base_path) or not os.path.exists(full_path):
            return jsonify({"error": "File not found or invalid"}), 404
        return send_file(full_path, as_attachment=True)
    else:
        zip_filename = f"atmoseye_logs_{datetime.now().strftime('%Y-%m-%d')}.zip"
        with zipfile.ZipFile(memory_file, 'w', zipfile.ZIP_DEFLATED) as zf:
            for file_path_str in files_to_download:
                full_path = os.path.abspath(os.path.join(LOG_DIR, file_path_str.replace('../', '')))
                if full_path.startswith(safe_base_path) and os.path.exists(full_path):
                    zf.write(full_path, arcname=file_path_str.replace('/', '_'))
        memory_file.seek(0)
        return send_file(memory_file, download_name=zip_filename, as_attachment=True)

@app.route('/api/logs/delete', methods=['POST'])
def delete_log_files():
    data = request.json
    files_to_delete = data.get('files', [])
    if not files_to_delete:
        return jsonify({"error": "No files specified"}), 400
    
    safe_base_path = os.path.abspath(LOG_DIR)
    deleted_count = 0
    for file_path_str in files_to_delete:
        full_path = os.path.abspath(os.path.join(LOG_DIR, file_path_str.replace('../', '')))
        if full_path.startswith(safe_base_path) and os.path.exists(full_path):
            try:
                os.remove(full_path)
                deleted_count += 1
            except Exception as e:
                logging.error(f"Could not delete file {full_path}: {e}")
    
    return jsonify({"status": "success", "message": f"Successfully deleted {deleted_count} file(s)."})

@app.route('/api/logs/compress_old', methods=['POST'])
def compress_old_logs():
    thirty_days_ago = datetime.now() - timedelta(days=30)
    compressed_count = 0
    safe_base_path = os.path.abspath(LOG_DIR)

    for dirpath, _, filenames in os.walk(LOG_DIR):
        for filename in filenames:
            if filename.endswith('.csv'):
                try:
                    file_date_str = os.path.splitext(filename)[0]
                    file_date = datetime.strptime(file_date_str, '%Y-%m-%d')
                    if file_date < thirty_days_ago:
                        full_path = os.path.join(dirpath, filename)
                        if not os.path.abspath(full_path).startswith(safe_base_path): continue
                        
                        df = pd.read_csv(full_path)
                        gz_path = full_path + '.gz'
                        df.to_csv(gz_path, index=False, compression='gzip')
                        os.remove(full_path)
                        compressed_count += 1
                except (ValueError, FileNotFoundError) as e:
                    logging.warning(f"Could not process or date-parse file {filename}: {e}")
                    continue
    
    return jsonify({"status": "success", "message": f"Compressed {compressed_count} log file(s) older than 30 days."})

@app.route('/api/logs/auto_clean', methods=['POST'])
def auto_clean_logs():
    data = request.json
    retention = data.get('retention', '90d')
    if retention == 'forever':
        return jsonify({"status": "success", "message": "Retention set to forever, no files cleaned."})

    try:
        days = int(retention[:-1])
    except:
        return jsonify({"error": "Invalid retention period format."}), 400

    cutoff_date = datetime.now() - timedelta(days=days)
    deleted_count = 0
    safe_base_path = os.path.abspath(LOG_DIR)

    for dirpath, _, filenames in os.walk(LOG_DIR):
        for filename in filenames:
            try:
                day_str = os.path.splitext(filename.replace('.csv.gz',''))[0]
                month_str = os.path.basename(dirpath)
                year_str = os.path.basename(os.path.dirname(dirpath))
                
                file_date = datetime(int(year_str), int(month_str), int(day_str))

                if file_date.date() < cutoff_date.date():
                    full_path = os.path.join(dirpath, filename)
                    if not os.path.abspath(full_path).startswith(safe_base_path): continue
                    os.remove(full_path)
                    deleted_count += 1
            except (ValueError, FileNotFoundError, TypeError) as e:
                logging.warning(f"Could not auto-clean file {filename}: {e}")
                continue

    return jsonify({"status": "success", "message": f"Cleaned up {deleted_count} log file(s) older than {days} days."})

@app.route('/api/wifi/status')
def wifi_status():
    return jsonify(wifi_manager.get_status())

@app.route('/api/wifi/list')
def wifi_list():
    networks = wifi_manager.scan_networks(wifi_manager.WLAN_EDITABLE)
    return jsonify(networks)

@app.route('/api/wifi/connect', methods=['POST'])
def wifi_connect():
    data = request.json
    ssid = data.get('ssid')
    password = data.get('password')
    if not ssid:
        return jsonify({"error": "SSID is required"}), 400
    result = wifi_manager.connect(wifi_manager.WLAN_EDITABLE, ssid, password)
    return jsonify(result)

@app.route('/api/wifi/disconnect', methods=['POST'])
def wifi_disconnect():
    result = wifi_manager.disconnect(wifi_manager.WLAN_EDITABLE)
    return jsonify(result)

@app.route('/api/wifi/saved')
def wifi_saved():
    saved_networks = wifi_manager.get_saved_networks(wifi_manager.WLAN_EDITABLE)
    return jsonify(saved_networks)

@app.route('/api/wifi/forget', methods=['POST'])
def wifi_forget():
    data = request.json
    ssid = data.get('ssid')
    if not ssid:
        return jsonify({"error": "SSID is required"}), 400
    result = wifi_manager.forget_network(wifi_manager.WLAN_EDITABLE, ssid)
    return jsonify(result)

@app.route('/api/wifi/mode', methods=['POST'])
def set_wifi_mode():
    data = request.json
    mode = data.get('mode')
    if not mode:
        return jsonify({"error": "Mode is required"}), 400
    result = wifi_manager.set_mode_wlan1(mode)
    return jsonify(result)

@app.route('/api/wifi/configure_ap', methods=['POST'])
def configure_ap():
    data = request.json
    ssid = data.get('ssid')
    password = data.get('password', None)
    if not ssid:
        return jsonify({"error": "SSID is required"}), 400
    if password and (len(password) < 8 or len(password) > 63):
        return jsonify({"error": "Password must be 8-63 characters."}), 400
    
    result = wifi_manager.configure_ap(ssid, password)
    return jsonify(result)

@app.route('/api/wifi/devices')
def get_connected_devices():
    devices = wifi_manager.list_connected_devices(wifi_manager.WLAN_EDITABLE)
    return jsonify(devices)

@app.route('/api/telegram/settings', methods=['GET', 'POST'])
def telegram_settings_route():
    if request.method == 'POST':
        data = request.json
        token = data.get('bot_token')
        chat_id = data.get('chat_id')
        
        try:
            with open("telegram_settings.py", "w") as f:
                f.write(f'BOT_TOKEN = "{token}"\n')
                f.write(f'CHAT_ID = "{chat_id}"\n')
            
            bot.update_credentials(token, chat_id)
            return jsonify({"status": "success", "message": "Settings saved. Bot is restarting."})
        except Exception as e:
            logging.error(f"Failed to write telegram_settings.py: {e}")
            return jsonify({"error": True, "message": "Failed to write settings file."}), 500

    else:
        try:
            token = telegram_settings.BOT_TOKEN
            chat_id = telegram_settings.CHAT_ID
        except Exception:
            token = ""
            chat_id = ""
        return jsonify({"bot_token": token, "chat_id": chat_id})

@app.route('/api/telegram/test', methods=['POST'])
def telegram_test_route():
    result = bot.send_test_message()
    return jsonify(result)

@app.route('/api/telegram/history')
def telegram_history_route():
    history = bot.get_message_history()
    return jsonify(history)

def setup_bot_fetchers(bot_instance):
    def get_bot_insight(tone='professional'):
        current_settings = iaqcalc.get_current_settings()
        min_duration = current_settings.get('insightMinDurationMinutesTotal', 0)
        return atmosinsight.generate_insight(tone=tone, min_duration_setting=min_duration)
        
    def get_bot_predict(history):
        return prediction.get_full_prediction(history)

    fetchers_dict = {
        'live': iaqcalc.get_latest_data,
        'health': _get_health_data,
        'predict': get_bot_predict,
        'insight': get_bot_insight,
        'history': iaqcalc.get_history_buffer
    }
    
    bot_instance.register_fetchers(fetchers_dict)
    iaqcalc.set_telegram_bot_instance(bot_instance)

def cleanup_on_exit():
    iaqcalc.cleanup()
    bot.stop()

atexit.register(cleanup_on_exit)

if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(threadName)s - %(levelname)s - %(message)s')
    
    logging.info("Loading Telegram credentials...")
    bot.update_credentials(telegram_settings.BOT_TOKEN, telegram_settings.CHAT_ID)
    
    setup_bot_fetchers(bot)
    
    logging.info("Starting sensor monitoring threads...")
    iaqcalc.start_monitoring_threads()
    
    logging.info("Starting Telegram bot threads...")
    bot.start_threads()

    logging.info("Starting Flask server...")
    
    ssl_context = ('cert.pem', 'key.pem')
    
    if not os.path.exists(ssl_context[0]) or not os.path.exists(ssl_context[1]):
        logging.critical(f"SSL ERROR: Certificate '{ssl_context[0]}' or key '{ssl_context[1]}' not found.")
        logging.critical("Server cannot start in HTTPS mode. Please generate SSL certificates.")
        logging.critical("Exiting.")
        cleanup_on_exit()
        sys.exit(1)

    try:
        logging.info(f"Starting web server (HTTPS) on https://0.0.0.0:5000")
        
        app.run(
            host='0.0.0.0', 
            port=5000, 
            debug=False, 
            ssl_context=ssl_context, 
            threaded=True, 
            processes=1 
        )
    
    except Exception as e:
        logging.critical(f"SERVER FAILED TO START: {e}")
        logging.critical("This could be due to a port conflict (5000) or permissions issue.")
        logging.critical("Exiting.")
        cleanup_on_exit()
        sys.exit(1)
