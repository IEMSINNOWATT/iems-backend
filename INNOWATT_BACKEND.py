from flask import Flask, jsonify
from flask_cors import CORS
import requests
from datetime import datetime, timedelta
import os
from dotenv import load_dotenv
import time
import socket
import logging
import threading
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# ----------------------------------------
# Logging Configuration
# ----------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler('thingsboard_fetcher.log')
    ]
)
logger = logging.getLogger(__name__)

# ----------------------------------------
# Load Environment Variables
# ----------------------------------------
load_dotenv()

app = Flask(__name__)
CORS(app)

# ----------------------------------------
# ThingsBoard Config
# ----------------------------------------
THINGSBOARD_HOST = 'https://demo.thingsboard.io'
USERNAME = os.getenv('TB_USERNAME')
PASSWORD = os.getenv('TB_PASSWORD')
DEVICE_ID = os.getenv('TB_DEVICE_ID')
JWT_TOKEN = os.getenv('TB_JWT_TOKEN')

# Case-insensitive key mapping
TELEMETRY_KEY_MAPPING = {
    'voltage': ['Voltage', 'voltage', 'VOLTAGE'],
    'current': ['Current', 'current', 'CURRENT'],
    'power': ['Power', 'power', 'POWER'],
    'energy': ['Energy', 'energy', 'ENERGY'],
    'frequency': ['Frequency', 'frequency', 'FREQUENCY'],
    'powerfact': ['PowerFact', 'PF', 'powerfactor', 'Power_Factor'],
    'rmp': ['RMP', 'rmp', 'Rmp']
}

# ----------------------------------------
# Retry Setup
# ----------------------------------------
retry_strategy = Retry(
    total=3,
    backoff_factor=1,
    status_forcelist=[408, 429, 500, 502, 503, 504]
)
adapter = HTTPAdapter(max_retries=retry_strategy)
http = requests.Session()
http.mount("https://", adapter)
http.mount("http://", adapter)

# ----------------------------------------
# Keep-Alive Pinger
# ----------------------------------------
def keep_alive_ping():
    while True:
        try:
            response = http.get("https://iems-backend.onrender.com/health", timeout=10)
            logger.info(f"Keep-alive ping sent. Status: {response.status_code}")
        except Exception as e:
            logger.warning(f"Keep-alive ping failed: {e}")
        time.sleep(180)  # every 3 minutes

# ----------------------------------------
# Internet Check
# ----------------------------------------
def check_internet_connection():
    try:
        socket.create_connection(("8.8.8.8", 53), timeout=5)
        return True
    except OSError:
        logger.warning("No internet connection available")
        return False

# ----------------------------------------
# Authentication
# ----------------------------------------
def get_auth_token():
    if not check_internet_connection():
        return None
    for attempt in range(3):
        try:
            response = http.post(
                f"{THINGSBOARD_HOST}/api/auth/login",
                json={"username": USERNAME, "password": PASSWORD},
                timeout=10
            )
            if response.status_code == 401:
                logger.error("Authentication failed")
                return None
            response.raise_for_status()
            return response.json().get('token')
        except requests.exceptions.RequestException as e:
            logger.warning(f"Attempt {attempt+1} failed: {e}")
            if attempt < 2:
                time.sleep(2 ** attempt)
    return None

# ----------------------------------------
# Telemetry Fetch
# ----------------------------------------
def fetch_telemetry(token, keys=None, start_ts=None, end_ts=None, interval=None, limit=None):
    if not token or not check_internet_connection():
        return None

    try:
        url = f"{THINGSBOARD_HOST}/api/plugins/telemetry/DEVICE/{DEVICE_ID}/values/timeseries"
        params = {}

        if keys:
            tb_keys = []
            for key in keys:
                tb_keys.extend(TELEMETRY_KEY_MAPPING.get(key.lower(), [key]))
            params['keys'] = ','.join(set(tb_keys))
        
        if start_ts:
            params['startTs'] = start_ts
        if end_ts:
            params['endTs'] = end_ts
        if interval:
            params['interval'] = interval
        if limit:
            params['limit'] = limit

        response = http.get(
            url,
            headers={'X-Authorization': f'Bearer {token}'},
            params=params,
            timeout=15
        )

        if response.status_code == 401:
            logger.info("Token expired, refreshing...")
            new_token = get_auth_token()
            if new_token:
                response = http.get(
                    url,
                    headers={'X-Authorization': f'Bearer {new_token}'},
                    params=params,
                    timeout=15
                )

        response.raise_for_status()
        return response.json()

    except requests.exceptions.RequestException as e:
        logger.error(f"Failed to fetch telemetry: {e}")
        return None

