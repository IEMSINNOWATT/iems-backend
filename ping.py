import time
import requests

URLS = [
    "https://iems-backend.onrender.com",
    "https://iems-frontend.onrender.com"
]

while True:
    for url in URLS:
        try:
            res = requests.get(url)
            print(f"Pinged {url} - Status: {res.status_code}")
        except Exception as e:
            print(f"Error pinging {url}: {e}")
    time.sleep(600)  # Ping every 10 mins
