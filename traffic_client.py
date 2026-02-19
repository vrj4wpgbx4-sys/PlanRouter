from typing import Dict, Any, List, Optional, Tuple
import os
import time
import json
import hashlib
import requests

from cache_db import CacheDB


class TrafficError(Exception):
    """Custom exception for traffic/incidents-related errors."""
    pass


class TrafficClient:
    """
    TomTom Traffic Incident Details API (v5) client.

    Provides:
      - dispatcher summary text (body only; GUI prints header once)
      - structured stats for risk scoring
    """

    TOMTOM_API_KEY: str = "JN8yvrMuv69y4VOE95EycU75yxEo05iP"

    CACHE_TTL_SECONDS: int = 3 * 60
    PURGE_EVERY_SECONDS: int = 6 * 60 * 60

    def __init__(self, api_key: Optional[str] = None) -> None:
        self.api_key = api_key or os.environ.get("TOMTOM_API_KEY") or self.TOMTOM_API_KEY
        if not self.api_key:
            raise TrafficError("TomTom Traffic API key is missing.")

        self.session = requests.Session()
        self.base_url = "https://api.tomtom.com/traffic/services/5/incidentDetails"

        self.cache = CacheDB()
        self._last_purge_ts = 0

    # -------------------------
    # Cache helpers
    # -------------------------

    def _maybe_purge_expired(self) -> None:
        now = int(time.time())
        if now - self._last_purge_ts >= self.PURGE_EVERY_SECONDS:
            try:
                self.cache.purge_expired()
            except Exception:
                pass
            self._last_purge_ts = now

    @staticmethod
    def _geometry_cache_key(geometry: Any) -> Optional[str]:
        try:
            if isinstance(geometry, str) and geometry:
                h = hashlib.sha256(geometry.encode("utf-8")).hexdigest()[:24]
                return f"poly_{h}"

            if isinstance(geometry, dict) and isinstance(geometry.get("coordinates"), list):
                coords = geometry["coordinates"]
                norm = []
                for pt in coords[:5000]:
                    if isinstance(pt, (list, tuple)) and len(pt) == 2:
                        lon, lat = float(pt[0]), float(pt[1])
                        norm.append([round(lon, 5), round(lat, 5)])
                payload = json.dumps(norm, separators=(",", ":"), ensure_ascii=False)
                h = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:24]
                return f"coords_{h}"
        except Exception:
            return None
        return None

    # -------------------------
    # Polyline decoding
    # -------------------------

    @staticmethod
    def _decode_polyline(encoded: str) -> List[Tuple[float, float]]:
        if not encoded:
            return []

        index = 0
        lat = 0
        lng = 0
        coords: List[Tuple[float, float]] = []
        length = len(encoded)

        while index < length:
            result = 0
            shift = 0
            while True:
                b = ord(encoded[index]) - 63
                index += 1
                result |= (b & 0x1F) << shift
                shift += 5
                if b < 0x20:
                    break
            dlat = ~(result >> 1) if (result & 1) else (result >> 1)
            lat += dlat

            result = 0
            shift = 0
            while True:
                b = ord(encoded[index]) - 63
                index += 1
                result |= (b & 0x1F) << shift
                shift += 5
                if b < 0x20:
                    break
            dlng = ~(result >> 1) if (result & 1) else (result >> 1)
            lng += dlng

            coords.append((lng / 1e5, lat / 1e5))

        return coords

    # -------------------------
    # Helpers
    # -------------------------

    @staticmethod
    def _box_for_point(lon: float, lat: float, half_size_deg: float = 0.1) -> Tuple[float, float, float, float]:
        return (lon - half_size_deg, lat - half_size_deg, lon + half_size_deg, lat + half_size_deg)

    @staticmethod
    def _category_label(icon_category: Optional[int]) -> str:
        mapping = {
            1: "Accident",
            2: "Accident",
            3: "Accident",
            4: "Accident",
            5: "Dangerous conditions",
            6: "Road works",
            7: "Road works",
            8: "Road works",
            9: "Road works",
            10: "Road works",
            11: "Road closed",
            12: "Road closed",
            13: "Road closed",
            14: "Road closed",
            15: "Broken down vehicle",
        }
        try:
            return mapping.get(int(icon_category), "Incident")
        except Exception:
            return "Incident"

    @staticmethod
    def _severity_bucket(magnitude_of_delay: Optional[int]) -> str:
        try:
            m = int(magnitude_of_delay)
            if m >= 8:
                return "high"
            if m >= 4:
                return "moderate"
            return "low"
        except Exception:
            return "unknown"

    # -------------------------
    # Public API
    # -------------------------

    def get_traffic(self, geometry: Any) -> Tuple[str, Dict[str, Any]]:
        self._maybe_purge_expired()

        cache_key = self._geometry_cache_key(geometry)
        if cache_key:
            try:
                cached = self.cache.get("traffic_bundle", cache_key)
                if isinstance(cached, dict):
                    return cached["summary"], cached["stats"]
            except Exception:
                pass

        coords: List[Tuple[float, float]] = []
        if isinstance(geometry, dict) and isinstance(geometry.get("coordinates"), list):
            for pt in geometry["coordinates"]:
                if isinstance(pt, (list, tuple)) and len(pt) == 2:
                    coords.append((float(pt[0]), float(pt[1])))
        elif isinstance(geometry, str):
            coords = self._decode_polyline(geometry)

        if not coords:
            summary = "Traffic incidents near route:\n- Route geometry not available."
            stats = {"total": 0, "accidents": 0, "closures": 0, "roadworks": 0, "severity": {}}
            return summary, stats

        # Sample origin/mid/destination
        sample_points = [coords[0]]
        if len(coords) > 2:
            sample_points.append(coords[len(coords) // 2])
        if len(coords) > 1:
            sample_points.append(coords[-1])

        incidents_all: Dict[str, Dict[str, Any]] = {}
        for lon, lat in sample_points:
            min_lon, min_lat, max_lon, max_lat = self._box_for_point(lon, lat)
            params = {
                "key": self.api_key,
                "bbox": f"{min_lon},{min_lat},{max_lon},{max_lat}",
                "language": "en-US",
                "timeValidityFilter": "present",
            }

            try:
                resp = self.session.get(self.base_url, params=params, timeout=10)
                resp.raise_for_status()
                data = resp.json()
                for inc in data.get("incidents", []):
                    inc_id = str(inc.get("id") or hash(repr(inc)))
                    incidents_all[inc_id] = inc
            except Exception:
                continue

        incidents_list = list(incidents_all.values())
        total = len(incidents_list)

        type_counts: Dict[str, int] = {}
        accidents = closures = roadworks = 0

        for inc in incidents_list:
            props = inc.get("properties", {})
            cat = self._category_label(props.get("iconCategory"))
            type_counts[cat] = type_counts.get(cat, 0) + 1

            if cat == "Accident":
                accidents += 1
            elif cat == "Road closed":
                closures += 1
            elif cat == "Road works":
                roadworks += 1

        stats = {
            "total": total,
            "accidents": accidents,
            "closures": closures,
            "roadworks": roadworks,
        }

        lines = [
            "Traffic incidents near route:",
            f"- {total} total in sampled corridor.",
            "Breakdown by type:",
        ]

        if type_counts:
            for k in sorted(type_counts.keys()):
                lines.append(f"- {k}: {type_counts[k]}")
        else:
            lines.append("- None")

        summary = "\n".join(lines)

        if cache_key:
            try:
                self.cache.set("traffic_bundle", cache_key, {"summary": summary, "stats": stats}, self.CACHE_TTL_SECONDS)
            except Exception:
                pass

        return summary, stats

    def summarize_incidents_for_route(self, geometry: Any) -> str:
        summary, _ = self.get_traffic(geometry)
        return summary
