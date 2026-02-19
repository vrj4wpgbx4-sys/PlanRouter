from __future__ import annotations

import os
import re
import time
import math
from typing import Any, Dict, List, Optional, Tuple

import requests

# CacheDB import compatibility
try:
    from route_planner_ai.cache_db import CacheDB
except Exception:  # pragma: no cover
    from cache_db import CacheDB  # type: ignore


class RoutingError(Exception):
    pass


class RoutingClient:
    """
    GUI-facing client. Uses ORS via direct HTTP.

    Public methods:
      - get_routes(origin_text, destination_text)
      - get_route_with_stops(origin_text, stops, destination_text)  # ordered stops
      - geocode(text) -> (lat, lon)  [optional helper used by weather]
    """

    def __init__(self, profile: str = "driving-hgv", max_alternatives: int = 3):
        self.profile = profile
        self.max_alternatives = max_alternatives
        self.request_alternatives: Optional[bool] = None  # None => policy default

    def get_routes(self, origin_text: str, destination_text: str) -> Dict[str, Any]:
        return get_routes(
            origin_text=origin_text,
            destination_text=destination_text,
            profile=self.profile,
            max_alternatives=self.max_alternatives,
            request_alternatives=self.request_alternatives,
        )

    def get_route_with_stops(self, origin_text: str, stops: List[str], destination_text: str) -> Dict[str, Any]:
        return get_route_with_stops(
            origin_text=origin_text,
            stops=stops,
            destination_text=destination_text,
            profile=self.profile,
            max_alternatives=self.max_alternatives,
            request_alternatives=self.request_alternatives,
        )

    def geocode(self, text: str) -> Tuple[float, float]:
        lon, lat = _geocode_lonlat(text)
        return float(lat), float(lon)


# -----------------------
# Config
# -----------------------

MAX_DISTANCE_RATIO = 3.0
_MIN_STRAIGHTLINE_MILES = 0.25

GEOCODE_TTL_SECONDS = 30 * 24 * 60 * 60
_PURGE_EVERY_SECONDS = 6 * 60 * 60
HTTP_TIMEOUT_SECONDS = 20

# Radiuses (meters) used to let ORS snap points onto a routable edge.
# We progressively widen on code 2010.
RADIUS_STEPS_M = [350, 750, 1500, 3000, 6000, 10000]

_cache = CacheDB()
_last_purge_ts = 0


def _maybe_purge_expired() -> None:
    global _last_purge_ts
    now = int(time.time())
    if now - _last_purge_ts >= _PURGE_EVERY_SECONDS:
        try:
            _cache.purge_expired()
        except Exception:
            pass
        _last_purge_ts = now


def _clean_text(t: str) -> str:
    t = (t or "").strip()
    t = re.sub(r"\s+", " ", t)
    return t


def _normalize_for_cache(text: str) -> str:
    t = _clean_text(text).lower()
    t = t.replace(",", " ")
    t = re.sub(r"\s+", " ", t).strip()
    return t


def _meters_to_miles(m: float) -> float:
    return m / 1609.344


def _seconds_to_minutes(s: float) -> float:
    return s / 60.0


def _get_ors_key() -> str:
    key = os.environ.get("ORS_API_KEY")
    if not key:
        raise RoutingError("ORS_API_KEY not set. Configure it in your environment.")
    return key


def _get_ors_base_url() -> str:
    return (os.environ.get("ORS_BASE_URL") or "https://api.openrouteservice.org").rstrip("/")


def _http_post_json(url: str, headers: Dict[str, str], payload: Dict[str, Any]) -> Dict[str, Any]:
    """
    ORS errors often come back as JSON with {"error":{"code":..., "message":...}}
    on non-200 statuses. Capture that cleanly.
    """
    try:
        resp = requests.post(url, headers=headers, json=payload, timeout=HTTP_TIMEOUT_SECONDS)
    except requests.Timeout:
        raise RoutingError("ORS request timed out.")
    except requests.RequestException as e:
        raise RoutingError(f"ORS network error: {e}")

    # Try to parse JSON even on failure
    data: Optional[Dict[str, Any]] = None
    try:
        data = resp.json()
    except Exception:
        data = None

    if resp.status_code != 200:
        if isinstance(data, dict) and isinstance(data.get("error"), dict):
            err = data["error"]
            code = err.get("code")
            msg = err.get("message") or "ORS error"
            raise RoutingError(f"ORS error {code}: {msg}")
        body = (resp.text or "")[:400].replace("\n", " ")
        raise RoutingError(f"ORS HTTP {resp.status_code}: {body}")

    if not isinstance(data, dict):
        raise RoutingError("ORS returned invalid JSON.")

    return data


def _haversine_miles(a_lonlat: Tuple[float, float], b_lonlat: Tuple[float, float]) -> float:
    lon1, lat1 = a_lonlat
    lon2, lat2 = b_lonlat
    r = 3958.7613
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)

    s = math.sin(dphi / 2.0) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dl / 2.0) ** 2
    c = 2 * math.atan2(math.sqrt(s), math.sqrt(1 - s))
    return r * c


