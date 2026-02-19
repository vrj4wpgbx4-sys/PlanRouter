from __future__ import annotations

from typing import Dict, Any, Optional
import time
import requests

# Keep legacy absolute import compatibility
try:
    from route_planner_ai.cache_db import CacheDB
except Exception:  # pragma: no cover
    from cache_db import CacheDB  # type: ignore


# Bump this any time we rewrite so the report proves which file is being used
CONDITIONS_CLIENT_VERSION = "2026-02-17.v2"


class ConditionsError(Exception):
    """Weather/conditions client error with user-friendly message."""
    pass


class ConditionsClient:
    """
    Open-Meteo weather snapshot client with caching.

    Goals:
      - Never hide the true reason for failure
      - Work with both Open-Meteo 'current=' and legacy 'current_weather=true'
      - Cache results to reduce calls
    """

    BASE_URL = "https://api.open-meteo.com/v1/forecast"

    POINT_TTL_SECONDS = 10 * 60          # 10 minutes
    PURGE_EVERY_SECONDS = 6 * 60 * 60    # purge best-effort every 6 hours
    TIMEOUT_SECONDS = 12

    def __init__(self) -> None:
        self.session = requests.Session()
        self.cache = CacheDB()
        self._last_purge_ts = 0

    def get_route_weather(
        self,
        origin_lat: float,
        origin_lon: float,
        dest_lat: float,
        dest_lon: float,
    ) -> str:
        """
        Return a short weather snapshot at origin / midpoint / destination.

        IMPORTANT:
        If anything fails, raise ConditionsError with an explicit reason
        (never generic "unexpected error").
        """
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

    # ---------------------------------------------------------------------
    # Internals
    # ---------------------------------------------------------------------

    def _maybe_purge_expired(self) -> None:
        now = int(time.time())
        if now - self._last_purge_ts >= self.PURGE_EVERY_SECONDS:
            try:
                self.cache.purge_expired()
            except Exception:
                pass
            self._last_purge_ts = now

    def _fetch_point_weather(self, lat: float, lon: float) -> Dict[str, Any]:
        cache_key = f"wx_{lat:.4f}_{lon:.4f}"

        cached = self._cache_get(cache_key)
        if isinstance(cached, dict) and self._looks_valid_cached_weather(cached):
            return cached

        # Try modern API first, then legacy fallback
        data = self._call_open_meteo(lat, lon)
        parsed = self._parse_open_meteo(data)

        self._cache_set(cache_key, parsed)
        return parsed

    def _call_open_meteo(self, lat: float, lon: float) -> Dict[str, Any]:
        # Attempt 1: modern schema
        params1 = {
            "latitude": lat,
            "longitude": lon,
            "current": "temperature_2m,weather_code,wind_speed_10m",
            "temperature_unit": "fahrenheit",
            "wind_speed_unit": "kmh",
        }

        try:
            return self._http_get_json(self.BASE_URL, params1)
        except ConditionsError as e:
            # If the error smells like schema incompatibility, try fallback; otherwise fail fast.
            msg = str(e).lower()
            if "schema" not in msg and "missing" not in msg and "current" not in msg:
                raise

        # Attempt 2: legacy schema
        params2 = {
            "latitude": lat,
            "longitude": lon,
            "current_weather": "true",
            "temperature_unit": "fahrenheit",
            "windspeed_unit": "kmh",
        }
        return self._http_get_json(self.BASE_URL, params2)

    def _http_get_json(self, url: str, params: Dict[str, Any]) -> Dict[str, Any]:
        try:
            resp = self.session.get(url, params=params, timeout=self.TIMEOUT_SECONDS)
        except requests.Timeout:
            raise ConditionsError(f"[{CONDITIONS_CLIENT_VERSION}] Weather timed out after {self.TIMEOUT_SECONDS}s.")
        except requests.RequestException as e:
            raise ConditionsError(f"[{CONDITIONS_CLIENT_VERSION}] Weather network error: {e!r}")

        if resp.status_code != 200:
            body = (resp.text or "")[:250].replace("\n", " ")
            raise ConditionsError(f"[{CONDITIONS_CLIENT_VERSION}] Weather HTTP {resp.status_code}: {body}")

        try:
            data = resp.json()
        except ValueError:
            raise ConditionsError(f"[{CONDITIONS_CLIENT_VERSION}] Weather returned invalid JSON.")

        if not isinstance(data, dict):
            raise ConditionsError(f"[{CONDITIONS_CLIENT_VERSION}] Weather response schema invalid (not a dict).")

        return data

    def _parse_open_meteo(self, data: Dict[str, Any]) -> Dict[str, Any]:
        # Modern: data["current"]
        if isinstance(data.get("current"), dict):
            cur = data["current"]
            try:
                temp_f = float(cur["temperature_2m"])
                wind_kmh = float(cur["wind_speed_10m"])
                code = int(cur["weather_code"])
            except Exception as e:
                raise ConditionsError(f"[{CONDITIONS_CLIENT_VERSION}] Weather schema mismatch (current): {e!r}")

            return self._build_weather(temp_f, wind_kmh, code)

        # Legacy: data["current_weather"]
        if isinstance(data.get("current_weather"), dict):
            cur = data["current_weather"]
            try:
                temp_f = float(cur["temperature"])
                wind_kmh = float(cur["windspeed"])
                code = int(cur["weathercode"])
            except Exception as e:
                raise ConditionsError(f"[{CONDITIONS_CLIENT_VERSION}] Weather schema mismatch (current_weather): {e!r}")

            return self._build_weather(temp_f, wind_kmh, code)

        raise ConditionsError(f"[{CONDITIONS_CLIENT_VERSION}] Weather response missing current conditions block (schema).")

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
