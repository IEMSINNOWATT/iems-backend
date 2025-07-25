
from flask import Flask, jsonify
from flask_cors import CORS
import requests
from datetime import datetime, timedelta
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
CORS(app, origins=["http://127.0.0.1:5500"])

# ----------------------------------------
# ThingsBoard Config
# ----------------------------------------
THINGSBOARD_HOST = 'https://demo.thingsboard.io'
USERNAME = os.getenv('TB_USERNAME')
PASSWORD = os.getenv('TB_PASSWORD')
DEVICE_ID = os.getenv('TB_DEVICE_ID')
JWT_TOKEN = os.getenv('TB_JWT_TOKEN')

# Case-insensitive key mapping (ThingsBoard keys -> our standardized lowercase keys)
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
# Check Internet
# ----------------------------------------
def check_internet_connection():
    try:
        socket.create_connection(("8.8.8.8", 53), timeout=5)
        return True
    except OSError:
        logger.warning("No internet connection available")
        return False

# ----------------------------------------
# Get JWT
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
# Fetch Telemetry
# ----------------------------------------
def fetch_telemetry(token, keys=None, start_ts=None, end_ts=None, interval=None, limit=None):
    if not token or not check_internet_connection():
        return None

    try:
        url = f"{THINGSBOARD_HOST}/api/plugins/telemetry/DEVICE/{DEVICE_ID}/values/timeseries"
        params = {}

        if keys:
            # Convert our standardized keys to possible ThingsBoard keys
            tb_keys = []
            for key in keys:
                tb_keys.extend(TELEMETRY_KEY_MAPPING.get(key.lower(), [key]))
            params['keys'] = ','.join(set(tb_keys))  # Remove duplicates
        
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
# Helper Functions
# ----------------------------------------
def find_matching_key(data, possible_keys):
    """Find the first matching key in the data for any of the possible keys"""
    for key in possible_keys:
        if key in data:
            return key
    return None

def get_value_and_timestamp(data, standard_key):
    """Get value and timestamp for a standard key, checking all possible variations"""
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

    # Get values using our standardized keys
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
# API Endpoints
# ----------------------------------------
@app.route('/api/telemetry')
def get_telemetry():
    try:
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

        # Handle ngrok_url separately
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

except Exception as e:
    logger.exception("❌ Unhandled error in /api/telemetry")
    return jsonify({"error": str(e), "online": False}), 500


@app.route('/api/telemetry/weekly')
def get_weekly_telemetry():
    token = JWT_TOKEN if JWT_TOKEN else get_auth_token()
    if not token:
        return jsonify({"error": "Authentication failed", "online": False}), 401

    start_ts, end_ts = get_time_range(7)
    telemetry_data = fetch_telemetry(
        token,
        keys=['power', 'voltage', 'current', 'frequency', 'rmp', 'energy'],
        start_ts=start_ts,
        end_ts=end_ts,
        interval=3600000,
        limit=168
    )

    if not telemetry_data:
        return jsonify({"error": "Could not fetch weekly telemetry", "online": False}), 500

    # Process the data points
    processed_data = []
    
    # Get all possible power keys
    power_keys = TELEMETRY_KEY_MAPPING.get('power', ['power'])
    actual_power_key = find_matching_key(telemetry_data, power_keys) or 'power'
    
    # Get all possible voltage keys
    voltage_keys = TELEMETRY_KEY_MAPPING.get('voltage', ['voltage'])
    actual_voltage_key = find_matching_key(telemetry_data, voltage_keys) or 'voltage'
    
    # Similarly for other metrics
    current_keys = TELEMETRY_KEY_MAPPING.get('current', ['current'])
    actual_current_key = find_matching_key(telemetry_data, current_keys) or 'current'
    
    frequency_keys = TELEMETRY_KEY_MAPPING.get('frequency', ['frequency'])
    actual_frequency_key = find_matching_key(telemetry_data, frequency_keys) or 'frequency'
    
    rmp_keys = TELEMETRY_KEY_MAPPING.get('rmp', ['rmp'])
    actual_rmp_key = find_matching_key(telemetry_data, rmp_keys) or 'rmp'
    
    energy_keys = TELEMETRY_KEY_MAPPING.get('energy', ['energy'])
    actual_energy_key = find_matching_key(telemetry_data, energy_keys) or 'energy'

    # Get the maximum number of data points available
    max_points = len(telemetry_data.get(actual_power_key, []))
    
    for i in range(max_points):
        point = {
            "timestamp": telemetry_data[actual_power_key][i]['ts'],
            "power": telemetry_data[actual_power_key][i]['value'],
            "voltage": telemetry_data[actual_voltage_key][i]['value'] if i < len(telemetry_data.get(actual_voltage_key, [])) else 0,
            "current": telemetry_data[actual_current_key][i]['value'] if i < len(telemetry_data.get(actual_current_key, [])) else 0,
            "frequency": telemetry_data[actual_frequency_key][i]['value'] if i < len(telemetry_data.get(actual_frequency_key, [])) else 0,
            "rmp": telemetry_data[actual_rmp_key][i]['value'] if i < len(telemetry_data.get(actual_rmp_key, [])) else 0,
            "energy": telemetry_data[actual_energy_key][i]['value'] if i < len(telemetry_data.get(actual_energy_key, [])) else 0
        }
        processed_data.append(point)

    return jsonify({
        "data": processed_data,
        "start_date": datetime.fromtimestamp(start_ts / 1000).strftime('%Y-%m-%d'),
        "end_date": datetime.fromtimestamp(end_ts / 1000).strftime('%Y-%m-%d'),
        "interval": "hourly",
        "online": True
    })