def _enforce_distance_sanity(
    *,
    routed_distance_miles: float,
    a_lonlat: Tuple[float, float],
    b_lonlat: Tuple[float, float],
    label_a: str,
    label_b: str,
    threshold: float = MAX_DISTANCE_RATIO,
) -> Dict[str, Any]:
    straight = _haversine_miles(a_lonlat, b_lonlat)
    straight = max(float(straight), _MIN_STRAIGHTLINE_MILES)
    ratio = float(routed_distance_miles) / straight if straight > 0 else float("inf")

    ok = ratio <= float(threshold)
    return {
        "ok": bool(ok),
        "ratio": float(ratio),
        "threshold": float(threshold),
        "straight_line_miles": float(straight),
        "routed_distance_miles": float(routed_distance_miles),
        "a": label_a,
        "b": label_b,
    }


# -----------------------
# Geocoding (ORS Pelias)
# -----------------------

def _geocode_lonlat(text: str) -> Tuple[float, float]:
    """
    Returns (lon, lat) using ORS Pelias /geocode/search.
    Cached by normalized text key.
    """
    _maybe_purge_expired()
    text_clean = _clean_text(text)
    if not text_clean:
        raise RoutingError("Geocode text is empty.")

    cache_key = f"geocode::{_normalize_for_cache(text_clean)}"
    cached = _cache.get("routing", cache_key)
    if isinstance(cached, dict) and "lon" in cached and "lat" in cached:
        return float(cached["lon"]), float(cached["lat"])

    base = _get_ors_base_url()
    headers = {"Authorization": _get_ors_key()}
    url = f"{base}/geocode/search"
    params = {"text": text_clean, "size": 1}

    try:
        resp = requests.get(url, headers=headers, params=params, timeout=HTTP_TIMEOUT_SECONDS)
    except requests.Timeout:
        raise RoutingError("Geocoding request timed out.")
    except requests.RequestException as e:
        raise RoutingError(f"Geocoding network error: {e}")

    if resp.status_code != 200:
        body = (resp.text or "")[:300].replace("\n", " ")
        raise RoutingError(f"Geocoding HTTP {resp.status_code}: {body}")

    try:
        data = resp.json()
    except Exception:
        raise RoutingError("Geocoding returned invalid JSON.")

    feats = data.get("features") or []
    if not feats:
        raise RoutingError(f"Geocoding returned no results for: {text_clean}")

    geom = feats[0].get("geometry") or {}
    coords = geom.get("coordinates")
    if not (isinstance(coords, list) and len(coords) == 2):
        raise RoutingError("Geocoding returned invalid coordinates.")

    lon = float(coords[0])
    lat = float(coords[1])

    _cache.set("routing", cache_key, {"lon": lon, "lat": lat}, GEOCODE_TTL_SECONDS)
    return lon, lat


# -----------------------
# Directions (with radiuses)
# -----------------------

def _directions_geojson(
    *,
    profile: str,
    coordinates: List[Tuple[float, float]],
    radiuses: Optional[List[int]],
    request_alternatives: bool,
    max_alternatives: int,
) -> Dict[str, Any]:
    base = _get_ors_base_url()
    headers = {"Authorization": _get_ors_key(), "Content-Type": "application/json"}
    url = f"{base}/v2/directions/{profile}/geojson"

    payload: Dict[str, Any] = {
        "coordinates": [list(pt) for pt in coordinates],
        "instructions": False,
    }

    if radiuses is not None:
        payload["radiuses"] = [int(r) for r in radiuses]

    if request_alternatives:
        # Best-effort; if ORS rejects alternatives (e.g., distance limits), we fall back in retry logic.
        payload["alternative_routes"] = {"target_count": max(1, int(max_alternatives))}

    return _http_post_json(url, headers=headers, payload=payload)


def _parse_point_index_from_2010(msg: str) -> Optional[int]:
    # Typical: "Could not find point 0: ... within a radius of 350.0 meters."
    m = re.search(r"Could not find point\s+(\d+)", msg or "", flags=re.IGNORECASE)
    if not m:
        return None
    try:
        return int(m.group(1))
    except Exception:
        return None


# -----------------------
# Public API
# -----------------------

