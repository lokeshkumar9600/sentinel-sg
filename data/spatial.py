"""Spatial layer extraction — converts all gov API payloads into a single
serializable object the frontend can render directly.

Reuses the same parsing logic as feature_engine.py so the map and the model
stay in sync. All coordinates are WGS84 lat/lon.
"""

from datetime import datetime
from zoneinfo import ZoneInfo

from data.feature_engine import haversine, CHANGI_LAT, CHANGI_LON

SGT = ZoneInfo("Asia/Singapore")


def _f(v, default: float):
    try:
        f = float(v)
        return f if f == f else default  # NaN check
    except (TypeError, ValueError):
        return default


def _station_id_to_name(stations: list[dict], station_id: str) -> str:
    for s in stations:
        if s.get("id") == station_id:
            return s.get("name", station_id)
    return station_id


def extract_spatial_layers(raw_data: dict) -> dict:
    """Return a JSON-serializable dict with all live spatial layers."""
    layers = {}

    # If no real data, generate demo data for the frontend
    if not raw_data:
        # Demo temperature stations around Singapore
        demo_stations = [
            {"lat": 1.3644, "lon": 103.9915, "name": "Changi", "value": 31.5},
            {"lat": 1.3167, "lon": 103.8500, "name": "Raffles", "value": 32.2},
            {"lat": 1.3521, "lon": 103.8198, "name": "City", "value": 31.8},
            {"lat": 1.3571, "lon": 103.9877, "name": "Pasir Ris", "value": 30.9},
            {"lat": 1.3279, "lon": 103.9144, "name": "Kallang", "value": 31.7},
            {"lat": 1.3039, "lon": 103.8314, "name": "Jurong", "value": 32.5},
            {"lat": 1.3891, "lon": 103.7445, "name": "Woodlands", "value": 31.2},
            {"lat": 1.4281, "lon": 103.7865, "name": "Yishun", "value": 30.8},
        ]
        layers["air_temperature"] = {"unit": "°C", "points": demo_stations}
        layers["rainfall"] = {"unit": "mm", "points": [
            {"lat": 1.35, "lon": 103.85, "name": "City", "value": 1.2},
            {"lat": 1.38, "lon": 103.90, "name": "Bedok", "value": 0.5},
        ]}
        layers["wind"] = {"unit": "kt", "points": [
            {"lat": 1.36, "lon": 103.98, "name": "Changi", "speed": 7.5, "dir": 140},
            {"lat": 1.32, "lon": 103.85, "name": "Raffles", "speed": 5.2, "dir": 160},
        ]}
        layers["lightning"] = {"count": 2, "points": [
            {"lat": 1.40, "lon": 103.75},
            {"lat": 1.33, "lon": 103.92},
        ]}
        layers["uv"] = {"label": "National", "value": 6}
        layers["wbgt"] = {"label": "—", "value": 29.5}
        layers["two_hr_forecast"] = [{"area": "Central", "forecast": "Partly cloudy"}]
        layers["twenty_four_hr_forecast"] = "Warm and humid with isolated showers"
        layers["four_day_outlook"] = ["Sunny intervals", "Scattered showers", "Mostly cloudy", "Thundery showers"]
        layers["radar_url"] = "https://data.gov.sg/dataset/weather-radar-images"

        return {
            "generated_at_sgt": datetime.now(SGT).strftime("%Y-%m-%d %H:%M:%S SGT"),
            "changi": {"lat": CHANGI_LAT, "lon": CHANGI_LON},
            "layers": layers
        }

    # Helper: common shape for station-value layers
    def _read_station_layer(dataset_key: str, value_key: str = "value") -> list[dict]:
        data = raw_data.get(dataset_key, {}).get("data", {})
        readings = data.get("readings", [])
        if not readings:
            return []
        stations = {s["id"]: (s["location"]["latitude"], s["location"]["longitude"], s.get("name", s["id"]))
                    for s in data.get("stations", [])}
        out = []
        for r in readings[0].get("data", []):
            st_id = r.get("stationId")
            val = r.get(value_key)
            if st_id in stations and val is not None:
                lat, lon, name = stations[st_id]
                out.append({"lat": lat, "lon": lon, "name": name, "value": _f(val, 0.0)})
        return out

    # 1. Air temperature — per-station °C
    layers["air_temperature"] = {
        "unit": "°C",
        "points": _read_station_layer("air_temperature")
    }

    # 2. Rainfall — per-station mm (5-min)
    layers["rainfall"] = {
        "unit": "mm",
        "points": _read_station_layer("rainfall")
    }

    # 3. Relative humidity — per-station %
    layers["relative_humidity"] = {
        "unit": "%",
        "points": _read_station_layer("relative_humidity")
    }

    # 4. Wind — merged speed + direction from two endpoints
    #    Both endpoints share the same station list; merge on stationId.
    wind_speed_data = raw_data.get("wind_speed", {}).get("data", {})
    wind_dir_data = raw_data.get("wind_direction", {}).get("data", {})
    wspd_readings = wind_speed_data.get("readings", [])
    wdir_readings = wind_dir_data.get("readings", [])
    if wspd_readings and wdir_readings:
        stations = {s["id"]: (s["location"]["latitude"], s["location"]["longitude"], s.get("name", s["id"]))
                    for s in wind_speed_data.get("stations", [])}
        # build speed dict
        speed_map = {}
        for r in wspd_readings[0].get("data", []):
            st_id = r.get("stationId")
            val = r.get("value")
            if st_id in stations and val is not None:
                speed_map[st_id] = _f(val, 0.0)
        # build dir dict
        dir_map = {}
        for r in wdir_readings[0].get("data", []):
            st_id = r.get("stationId")
            val = r.get("value")
            if st_id in stations and val is not None:
                dir_map[st_id] = _f(val, 0.0)
        points = []
        for st_id, (lat, lon, name) in stations.items():
            if st_id in speed_map and st_id in dir_map:
                points.append({
                    "lat": lat,
                    "lon": lon,
                    "name": name,
                    "speed": speed_map[st_id],
                    "dir": dir_map[st_id]
                })
        layers["wind"] = {
            "unit": "kt",
            "points": points
        }
    else:
        layers["wind"] = {"unit": "kt", "points": []}

    # 5. UV index — national scalar
    uv_records = raw_data.get("uv_index", {}).get("data", {}).get("records", [])
    layers["uv"] = {
        "label": "National",
        "value": uv_records[0].get("value", 0) if uv_records else 0
    }

    # 6. WBGT — national scalar
    wbgt_records = raw_data.get("wbgt", {}).get("data", {}).get("records", [])
    layers["wbgt"] = {
        "label": "—",
        "value": max([r.get("value", 0) for r in wbgt_records], default=27.0)
    }

    # 7. Lightning — per-strike lat/lon
    lightning_data = raw_data.get("lightning", {}).get("data", {})
    lightning_readings = lightning_data.get("readings", [])
    lightning_points = []
    for r in lightning_readings:
        loc = r.get("geoLocation")
        if loc and "latitude" in loc and "longitude" in loc:
            lightning_points.append({"lat": loc["latitude"], "lon": loc["longitude"]})
    layers["lightning"] = {
        "count": len(lightning_points),
        "points": lightning_points
    }

    # 8. Two-hour forecast — area name + forecast text
    two_hr = raw_data.get("two_hr_forecast", {}).get("data", {})
    items = two_hr.get("items", [])
    two_hr_forecasts = []
    if items:
        for f in items[0].get("forecasts", []):
            area = f.get("area")
            forecast = f.get("forecast")
            if area and forecast:
                two_hr_forecasts.append({"area": area, "forecast": forecast})
    layers["two_hr_forecast"] = two_hr_forecasts

    # 9. 24-hour forecast — text summary
    twentyfour = raw_data.get("twenty_four_hr_forecast", {}).get("data", {})
    items24 = twentyfour.get("items", [])
    twentyfour_text = ""
    if items24:
        twentyfour_text = items24[0].get("periods", [{}])[0].get("text", "") or items24[0].get("text", "")
    layers["twenty_four_hr_forecast"] = twentyfour_text

    # 10. Four-day outlook — list of daily summaries
    four_day = raw_data.get("four_day_outlook", {}).get("data", {})
    items4 = four_day.get("items", [])
    four_day_list = []
    if items4:
        for day in items4:
            day_text = f"{day.get('date', '')}: {day.get('text', '')}"
            four_day_list.append(day_text.strip())
    layers["four_day_outlook"] = four_day_list

    # 11. Radar image URL
    radar_data = raw_data.get("radar_70km", {}).get("data", {})
    radar_url = radar_data.get("imageUrl") or radar_data.get("url")
    layers["radar_url"] = radar_url

    return {
        "generated_at_sgt": datetime.now(SGT).strftime("%Y-%m-%d %H:%M:%S SGT"),
        "changi": {"lat": CHANGI_LAT, "lon": CHANGI_LON},
        "layers": layers
    }