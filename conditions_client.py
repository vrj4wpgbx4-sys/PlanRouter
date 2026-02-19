from __future__ import annotations

from typing import Dict, Any, Optional
from datetime import datetime, timezone
import time
import requests

# Keep legacy absolute import compatibility
try:
    from route_planner_ai.cache_db import CacheDB
except Exception:  # pragma: no cover
    from cache_db import CacheDB  # type: ignore


# Bump this any time we rewrite so the report proves which file is being used
CONDITIONS_CLIENT_VERSION = "2026-02-19.v3"


class ConditionsError(Exception):
    """Weather/conditions client error with user-friendly message."""
    pass


class ConditionsClient:
    """
    Open-Meteo weather client with:

    - Snapshot mode (current conditions)  ← existing behavior (Dispatcher)
    - ETA-aligned forecast mode           ← new behavior (Driver Phase 1)
    """

    BASE_URL = "https://api.open-meteo.com/v1/forecast"

    POINT_TTL_SECONDS = 10 * 60
    PURGE_EVERY_SECONDS = 6 * 60 * 60
    TIMEOUT_SECONDS = 12

    def __init__(self) -> None:
        self.session = requests.Session()
        self.cache = CacheDB()
        self._last_purge_ts = 0

    # ============================================================
    # EXISTING SNAPSHOT MODE (UNCHANGED)
    # ============================================================

    def get_route_weather(
        self,
        origin_lat: float,
        origin_lon: float,
        dest_lat: float,
        dest_lon: float,
    ) -> str:

        self._maybe_purge_expired()

        mid_lat = (origin_lat + dest_lat) / 2.0
        mid_lon = (origin_lon + dest_lon) / 2.0

        o = self._fetch_point_weather(origin_lat, origin_lon)
        m = self._fetch_point_weather(mid_lat, mid_lon)
        d = self._fetch_point_weather(dest_lat, dest_lon)

        return (
            f"Weather snapshot along route (current conditions) [{CONDITIONS_CLIENT_VERSION}]:\n"
            f"- Origin ({origin_lat:.2f}, {origin_lon:.2f}): {o['description']}, "
            f"{o['temp_f']:.1f}°F ({o['temp_c']:.1f}°C), wind {o['wind_kmh']:.0f} km/h\n"
            f"- Midpoint ({mid_lat:.2f}, {mid_lon:.2f}): {m['description']}, "
            f"{m['temp_f']:.1f}°F ({m['temp_c']:.1f}°C), wind {m['wind_kmh']:.0f} km/h\n"
            f"- Destination ({dest_lat:.2f}, {dest_lon:.2f}): {d['description']}, "
            f"{d['temp_f']:.1f}°F ({d['temp_c']:.1f}°C), wind {d['wind_kmh']:.0f} km/h"
        )

    # ============================================================
    # NEW: ETA-ALIGNED FORECAST MODE (Driver Phase 1)
    # ============================================================

    def get_route_weather_with_eta(
        self,
        origin_lat: float,
        origin_lon: float,
        dest_lat: float,
        dest_lon: float,
        eta_midpoint: datetime,
        eta_destination: datetime,
    ) -> str:
        """
        Time-aware weather forecast aligned to driver ETAs.

        ETAs must be timezone-aware datetimes.
        """

        self._maybe_purge_expired()

        mid_lat = (origin_lat + dest_lat) / 2.0
        mid_lon = (origin_lon + dest_lon) / 2.0

        m = self._fetch_point_forecast_for_eta(mid_lat, mid_lon, eta_midpoint)
        d = self._fetch_point_forecast_for_eta(dest_lat, dest_lon, eta_destination)

        return (
            f"Weather forecast aligned to ETA [{CONDITIONS_CLIENT_VERSION}]:\n"
            f"- Midpoint ETA ({eta_midpoint.isoformat()}): "
            f"{m['description']}, {m['temp_f']:.1f}°F, wind {m['wind_kmh']:.0f} km/h\n"
            f"- Destination ETA ({eta_destination.isoformat()}): "
            f"{d['description']}, {d['temp_f']:.1f}°F, wind {d['wind_kmh']:.0f} km/h"
        )

    # ============================================================
    # INTERNALS
    # ============================================================

    def _maybe_purge_expired(self) -> None:
        now = int(time.time())
        if now - self._last_purge_ts >= self.PURGE_EVERY_SECONDS:
            try:
                self.cache.purge_expired()
            except Exception:
                pass
            self._last_purge_ts = now

    # ------------------------------------------------------------
    # Snapshot mode internals (unchanged)
    # ------------------------------------------------------------

    def _fetch_point_weather(self, lat: float, lon: float) -> Dict[str, Any]:
        cache_key = f"wx_{lat:.4f}_{lon:.4f}"
        cached = self._cache_get(cache_key)

        if isinstance(cached, dict) and self._looks_valid_cached_weather(cached):
            return cached

        data = self._call_open_meteo_current(lat, lon)
        parsed = self._parse_open_meteo_current(data)

        self._cache_set(cache_key, parsed)
        return parsed

    # ------------------------------------------------------------
    # NEW: Forecast mode internals
    # ------------------------------------------------------------

    def _fetch_point_forecast_for_eta(
        self,
        lat: float,
        lon: float,
        eta: datetime,
    ) -> Dict[str, Any]:

        if eta.tzinfo is None:
            raise ConditionsError(
                f"[{CONDITIONS_CLIENT_VERSION}] ETA must be timezone-aware."
            )

        eta_utc = eta.astimezone(timezone.utc)
        data = self._call_open_meteo_hourly(lat, lon)

        hourly = data.get("hourly")
        if not hourly:
            raise ConditionsError(
                f"[{CONDITIONS_CLIENT_VERSION}] Hourly forecast missing."
            )

        times = hourly.get("time", [])
        temps = hourly.get("temperature_2m", [])
        winds = hourly.get("wind_speed_10m", [])
        codes = hourly.get("weather_code", [])

        if not times:
            raise ConditionsError(
                f"[{CONDITIONS_CLIENT_VERSION}] Hourly time array empty."
            )

        eta_ts = int(eta_utc.timestamp())
        best_idx = 0
        best_diff = float("inf")

        for i, t in enumerate(times):
            ts = int(t)
            diff = abs(ts - eta_ts)
            if diff < best_diff:
                best_diff = diff
                best_idx = i

        temp_f = float(temps[best_idx])
        wind_kmh = float(winds[best_idx])
        code = int(codes[best_idx])

        return self._build_weather(temp_f, wind_kmh, code)

    # ------------------------------------------------------------
    # API calls
    # ------------------------------------------------------------

    def _call_open_meteo_current(self, lat: float, lon: float) -> Dict[str, Any]:
        params = {
            "latitude": lat,
            "longitude": lon,
            "current": "temperature_2m,weather_code,wind_speed_10m",
            "temperature_unit": "fahrenheit",
            "wind_speed_unit": "kmh",
        }
        return self._http_get_json(self.BASE_URL, params)

    def _call_open_meteo_hourly(self, lat: float, lon: float) -> Dict[str, Any]:
        params = {
            "latitude": lat,
            "longitude": lon,
            "hourly": "temperature_2m,weather_code,wind_speed_10m",
            "temperature_unit": "fahrenheit",
            "wind_speed_unit": "kmh",
            "timeformat": "unixtime",
            "timezone": "GMT",
            "forecast_days": 7,
        }
        return self._http_get_json(self.BASE_URL, params)

    # ------------------------------------------------------------
    # Shared helpers
    # ------------------------------------------------------------

    def _http_get_json(self, url: str, params: Dict[str, Any]) -> Dict[str, Any]:
        try:
            resp = self.session.get(url, params=params, timeout=self.TIMEOUT_SECONDS)
        except requests.Timeout:
            raise ConditionsError(
                f"[{CONDITIONS_CLIENT_VERSION}] Weather timed out."
            )
        except requests.RequestException as e:
            raise ConditionsError(
                f"[{CONDITIONS_CLIENT_VERSION}] Weather network error: {e!r}"
            )

        if resp.status_code != 200:
            raise ConditionsError(
                f"[{CONDITIONS_CLIENT_VERSION}] Weather HTTP {resp.status_code}: {resp.text[:200]}"
            )

        return resp.json()

    def _parse_open_meteo_current(self, data: Dict[str, Any]) -> Dict[str, Any]:
        cur = data.get("current")
        if not isinstance(cur, dict):
            raise ConditionsError(
                f"[{CONDITIONS_CLIENT_VERSION}] Current block missing."
            )

        temp_f = float(cur["temperature_2m"])
        wind_kmh = float(cur["wind_speed_10m"])
        code = int(cur["weather_code"])

        return self._build_weather(temp_f, wind_kmh, code)

    def _build_weather(self, temp_f: float, wind_kmh: float, code: int) -> Dict[str, Any]:
        temp_c = (temp_f - 32.0) * 5.0 / 9.0
        desc = self._weather_code_description(code)

        return {
            "temp_f": float(temp_f),
            "temp_c": float(temp_c),
            "wind_kmh": float(wind_kmh),
            "weather_code": int(code),
            "description": desc,
        }

    def _cache_get(self, key: str) -> Optional[Dict[str, Any]]:
        try:
            return self.cache.get(key)
        except Exception:
            return None

    def _cache_set(self, key: str, value: Dict[str, Any]) -> None:
        try:
            self.cache.set(key, value, ttl_seconds=self.POINT_TTL_SECONDS)
        except Exception:
            pass

    @staticmethod
    def _looks_valid_cached_weather(x: Dict[str, Any]) -> bool:
        return all(k in x for k in ("temp_f", "temp_c", "wind_kmh", "weather_code", "description"))

    @staticmethod
    def _weather_code_description(code: int) -> str:
        if code == 0:
            return "Clear sky"
        if code in (1, 2, 3):
            return "Partly cloudy"
        if code in (45, 48):
            return "Fog"
        if code in (51, 53, 55, 56, 57):
            return "Drizzle"
        if code in (61, 63, 65, 66, 67):
            return "Rain"
        if code in (71, 73, 75, 77, 85, 86):
            return "Snow"
        if code in (80, 81, 82):
            return "Rain showers"
        if code in (95, 96, 99):
            return "Thunderstorm"
        return "Overcast"