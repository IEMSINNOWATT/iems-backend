import time
import requests

# Replace this with your actual Render URL (your deployed server)
URL = "https://iems-backend.onrender.com"

def ping_render():
    try:
        response = requests.get(URL, timeout=10)
        if response.status_code == 200:
            print(f"[✓] Ping successful at {time.ctime()}")
        else:
            print(f"[!] Ping failed with status {response.status_code} at {time.ctime()}")
    except requests.exceptions.RequestException as e:
        print(f"[X] Ping error at {time.ctime()}: {e}")

if __name__ == "__main__":
    while True:
        ping_render()
        time.sleep(180)  # Sleep for 180 seconds (3 minutes)

