# api/route_service.py
"""
RoutePlanner service layer.
"""

from __future__ import annotations

import json
import math
import re
from datetime import datetime, timedelta, timezone
from typing import Tuple, List, Dict, Any, Optional
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import urlopen

from routing_client import RoutingClient, RoutingError

from .models import (
    RouteRequest,
    RouteResponse,
    ConditionsSummary,
    RiskComponent,
)


# ----------------------------
# CONSTANTS
# ----------------------------
DEFAULT_SAMPLE_SPACING_MILES = 50.0
MIN_SAMPLE_POINTS = 2
OPEN_METEO_FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
OPEN_METEO_HOURLY_VARS = [
    "temperature_2m",
    "precipitation_probability",
    "weather_code",
    "visibility",
    "wind_speed_10m",
    "wind_gusts_10m",
]
HIGHWAY_PATTERN = re.compile(r"\bI[- ]?\d+\b", re.IGNORECASE)


# ----------------------------
# ROUTE METRICS + SEGMENTS
# ----------------------------
def _compute_route_metrics(req: RouteRequest) -> Tuple[float, float, Dict[str, Any], List[Dict[str, Any]]]:
    client = RoutingClient(profile="driving-hgv")

    if req.stops:
        routes_result = client.get_route_with_stops(
            origin_text=req.origin,
            stops=[s.location for s in req.stops],
            destination_text=req.destination,
        )
    else:
        routes_result = client.get_routes(
            origin_text=req.origin,
            destination_text=req.destination,
        )

    routes = routes_result.get("routes") or []
    if not routes:
        raise RoutingError("Routing returned no routes.")

    primary = routes[0]
    summary = primary.get("summary") or {}
    geometry = primary.get("geometry") or {}
    segments = primary.get("segments") or []

    distance_miles = float(summary.get("distance_miles") or 0.0)
    if distance_miles <= 0:
        raise RoutingError("Routing returned zero distance.")

    avg_speed = req.avg_speed_mph if req.avg_speed_mph > 0 else 1.0
    eta_hours = distance_miles / avg_speed

    return distance_miles, eta_hours, geometry, segments


