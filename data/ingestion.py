import concurrent.futures
import os
import requests
from dotenv import load_dotenv

load_dotenv()

DATA_GOV_API_KEY = os.getenv("DATA_GOV_API_KEY", "")
BASE_URL = "https://api-open.data.gov.sg/v2/real-time/api"
HEADERS = {"x-api-key": DATA_GOV_API_KEY} if DATA_GOV_API_KEY else {}

ENDPOINTS = {
    "air_temperature": f"{BASE_URL}/air-temperature",
    "relative_humidity": f"{BASE_URL}/relative-humidity",
    "rainfall": f"{BASE_URL}/rainfall",
    "wind_speed": f"{BASE_URL}/wind-speed",
    "wind_direction": f"{BASE_URL}/wind-direction",
    "uv_index": f"{BASE_URL}/uv",
    "two_hr_forecast": f"{BASE_URL}/two-hr-forecast",
    "twenty_four_hr_forecast": f"{BASE_URL}/twenty-four-hr-forecast",
    "four_day_outlook": f"{BASE_URL}/four-day-outlook",
    "radar_70km": f"{BASE_URL}/weather-radar-images/70km",
    "wbgt": f"{BASE_URL}/weather?api=wbgt",
    "lightning": f"{BASE_URL}/weather?api=lightning",
}

def _fetch_endpoint(name: str, url: str) -> tuple[str, dict]:
    try:
        res = requests.get(url, headers=HEADERS, timeout=8)
        res.raise_for_status()
        return name, res.json()
    except Exception as e:
        print(f"[Warning] Failed to fetch {name}: {e}")
        return name, {}

def fetch_all_data_gov() -> dict[str, dict]:
    """Concurrent fetcher for all 12 data.gov.sg real-time APIs."""
    results = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=12) as executor:
        futures = [
            executor.submit(_fetch_endpoint, name, url)
            for name, url in ENDPOINTS.items()
        ]
        for future in concurrent.futures.as_completed(futures):
            name, payload = future.result()
            results[name] = payload
    return results

def fetch_wsss_metar_history() -> list[dict]:
    """Fetch recent observation history for WSSS (Polymarket Target)."""
    url = "https://aviationweather.gov/api/data/metar?ids=WSSS&format=json&hours=24"
    try:
        res = requests.get(url, timeout=8)
        if res.status_code == 200:
            return res.json()
    except Exception as e:
        print(f"[Error] Failed to fetch WSSS METAR: {e}")
    return []