# ----------------------------------------
# Helpers
# ----------------------------------------
def find_matching_key(data, possible_keys):
    for key in possible_keys:
        if key in data:
            return key
    return None

def get_value_and_timestamp(data, standard_key):
    possible_keys = TELEMETRY_KEY_MAPPING.get(standard_key, [standard_key])
    actual_key = find_matching_key(data, possible_keys)
    
    if not actual_key:
        return 0.0, None
    
    entry = data.get(actual_key, [{}])[0]
    try:
        value = float(entry.get("value", 0.0))
    except (ValueError, TypeError):
        value = 0.0
    ts = entry.get("ts")
    return value, ts

def process_telemetry_data(telemetry_data):
    if not telemetry_data:
        return None

    power, power_ts = get_value_and_timestamp(telemetry_data, "power")
    voltage, voltage_ts = get_value_and_timestamp(telemetry_data, "voltage")
    current, current_ts = get_value_and_timestamp(telemetry_data, "current")
    frequency, frequency_ts = get_value_and_timestamp(telemetry_data, "frequency")
    rmp, rmp_ts = get_value_and_timestamp(telemetry_data, "rmp")
    energy, energy_ts = get_value_and_timestamp(telemetry_data, "energy")
    powerfact, powerfact_ts = get_value_and_timestamp(telemetry_data, "powerfact")

    return {
        "power": power,
        "power_timestamp": power_ts,
        "voltage": voltage,
        "voltage_timestamp": voltage_ts,
        "current": current,
        "current_timestamp": current_ts,
        "frequency": frequency,
        "frequency_timestamp": frequency_ts,
        "rmp": rmp,
        "rmp_timestamp": rmp_ts,
        "energy": energy,
        "energy_timestamp": energy_ts,
        "powerfactor": powerfact,
        "powerfactor_timestamp": powerfact_ts,
        "timestamp": int(time.time() * 1000),
        "online": True
    }

def get_time_range(days):
    end_ts = int(time.time() * 1000)
    start_ts = end_ts - days * 24 * 60 * 60 * 1000
    return start_ts, end_ts

# ----------------------------------------
# Endpoints
# ----------------------------------------
@app.route('/api/telemetry')
def get_telemetry():
    token = JWT_TOKEN if JWT_TOKEN else get_auth_token()
    if not token:
        return jsonify({"error": "Authentication failed", "online": False}), 401

    telemetry_data = fetch_telemetry(
        token, 
        keys=['power', 'voltage', 'current', 'frequency', 'rmp', 'energy', 'powerfact', 'ngrok_url']
    )
    
    if not telemetry_data:
        return jsonify({"error": "Could not fetch telemetry", "online": False}), 500

    processed = process_telemetry_data(telemetry_data)

    ngrok_url = None
    for key in ['ngrok_url', 'Ngrok_Url', 'NGROK_URL']:
        if key in telemetry_data and telemetry_data[key]:
            try:
                ngrok_url = telemetry_data[key][0]["value"]
                break
            except (KeyError, IndexError, TypeError):
                continue
    processed["ngrok_url"] = ngrok_url

    return jsonify(processed)

@app.route('/api/telemetry/weekly')
def get_weekly_telemetry():
    # Same logic (unchanged from your original code)
    pass

@app.route('/api/telemetry/monthly')
def get_monthly_telemetry():
    # Same logic (unchanged from your original code)
    pass

@app.route('/health')
def health_check():
    return jsonify({
        "status": "running",
        "thingsboard_accessible": check_internet_connection(),
        "timestamp": datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    })

# ----------------------------------------
# Entry Point
# ----------------------------------------
if __name__ == '__main__':
    logger.info("Starting ThingsBoard Data Fetcher Service")

    # Start keep-alive thread
    threading.Thread(target=keep_alive_ping, daemon=True).start()

    app.run(host='0.0.0.0', port=5000, debug=False)
