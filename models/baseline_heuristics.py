def predict_max_temperature(features: dict) -> tuple[float, float]:
    current_temp = features["current_wsss_temp"]
    hr = features["hour_of_day"]
    
    solar_heating = max(0, (15 - hr) * 0.8) if hr < 15 else 0.2
    rain_penalty = features["rain_station_ratio"] * 2.5
    uv_boost = (features["uv_index"] / 10.0) * 0.5
    
    predicted_mu = max(current_temp, current_temp + solar_heating - rain_penalty + uv_boost)
    sigma = max(0.6, (16 - hr) * 0.15) if hr < 16 else 0.4
    
    return round(predicted_mu, 2), round(sigma, 2)