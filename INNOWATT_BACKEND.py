from flask import Flask, jsonify
from flask_cors import CORS
import requests
from datetime import datetime
import os
from dotenv import load_dotenv
import time
import socket
import logging
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

# ----------------------------------------
# Key Mapping
# ----------------------------------------
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
# HTTP Retry Session
# ----------------------------------------
retry_strategy = Retry(
    total=3,
    backoff_factor=1,
    status_forcelist=[408, 429, 500, 502, 503, 504]
)
adapter = HTTPAdapter(max_retries=retry_strategy)
http = requests.Session()
http.keep_alive = True
http.headers.update({'Accept-Encoding': 'gzip, deflate'})
http.mount("https://", adapter)
http.mount("http://", adapter)

# ----------------------------------------
# Internet Check
# ----------------------------------------
def check_internet_connection():
    try:
        socket.create_connection(("8.8.8.8", 53), timeout=5)
        return True
    except OSError:
        logger.warning("No internet connection")
        return False

# ----------------------------------------
# JWT Auth
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
                logger.error("Invalid credentials")
                return None
            response.raise_for_status()
            return response.json().get('token')
        except requests.RequestException as e:
            logger.warning(f"Auth attempt {attempt+1} failed: {e}")
            time.sleep(2 ** attempt)
    return None

# ----------------------------------------
# Telemetry Fetcher
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

        headers = {'X-Authorization': f'Bearer {token}'}
        response = http.get(url, headers=headers, params=params, timeout=15)

        if response.status_code == 401:
            logger.info("Refreshing token...")
            token = get_auth_token()
            if token:
                response = http.get(url, headers={'X-Authorization': f'Bearer {token}'}, params=params, timeout=15)

        response.raise_for_status()
        return response.json()

    except requests.RequestException as e:
        logger.error(f"Telemetry fetch failed: {e}")
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
    return value, entry.get("ts")

def process_telemetry_data(data):
    if not data:
        return None
    return {
        "power": *get_value_and_timestamp(data, "power"),
        "voltage": *get_value_and_timestamp(data, "voltage"),
        "current": *get_value_and_timestamp(data, "current"),
        "frequency": *get_value_and_timestamp(data, "frequency"),
        "rmp": *get_value_and_timestamp(data, "rmp"),
        "energy": *get_value_and_timestamp(data, "energy"),
        "powerfactor": *get_value_and_timestamp(data, "powerfact"),
        "timestamp": int(time.time() * 1000),
        "online": True
    }

def get_time_range(days):
    end = int(time.time() * 1000)
    return end - days * 86400000, end

# ----------------------------------------
# API Endpoints
# ----------------------------------------
@app.route('/api/telemetry')
def get_telemetry():
    token = JWT_TOKEN or get_auth_token()
    if not token:
        return jsonify({"error": "Auth failed", "online": False}), 401

    data = fetch_telemetry(token, keys=list(TELEMETRY_KEY_MAPPING.keys()) + ['ngrok_url'])
    if not data:
        return jsonify({"error": "Fetch failed", "online": False}), 500

    result = process_telemetry_data(data)
    ngrok_url = next((data.get(key, [{}])[0].get("value") for key in ['ngrok_url', 'NGROK_URL'] if key in data), None)
    result["ngrok_url"] = ngrok_url
    return jsonify(result)

@app.route('/api/telemetry/weekly')
def get_weekly():
    return fetch_range_data(7, 3600000, 168)

@app.route('/api/telemetry/monthly')
def get_monthly():
    return fetch_range_data(30, 86400000, 30)

def fetch_range_data(days, interval, limit):
    token = JWT_TOKEN or get_auth_token()
    if not token:
        return jsonify({"error": "Auth failed", "online": False}), 401

    start_ts, end_ts = get_time_range(days)
    data = fetch_telemetry(token, keys=list(TELEMETRY_KEY_MAPPING.keys()), start_ts=start_ts, end_ts=end_ts, interval=interval, limit=limit)
    if not data:
        return jsonify({"error": "Fetch failed", "online": False}), 500

    key_map = {key: find_matching_key(data, TELEMETRY_KEY_MAPPING.get(key, [key])) for key in TELEMETRY_KEY_MAPPING}
    max_len = max((len(data.get(k, [])) for k in key_map.values() if k), default=0)

    points = []
    for i in range(max_len):
        points.append({
            "timestamp": data.get(key_map['power'], [{}])[i].get('ts'),
            "power": data.get(key_map['power'], [{}])[i].get('value', 0),
            "voltage": data.get(key_map['voltage'], [{}])[i].get('value', 0),
            "current": data.get(key_map['current'], [{}])[i].get('value', 0),
            "frequency": data.get(key_map['frequency'], [{}])[i].get('value', 0),
            "rmp": data.get(key_map['rmp'], [{}])[i].get('value', 0),
            "energy": data.get(key_map['energy'], [{}])[i].get('value', 0),
        })

    return jsonify({
        "data": points,
        "start_date": datetime.fromtimestamp(start_ts / 1000).strftime('%Y-%m-%d'),
        "end_date": datetime.fromtimestamp(end_ts / 1000).strftime('%Y-%m-%d'),
        "interval": "hourly" if days <= 7 else "daily",
        "online": True
    })

@app.route('/health')
def health():
    return jsonify({
        "status": "running",
        "thingsboard_accessible": check_internet_connection(),
        "timestamp": datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    })

if __name__ == '__main__':
    logger.info("Starting ThingsBoard Fetcher")
    app.run(host='0.0.0.0', port=5000, debug=False, threaded=True)