def get_routes(
    origin_text: str,
    destination_text: str,
    *,
    profile: str = "driving-hgv",
    max_alternatives: int = 3,
    request_alternatives: Optional[bool] = None,
) -> Dict[str, Any]:
    origin_text = _clean_text(origin_text)
    destination_text = _clean_text(destination_text)
    if not origin_text or not destination_text:
        raise RoutingError("Both origin and destination are required.")

    if request_alternatives is None:
        request_alternatives = (profile != "driving-hgv")

    a = _geocode_lonlat(origin_text)       # (lon, lat)
    b = _geocode_lonlat(destination_text)  # (lon, lat)

    coords = [a, b]

    # Start with same radius for both points; widen on 2010 errors.
    request_alts = bool(request_alternatives)
    last_err: Optional[str] = None

    for step in RADIUS_STEPS_M:
        radiuses = [step] * len(coords)
        try:
            data = _directions_geojson(
                profile=profile,
                coordinates=coords,
                radiuses=radiuses,
                request_alternatives=request_alts,
                max_alternatives=max_alternatives,
            )
            break
        except RoutingError as e:
            msg = str(e)
            last_err = msg

            # If alternatives are rejected by ORS, retry once with alternatives disabled.
            if "2004" in msg and request_alts:
                request_alts = False
                continue

            # If we get 2010 (no routable point within radius), widening radius is exactly the fix.
            if "2010" in msg:
                continue

            # Other errors: stop.
            raise
    else:
        raise RoutingError(f"Could not route after radius retries. Last error: {last_err}")

    features = data.get("features") or []
    if not features:
        raise RoutingError("Routing returned no results.")

    routes_out: List[Dict[str, Any]] = []
    for feat in features:
        props = feat.get("properties") or {}
        summary = props.get("summary") or {}
        dist_m = float(summary.get("distance", 0.0) or 0.0)
        dur_s = float(summary.get("duration", 0.0) or 0.0)

        geom = feat.get("geometry")
        if not (isinstance(geom, dict) and isinstance(geom.get("coordinates"), list)):
            geom = {"type": "LineString", "coordinates": []}

        routes_out.append(
            {
                "summary": {
                    "distance_miles": _meters_to_miles(dist_m),
                    "duration_minutes": _seconds_to_minutes(dur_s),
                },
                "geometry": geom,
            }
        )

    sanity = _enforce_distance_sanity(
        routed_distance_miles=float(routes_out[0]["summary"]["distance_miles"]),
        a_lonlat=a,
        b_lonlat=b,
        label_a=origin_text,
        label_b=destination_text,
    )

    toll_info = {"available": False}
    return {"routes": routes_out, "toll": toll_info, "sanity": sanity}


def get_route_with_stops(
    origin_text: str,
    stops: List[str],
    destination_text: str,
    *,
    profile: str = "driving-hgv",
    max_alternatives: int = 3,
    request_alternatives: Optional[bool] = None,
) -> Dict[str, Any]:
    origin_text = _clean_text(origin_text)
    destination_text = _clean_text(destination_text)
    if not origin_text or not destination_text:
        raise RoutingError("Both origin and destination are required.")

    stops_clean = [_clean_text(s) for s in (stops or [])]
    stops_clean = [s for s in stops_clean if s]

    if len(stops_clean) > 20:
        raise RoutingError("Too many stops (max 20). Split the trip into multiple runs.")

    if request_alternatives is None:
        # Strong default: alternatives OFF for HGV (and generally for multi-stop).
        request_alternatives = False if profile == "driving-hgv" else False

    # Ordered waypoints: origin -> stops... -> destination
    coords: List[Tuple[float, float]] = []
    coords.append(_geocode_lonlat(origin_text))
    for i, s in enumerate(stops_clean, start=1):
        try:
            coords.append(_geocode_lonlat(s))
        except RoutingError as e:
            raise RoutingError(f"Stop {i} geocode failed ('{s}'): {e}")
    coords.append(_geocode_lonlat(destination_text))

    request_alts = bool(request_alternatives)
    last_err: Optional[str] = None

    # Radius retry loop (one radius applied to all waypoints each attempt)
    for step in RADIUS_STEPS_M:
        radiuses = [step] * len(coords)
        try:
            data = _directions_geojson(
                profile=profile,
                coordinates=coords,
                radiuses=radiuses,
                request_alternatives=request_alts,
                max_alternatives=max_alternatives,
            )
            break
        except RoutingError as e:
            msg = str(e)
            last_err = msg

            # If alternatives rejected, retry with alts disabled
            if "2004" in msg and request_alts:
                request_alts = False
                continue

            # 2010 -> widen radiuses (expected)
            if "2010" in msg:
                continue

            # Anything else: stop
            raise
    else:
        raise RoutingError(f"Could not route multi-drop after radius retries. Last error: {last_err}")

    features = data.get("features") or []
    if not features:
        raise RoutingError("Routing returned no results for multi-drop trip.")

    routes_out: List[Dict[str, Any]] = []
    for feat in features:
        props = feat.get("properties") or {}
        summary = props.get("summary") or {}
        dist_m = float(summary.get("distance", 0.0) or 0.0)
        dur_s = float(summary.get("duration", 0.0) or 0.0)

        geom = feat.get("geometry")
        if not (isinstance(geom, dict) and isinstance(geom.get("coordinates"), list)):
            geom = {"type": "LineString", "coordinates": []}

        routes_out.append(
            {
                "summary": {
                    "distance_miles": _meters_to_miles(dist_m),
                    "duration_minutes": _seconds_to_minutes(dur_s),
                },
                "geometry": geom,
            }
        )

    sanity = _enforce_distance_sanity(
        routed_distance_miles=float(routes_out[0]["summary"]["distance_miles"]),
        a_lonlat=coords[0],
        b_lonlat=coords[-1],
        label_a=origin_text,
        label_b=destination_text,
    )

    toll_info = {"available": False}
    return {"routes": routes_out, "toll": toll_info, "sanity": sanity}