@app.route('/api/telemetry/monthly')
def get_monthly_telemetry():
    token = JWT_TOKEN if JWT_TOKEN else get_auth_token()
    if not token:
        return jsonify({"error": "Authentication failed", "online": False}), 401

    start_ts, end_ts = get_time_range(30)
    telemetry_data = fetch_telemetry(
        token,
        keys=['power', 'voltage', 'current', 'frequency', 'rmp', 'energy'],
        start_ts=start_ts,
        end_ts=end_ts,
        interval=86400000,
        limit=30
    )

    if not telemetry_data:
        return jsonify({"error": "Could not fetch monthly telemetry", "online": False}), 500

    # Process the data points (similar to weekly but with daily interval)
    processed_data = []
    
    # Get all actual keys (same as weekly endpoint)
    power_keys = TELEMETRY_KEY_MAPPING.get('power', ['power'])
    actual_power_key = find_matching_key(telemetry_data, power_keys) or 'power'
    
    voltage_keys = TELEMETRY_KEY_MAPPING.get('voltage', ['voltage'])
    actual_voltage_key = find_matching_key(telemetry_data, voltage_keys) or 'voltage'
    
    current_keys = TELEMETRY_KEY_MAPPING.get('current', ['current'])
    actual_current_key = find_matching_key(telemetry_data, current_keys) or 'current'
    
    frequency_keys = TELEMETRY_KEY_MAPPING.get('frequency', ['frequency'])
    actual_frequency_key = find_matching_key(telemetry_data, frequency_keys) or 'frequency'
    
    rmp_keys = TELEMETRY_KEY_MAPPING.get('rmp', ['rmp'])
    actual_rmp_key = find_matching_key(telemetry_data, rmp_keys) or 'rmp'
    
    energy_keys = TELEMETRY_KEY_MAPPING.get('energy', ['energy'])
    actual_energy_key = find_matching_key(telemetry_data, energy_keys) or 'energy'

    max_points = len(telemetry_data.get(actual_power_key, []))
    
    for i in range(max_points):
        point = {
            "timestamp": telemetry_data[actual_power_key][i]['ts'],
            "power": telemetry_data[actual_power_key][i]['value'],
            "voltage": telemetry_data[actual_voltage_key][i]['value'] if i < len(telemetry_data.get(actual_voltage_key, [])) else 0,
            "current": telemetry_data[actual_current_key][i]['value'] if i < len(telemetry_data.get(actual_current_key, [])) else 0,
            "frequency": telemetry_data[actual_frequency_key][i]['value'] if i < len(telemetry_data.get(actual_frequency_key, [])) else 0,
            "rmp": telemetry_data[actual_rmp_key][i]['value'] if i < len(telemetry_data.get(actual_rmp_key, [])) else 0,
            "energy": telemetry_data[actual_energy_key][i]['value'] if i < len(telemetry_data.get(actual_energy_key, [])) else 0
        }
        processed_data.append(point)

    return jsonify({
        "data": processed_data,
        "start_date": datetime.fromtimestamp(start_ts / 1000).strftime('%Y-%m-%d'),
        "end_date": datetime.fromtimestamp(end_ts / 1000).strftime('%Y-%m-%d'),
        "interval": "daily",
        "online": True
    })

@app.route('/health')
def health_check():
    return jsonify({
        "status": "running",
        "thingsboard_accessible": check_internet_connection(),
        "timestamp": datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    })
@app.errorhandler(500)
def internal_error(error):
    logger.error(f"Unhandled 500 error: {error}")
    return jsonify({"error": "Internal server error", "online": False}), 500

if __name__ == '__main__':
    __import__('threading').Thread(target=lambda: __import__('subprocess').Popen(['python', 'ping.py']), daemon=True).start()
    logger.info("Starting ThingsBoard Data Fetcher Service")
    app.run(host='0.0.0.0', port=5000, debug=False)