# ----------------------------
# DEPARTURE TIME HELPERS
# ----------------------------
def _coerce_departure_time(req: RouteRequest) -> str:
    raw_value = getattr(req, "departure_time", None)

    if raw_value in (None, "", "now"):
        return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")

    if isinstance(raw_value, datetime):
        if raw_value.tzinfo is None:
            raw_value = raw_value.replace(tzinfo=timezone.utc)
        return raw_value.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")

    try:
        parsed = datetime.fromisoformat(str(raw_value).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    except Exception:
        return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _parse_departure_datetime(departure_time: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(departure_time.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    except Exception:
        return datetime.now(timezone.utc).replace(microsecond=0)


# ----------------------------
# GEOMETRY HELPERS
# ----------------------------
def _extract_coordinates(geometry: Dict[str, Any]) -> List[List[float]]:
    coordinates = geometry.get("coordinates") or []

    if isinstance(coordinates, list) and coordinates:
        return coordinates

    return []


def _estimate_sample_count(distance_miles: float) -> int:
    sample_count = int(math.ceil(distance_miles / DEFAULT_SAMPLE_SPACING_MILES)) + 1
    return max(MIN_SAMPLE_POINTS, sample_count)


def _build_sample_indexes(coordinates: List[List[float]], distance_miles: float) -> List[int]:
    if not coordinates:
        return []

    sample_count = min(len(coordinates), _estimate_sample_count(distance_miles))
    if sample_count <= 1:
        return [0]

    last_index = len(coordinates) - 1
    return sorted(
        {
            min(last_index, round(i * last_index / (sample_count - 1)))
            for i in range(sample_count)
        }
    )


# ----------------------------
# WEATHER CODE MAPPING
# ----------------------------
def _weather_code_to_condition(weather_code: Optional[int]) -> str:
    mapping = {
        0: "clear",
        1: "mainly clear",
        2: "partly cloudy",
        3: "overcast",
        45: "fog",
        48: "depositing rime fog",
        51: "light drizzle",
        53: "moderate drizzle",
        55: "dense drizzle",
        56: "light freezing drizzle",
        57: "dense freezing drizzle",
        61: "slight rain",
        63: "moderate rain",
        65: "heavy rain",
        66: "light freezing rain",
        67: "heavy freezing rain",
        71: "slight snow",
        73: "moderate snow",
        75: "heavy snow",
        77: "snow grains",
        80: "slight rain showers",
        81: "moderate rain showers",
        82: "violent rain showers",
        85: "slight snow showers",
        86: "heavy snow showers",
        95: "thunderstorm",
        96: "thunderstorm with slight hail",
        99: "thunderstorm with heavy hail",
    }
    return mapping.get(weather_code, "unknown")


# ----------------------------
# OPEN-METEO FETCH
# ----------------------------
def _fetch_hourly_weather(lat: float, lon: float, departure_time: str, eta_time: str) -> Dict[str, Any]:
    departure_dt = _parse_departure_datetime(departure_time)
    eta_dt = _parse_departure_datetime(eta_time)
    latest_dt = max(departure_dt, eta_dt)
    forecast_days = max(1, min(16, math.ceil((latest_dt - departure_dt).total_seconds() / 86400) + 2))

    query = urlencode(
        {
            "latitude": f"{lat:.6f}",
            "longitude": f"{lon:.6f}",
            "hourly": ",".join(OPEN_METEO_HOURLY_VARS),
            "temperature_unit": "fahrenheit",
            "wind_speed_unit": "mph",
            "precipitation_unit": "inch",
            "timezone": "GMT",
            "forecast_days": str(forecast_days),
        }
    )

    url = f"{OPEN_METEO_FORECAST_URL}?{query}"

    try:
        with urlopen(url, timeout=10) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (HTTPError, URLError, TimeoutError, json.JSONDecodeError) as exc:
        return {
            "status": "error",
            "condition": "unknown",
            "temperature_f": None,
            "wind_mph": None,
            "wind_gust_mph": None,
            "precipitation_probability": None,
            "visibility_miles": None,
            "weather_code": None,
            "note": f"Weather fetch failed: {exc}",
        }

    hourly = payload.get("hourly") or {}
    times = hourly.get("time") or []
    if not times:
        return {
            "status": "error",
            "condition": "unknown",
            "temperature_f": None,
            "wind_mph": None,
            "wind_gust_mph": None,
            "precipitation_probability": None,
            "visibility_miles": None,
            "weather_code": None,
            "note": "Weather response did not include hourly data.",
        }

    target_hour = eta_dt.astimezone(timezone.utc).replace(minute=0, second=0, microsecond=0)
    target_hour_str = target_hour.strftime("%Y-%m-%dT%H:00")

    try:
        idx = times.index(target_hour_str)
    except ValueError:
        idx = min(
            range(len(times)),
            key=lambda i: abs(
                (
                    datetime.fromisoformat(times[i]).replace(tzinfo=timezone.utc)
                    - target_hour
                ).total_seconds()
            ),
        )

    weather_code = hourly.get("weather_code", [None])[idx]
    visibility_meters = hourly.get("visibility", [None])[idx]
    visibility_miles = None
    if visibility_meters is not None:
        visibility_miles = round(float(visibility_meters) * 0.000621371, 1)

    return {
        "status": "live",
        "condition": _weather_code_to_condition(weather_code),
        "temperature_f": hourly.get("temperature_2m", [None])[idx],
        "wind_mph": hourly.get("wind_speed_10m", [None])[idx],
        "wind_gust_mph": hourly.get("wind_gusts_10m", [None])[idx],
        "precipitation_probability": hourly.get("precipitation_probability", [None])[idx],
        "visibility_miles": visibility_miles,
        "weather_code": weather_code,
        "note": "Forecast matched to nearest available hourly model time.",
    }


# ----------------------------
# WEATHER RISK HELPERS
# ----------------------------
def _classify_wind_risk(wind_mph: Optional[float], wind_gust_mph: Optional[float]) -> Optional[str]:
    gust = float(wind_gust_mph) if wind_gust_mph is not None else 0.0
    wind = float(wind_mph) if wind_mph is not None else 0.0
    effective = max(wind, gust)

    if effective >= 45:
        return "high"
    if effective >= 30:
        return "moderate"
    if effective > 0:
        return "low"
    return None


def _classify_precipitation_risk(condition: str, precipitation_probability: Optional[float]) -> Optional[str]:
    probability = float(precipitation_probability) if precipitation_probability is not None else 0.0
    condition_lc = (condition or "").lower()

    if any(token in condition_lc for token in ["thunderstorm", "freezing", "heavy snow", "heavy rain", "violent"]):
        return "high"
    if probability >= 60 or any(token in condition_lc for token in ["snow", "moderate rain", "rain showers"]):
        return "moderate"
    if probability >= 20 or any(token in condition_lc for token in ["rain", "drizzle", "fog"]):
        return "low"
    return None


# ----------------------------
# WEATHER CHECKPOINTS
# ----------------------------
def _build_weather_checkpoints(
    geometry: Dict[str, Any],
    distance_miles: float,
    eta_hours: float,
    departure_time: str,
) -> List[Dict[str, Any]]:
    coordinates = _extract_coordinates(geometry)
    if not coordinates:
        return []

    sample_indexes = _build_sample_indexes(coordinates, distance_miles)
    departure_dt = _parse_departure_datetime(departure_time)
    checkpoints: List[Dict[str, Any]] = []

    for idx in sample_indexes:
        coord = coordinates[idx]
        if not isinstance(coord, (list, tuple)) or len(coord) < 2:
            continue

        lon = float(coord[0])
        lat = float(coord[1])
        progress = 0.0 if len(coordinates) == 1 else idx / max(1, len(coordinates) - 1)
        checkpoint_mile = round(distance_miles * progress, 1)
        checkpoint_eta = departure_dt + timedelta(hours=eta_hours * progress)
        checkpoint_eta_str = checkpoint_eta.replace(microsecond=0).isoformat()

        weather = _fetch_hourly_weather(
            lat=lat,
            lon=lon,
            departure_time=departure_time,
            eta_time=checkpoint_eta_str,
        )

        wind_risk = _classify_wind_risk(
            wind_mph=weather.get("wind_mph"),
            wind_gust_mph=weather.get("wind_gust_mph"),
        )
        precipitation_risk = _classify_precipitation_risk(
            condition=str(weather.get("condition") or "unknown"),
            precipitation_probability=weather.get("precipitation_probability"),
        )

        weather["wind_risk"] = wind_risk
        weather["precipitation_risk"] = precipitation_risk

        checkpoints.append(
            {
                "mile": checkpoint_mile,
                "progress": round(progress, 3),
                "lat": lat,
                "lon": lon,
                "eta": checkpoint_eta_str,
                "weather": weather,
            }
        )

    return checkpoints


# ----------------------------
# WEATHER SUMMARY
# ----------------------------
def _build_weather_summary(
    departure_time: str,
    checkpoints: List[Dict[str, Any]],
) -> Dict[str, Any]:
    if not checkpoints:
        return {
            "status": "error",
            "departure_time": departure_time,
            "summary": "Weather could not be evaluated because no route checkpoints were available.",
            "alerts": ["No route checkpoints were available for weather sampling."],
            "driver_notes": ["Route geometry did not provide checkpoint coordinates."],
            "wind_risk": "unknown",
            "precipitation_risk": "unknown",
            "checkpoints": [],
        }

    alerts: List[str] = []
    driver_notes: List[str] = []
    conditions_seen: List[str] = []
    overall_status = "live"
    wind_risk_rank = 0
    precipitation_risk_rank = 0
    risk_rank = {"unknown": 0, None: 0, "low": 1, "moderate": 2, "high": 3}
    reverse_rank = {0: "unknown", 1: "low", 2: "moderate", 3: "high"}

    for checkpoint in checkpoints:
        weather = checkpoint.get("weather") or {}
        if weather.get("status") != "live":
            overall_status = "partial" if overall_status == "live" else overall_status

        condition = str(weather.get("condition") or "unknown")
        if condition not in conditions_seen and condition != "unknown":
            conditions_seen.append(condition)

        wind_risk = weather.get("wind_risk")
        precipitation_risk = weather.get("precipitation_risk")
        wind_risk_rank = max(wind_risk_rank, risk_rank.get(wind_risk, 0))
        precipitation_risk_rank = max(precipitation_risk_rank, risk_rank.get(precipitation_risk, 0))

        mile = checkpoint.get("mile")
        gust = weather.get("wind_gust_mph")
        precip_prob = weather.get("precipitation_probability")
        visibility = weather.get("visibility_miles")

        if wind_risk == "high":
            alerts.append(f"High wind exposure near mile {mile} (gusts around {gust} mph).")
        elif wind_risk == "moderate":
            alerts.append(f"Moderate wind exposure near mile {mile} (gusts around {gust} mph).")

        if precipitation_risk == "high":
            alerts.append(f"High precipitation risk near mile {mile} ({condition}, {precip_prob}% chance).")
        elif precipitation_risk == "moderate":
            alerts.append(f"Moderate precipitation risk near mile {mile} ({condition}, {precip_prob}% chance).")

        if visibility is not None and visibility <= 2:
            alerts.append(f"Low visibility near mile {mile} ({visibility} miles).")

    if wind_risk_rank >= 3:
        driver_notes.append("High wind segments are present. Evaluate exposure for high-profile loads.")
    elif wind_risk_rank == 2:
        driver_notes.append("Moderate winds are present on parts of the route.")

    if precipitation_risk_rank >= 3:
        driver_notes.append("Heavy or hazardous precipitation is present along parts of the route.")
    elif precipitation_risk_rank == 2:
        driver_notes.append("Moderate precipitation is present along parts of the route.")

    if not driver_notes:
        driver_notes.append("No major weather-related operational concerns detected from current forecast sampling.")

    if not alerts:
        alerts.append("No major weather alerts detected from sampled checkpoints.")

    if not conditions_seen:
        conditions_seen.append("unknown")

    summary = (
        f"Route weather sampled across {len(checkpoints)} checkpoints. "
        f"Conditions observed: {', '.join(conditions_seen[:4])}. "
        f"Wind risk: {reverse_rank[wind_risk_rank]}. "
        f"Precipitation risk: {reverse_rank[precipitation_risk_rank]}."
    )

    return {
        "status": overall_status,
        "departure_time": departure_time,
        "summary": summary,
        "alerts": alerts,
        "driver_notes": driver_notes,
        "wind_risk": reverse_rank[wind_risk_rank],
        "precipitation_risk": reverse_rank[precipitation_risk_rank],
        "checkpoints": checkpoints,
    }


# ----------------------------
# CONDITIONS
# ----------------------------
def _compute_conditions(req: RouteRequest, weather_summary: Dict[str, Any]) -> ConditionsSummary:
    weather_text = str(weather_summary.get("summary") or "Weather not integrated yet.")
    alerts = list(weather_summary.get("alerts") or [])

    return ConditionsSummary(
        weather_summary=weather_text,
        traffic_summary="Traffic not integrated yet.",
        alerts=alerts,
    )


# ----------------------------
# RISK (baseline)
# ----------------------------
def _compute_risk(
    req: RouteRequest,
    distance_miles: float,
    conditions: ConditionsSummary,
) -> Tuple[float, str, List[RiskComponent]]:
    return 10.0, "LOW", []


# ----------------------------
# HIGHWAY EXTRACTION
# ----------------------------
def _extract_highways(segments: List[Dict[str, Any]]) -> List[str]:
    highways: List[str] = []

    for seg in segments:
        for step in seg.get("steps", []):
            instruction = str(step.get("instruction", "") or "")
            matches = HIGHWAY_PATTERN.findall(instruction)

            for match in matches:
                normalized = match.upper().replace(" ", "-")
                if normalized not in highways:
                    highways.append(normalized)

    return highways


# ----------------------------
# STATE INFERENCE FROM HIGHWAYS
# ----------------------------
HIGHWAY_STATE_MAP: Dict[str, List[str]] = {
    "I-90": ["MT", "WY", "SD", "MN", "WI", "IL"],
    "I-94": ["MT", "ND", "MN", "WI", "IL"],
    "I-35": ["TX", "OK", "KS", "MO", "IA", "MN"],
    "I-39": ["IL", "WI"],
    "I-694": ["MN"],
}


def _infer_states_from_highways(highways: List[str]) -> List[str]:
    states: List[str] = []

    for highway in highways:
        for state in HIGHWAY_STATE_MAP.get(highway, []):
            if state not in states:
                states.append(state)

    return states


# ----------------------------
# DRIVER NOTES ENGINE
# ----------------------------
def _build_driver_notes(
    highways: List[str],
    states: List[str],
    weather_summary: Dict[str, Any],
) -> List[str]:
    notes: List[str] = []

    if any(h in {"I-90", "I-94"} for h in highways):
        notes.append("northern corridor freight route")

    if "MT" in states:
        notes.append("mountain terrain early")

    if any(s in {"ND", "SD", "WY", "MT", "NE"} for s in states):
        notes.append("high crosswind exposure across plains")

    if "IL" in states:
        notes.append("heavy traffic congestion approaching Chicago")

    wind_risk = weather_summary.get("wind_risk")
    if wind_risk == "high":
        notes.append("high wind risk detected along route")
    elif wind_risk == "moderate":
        notes.append("moderate wind exposure along route")

    precipitation_risk = weather_summary.get("precipitation_risk")
    if precipitation_risk == "high":
        notes.append("high precipitation risk detected along route")
    elif precipitation_risk == "moderate":
        notes.append("moderate precipitation along route")

    if weather_summary.get("status") in {"partial", "error"}:
        notes.append("weather data was only partially available")

    if not notes:
        notes.append("standard highway conditions")

    return notes


# ----------------------------
# EXPLANATION ENGINE
# ----------------------------
def _build_route_explanation(
    distance_miles: float,
    eta_hours: float,
    highways: List[str],
    states: List[str],
    notes: List[str],
    risk_band: str,
    departure_time: str,
    weather_summary: Dict[str, Any],
) -> str:
    hours = int(eta_hours)
    minutes = int((eta_hours - hours) * 60)

    highways_str = ", ".join(highways) if highways else "regional highways"
    states_str = " → ".join(states) if states else "multi-state route"
    notes_str = "\n".join(f"- {note}" for note in notes)
    weather_line = str(weather_summary.get("summary") or "Weather summary unavailable.")

    return (
        f"{int(distance_miles)} miles (~{hours}h {minutes}m)\n\n"
        f"Departure Time:\n- {departure_time}\n\n"
        f"Primary Highways:\n- {highways_str}\n\n"
        f"States:\n- {states_str}\n\n"
        f"Weather:\n- {weather_line}\n\n"
        f"Driver Notes:\n{notes_str}\n\n"
        f"Overall: {risk_band.lower()} operational risk"
    )


# ----------------------------
# ACTION
# ----------------------------
def _derive_recommended_action(
    mode: str,
    risk_band: str,
    conditions: ConditionsSummary,
) -> str:
    alerts = conditions.alerts or []
    alerts_text = " ".join(str(alert).lower() for alert in alerts)

    if "high wind" in alerts_text or "high precipitation" in alerts_text or "low visibility" in alerts_text:
        return "Re-evaluate departure timing and route exposure before dispatch."

    if "moderate wind" in alerts_text or "moderate precipitation" in alerts_text:
        return "Proceed with caution and review route weather checkpoints."

    if risk_band == "LOW":
        return "Good to go."
    if risk_band == "MEDIUM":
        return "Stay alert."
    return "Re-evaluate route."


# ----------------------------
# MAIN ENTRY
# ----------------------------
def plan_route(req: RouteRequest) -> RouteResponse:
    distance_miles, eta_hours, geometry, segments = _compute_route_metrics(req)

    departure_time = _coerce_departure_time(req)
    weather_checkpoints = _build_weather_checkpoints(
        geometry=geometry,
        distance_miles=distance_miles,
        eta_hours=eta_hours,
        departure_time=departure_time,
    )
    weather_summary = _build_weather_summary(
        departure_time=departure_time,
        checkpoints=weather_checkpoints,
    )

    conditions = _compute_conditions(req, weather_summary)
    risk_score, risk_band, risk_components = _compute_risk(
        req=req,
        distance_miles=distance_miles,
        conditions=conditions,
    )

    highways = _extract_highways(segments)
    states = _infer_states_from_highways(highways)
    notes = _build_driver_notes(highways, states, weather_summary)

    explanation = _build_route_explanation(
        distance_miles=distance_miles,
        eta_hours=eta_hours,
        highways=highways,
        states=states,
        notes=notes,
        risk_band=risk_band,
        departure_time=departure_time,
        weather_summary=weather_summary,
    )

    meta = {
        "origin": req.origin,
        "destination": req.destination,
        "geometry": geometry,
        "highways": highways,
        "states": states,
        "driver_notes": notes,
        "explanation": explanation,
        "weather": weather_summary,
    }

    return RouteResponse(
        distance_miles=distance_miles,
        eta_hours=eta_hours,
        risk_score=risk_score,
        risk_band=risk_band,
        conditions=conditions,
        recommended_action=_derive_recommended_action(
            req.mode,
            risk_band,
            conditions,
        ),
        risk_components=risk_components,
        meta=meta,
    )
