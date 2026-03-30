from __future__ import annotations

import math
import os
import re
import time
from typing import Any, Dict, List, Optional, Tuple

import requests


class RoutingError(Exception):
    pass


# CacheDB import compatibility
try:
    from cache_db import CacheDB  # type: ignore
except Exception:  # pragma: no cover
    CacheDB = None  # type: ignore


# -----------------------
# Config
# -----------------------

MAX_DISTANCE_RATIO = 3.0
_MIN_STRAIGHTLINE_MILES = 0.25

GEOCODE_TTL_SECONDS = 30 * 24 * 60 * 60
_PURGE_EVERY_SECONDS = 6 * 60 * 60
HTTP_TIMEOUT_SECONDS = 20

# We progressively widen snap radiuses if ORS says a point is not routable.
RADIUS_STEPS_M = [350, 750, 1500, 3000, 6000, 10000]

_cache = CacheDB() if CacheDB is not None else None
_last_purge_ts = 0


def _maybe_purge_expired() -> None:
    global _last_purge_ts
    if _cache is None:
        return

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
    try:
        resp = requests.post(url, headers=headers, json=payload, timeout=HTTP_TIMEOUT_SECONDS)
    except requests.Timeout:
        raise RoutingError("ORS request timed out.")
    except requests.RequestException as e:
        raise RoutingError(f"ORS network error: {e}")

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
    Cached by normalized text key when CacheDB is available.
    """
    _maybe_purge_expired()
    text_clean = _clean_text(text)
    if not text_clean:
        raise RoutingError("Geocode text is empty.")

    cache_key = f"geocode::{_normalize_for_cache(text_clean)}"
    if _cache is not None:
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

    if _cache is not None:
        _cache.set("routing", cache_key, {"lon": lon, "lat": lat}, GEOCODE_TTL_SECONDS)

    return lon, lat


# -----------------------
# Directions
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
        "instructions": True,
        "instructions_format": "text",
    }

    if radiuses is not None:
        payload["radiuses"] = [int(r) for r in radiuses]

    if request_alternatives:
        payload["alternative_routes"] = {"target_count": max(1, int(max_alternatives))}

    return _http_post_json(url, headers=headers, payload=payload)


# -----------------------
# Corridor anchors for forced alternates
# -----------------------

ANCHOR_CITIES: List[Tuple[float, float, str]] = [
    (45.7833, -108.5007, "Billings"),
    (44.0805, -103.2310, "Rapid City"),
    (43.5446, -96.7311, "Sioux Falls"),
    (41.5868, -93.6250, "Des Moines"),
    (41.2565, -95.9345, "Omaha"),
    (39.0997, -94.5786, "Kansas City"),
    (39.7392, -104.9903, "Denver"),
    (41.1400, -104.8202, "Cheyenne"),
    (46.8083, -100.7837, "Bismarck"),
    (46.8772, -96.7898, "Fargo"),
    (44.9778, -93.2650, "Minneapolis"),
    (43.0731, -89.4012, "Madison"),
    (41.8781, -87.6298, "Chicago"),
    (39.7684, -86.1581, "Indianapolis"),
    (38.6270, -90.1994, "St. Louis"),
    (36.1627, -86.7816, "Nashville"),
    (40.7608, -111.8910, "Salt Lake City"),
    (43.6150, -116.2023, "Boise"),
    (47.6588, -117.4260, "Spokane"),
    (41.4993, -81.6944, "Cleveland"),
    (40.4406, -79.9959, "Pittsburgh"),
]


def _choose_forced_anchors(
    origin_lonlat: Tuple[float, float],
    dest_lonlat: Tuple[float, float],
) -> List[Tuple[str, Tuple[float, float]]]:
    """
    Pick up to 2 alternate corridor anchors for long trips.
    Returns list of (name, (lon, lat)).
    """
    o_lon, o_lat = origin_lonlat
    d_lon, d_lat = dest_lonlat

    lon_span = abs(d_lon - o_lon)
    lat_span = abs(d_lat - o_lat)
    mid_lon = (o_lon + d_lon) / 2.0
    mid_lat = (o_lat + d_lat) / 2.0

    east_west = lon_span >= lat_span

    scored: List[Tuple[float, str, Tuple[float, float]]] = []

    for lat, lon, name in ANCHOR_CITIES:
        if east_west:
            if not (min(o_lon, d_lon) - 4 <= lon <= max(o_lon, d_lon) + 4):
                continue
            offset = abs(lat - mid_lat)
            along = abs(lon - mid_lon)
            if offset < 1.5:
                continue
            score = along * 1.0 + abs(offset - 3.0) * 0.8
        else:
            if not (min(o_lat, d_lat) - 4 <= lat <= max(o_lat, d_lat) + 4):
                continue
            offset = abs(lon - mid_lon)
            along = abs(lat - mid_lat)
            if offset < 1.5:
                continue
            score = along * 1.0 + abs(offset - 3.0) * 0.8

        scored.append((score, name, (lon, lat)))

    scored.sort(key=lambda x: x[0])

    chosen: List[Tuple[str, Tuple[float, float]]] = []
    used_names: set[str] = set()

    for _, name, lonlat in scored:
        if name in used_names:
            continue
        chosen.append((name, lonlat))
        used_names.add(name)
        if len(chosen) >= 2:
            break

    return chosen


def _route_feature_to_output(feature: Dict[str, Any], forced_via: Optional[str] = None) -> Dict[str, Any]:
    props = feature.get("properties") or {}
    summary = props.get("summary") or {}
    dist_m = float(summary.get("distance", 0.0) or 0.0)
    dur_s = float(summary.get("duration", 0.0) or 0.0)

    geom = feature.get("geometry")
    if not (isinstance(geom, dict) and isinstance(geom.get("coordinates"), list)):
        geom = {"type": "LineString", "coordinates": []}

    out = {
        "summary": {
            "distance_miles": _meters_to_miles(dist_m),
            "duration_minutes": _seconds_to_minutes(dur_s),
        },
        "geometry": geom,
        "segments": props.get("segments") or [],
    }
    if forced_via:
        out["forced_via"] = forced_via
    return out


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
        request_alternatives = True

    a = _geocode_lonlat(origin_text)
    b = _geocode_lonlat(destination_text)

    coords = [a, b]
    request_alts = bool(request_alternatives)
    last_err: Optional[str] = None
    data: Optional[Dict[str, Any]] = None

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

            if "2004" in msg and request_alts:
                request_alts = False
                continue

            if "2010" in msg:
                continue

            raise
    else:
        raise RoutingError(f"Could not route after radius retries. Last error: {last_err}")

    if data is None:
        raise RoutingError("Routing returned no data.")

    features = data.get("features") or []
    if not features:
        raise RoutingError("Routing returned no results.")

    routes_out: List[Dict[str, Any]] = []

    for feat in features[: max(1, int(max_alternatives))]:
        routes_out.append(_route_feature_to_output(feat))

    straight_trip_miles = _haversine_miles(a, b)
    if len(routes_out) < 2 and straight_trip_miles >= 250:
        anchors = _choose_forced_anchors(a, b)
        for anchor_name, anchor_lonlat in anchors:
            try:
                forced_coords = [a, anchor_lonlat, b]
                forced_data: Optional[Dict[str, Any]] = None
                forced_err: Optional[str] = None

                for step in RADIUS_STEPS_M:
                    try:
                        forced_data = _directions_geojson(
                            profile=profile,
                            coordinates=forced_coords,
                            radiuses=[step] * len(forced_coords),
                            request_alternatives=False,
                            max_alternatives=1,
                        )
                        break
                    except RoutingError as e:
                        forced_err = str(e)
                        if "2010" in forced_err:
                            continue
                        raise

                if forced_data is None:
                    continue

                forced_features = forced_data.get("features") or []
                if not forced_features:
                    continue

                candidate = _route_feature_to_output(forced_features[0], forced_via=anchor_name)

                candidate_miles = float(candidate["summary"]["distance_miles"])
                primary_miles = float(routes_out[0]["summary"]["distance_miles"])
                if abs(candidate_miles - primary_miles) < 10:
                    continue

                sanity_candidate = _enforce_distance_sanity(
                    routed_distance_miles=candidate_miles,
                    a_lonlat=a,
                    b_lonlat=b,
                    label_a=origin_text,
                    label_b=destination_text,
                )
                if not sanity_candidate["ok"]:
                    continue

                routes_out.append(candidate)
                if len(routes_out) >= max(1, int(max_alternatives)):
                    break

            except Exception:
                continue

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
        request_alternatives = False

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
    data: Optional[Dict[str, Any]] = None

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

            if "2004" in msg and request_alts:
                request_alts = False
                continue

            if "2010" in msg:
                continue

            raise
    else:
        raise RoutingError(f"Could not route multi-drop after radius retries. Last error: {last_err}")

    if data is None:
        raise RoutingError("Routing returned no data for multi-drop trip.")

    features = data.get("features") or []
    if not features:
        raise RoutingError("Routing returned no results for multi-drop trip.")

    routes_out: List[Dict[str, Any]] = []
    for feat in features[:1]:
        routes_out.append(_route_feature_to_output(feat))

    sanity = _enforce_distance_sanity(
        routed_distance_miles=float(routes_out[0]["summary"]["distance_miles"]),
        a_lonlat=coords[0],
        b_lonlat=coords[-1],
        label_a=origin_text,
        label_b=destination_text,
    )

    toll_info = {"available": False}
    return {"routes": routes_out, "toll": toll_info, "sanity": sanity}


class RoutingClient:
    """
    GUI-facing client. Uses ORS via direct HTTP.

    Public methods:
      - get_routes(origin_text, destination_text, alternatives=None)
      - get_route_with_stops(origin_text, stops, destination_text, alternatives=None)
      - geocode(text) -> (lat, lon)
    """

    def __init__(self, profile: str = "driving-hgv", max_alternatives: int = 3):
        self.profile = profile
        self.max_alternatives = max_alternatives
        self.request_alternatives: Optional[bool] = None

    def get_routes(
        self,
        origin_text: str,
        destination_text: str,
        alternatives: Optional[bool] = None,
    ) -> Dict[str, Any]:
        request_alternatives = (
            alternatives if alternatives is not None else self.request_alternatives
        )
        return get_routes(
            origin_text=origin_text,
            destination_text=destination_text,
            profile=self.profile,
            max_alternatives=self.max_alternatives,
            request_alternatives=request_alternatives,
        )

    def get_route_with_stops(
        self,
        origin_text: str,
        stops: List[str],
        destination_text: str,
        alternatives: Optional[bool] = None,
    ) -> Dict[str, Any]:
        request_alternatives = (
            alternatives if alternatives is not None else self.request_alternatives
        )
        return get_route_with_stops(
            origin_text=origin_text,
            stops=stops,
            destination_text=destination_text,
            profile=self.profile,
            max_alternatives=self.max_alternatives,
            request_alternatives=request_alternatives,
        )

    def geocode(self, text: str) -> Tuple[float, float]:
        lon, lat = _geocode_lonlat(text)
        return float(lat), float(lon)