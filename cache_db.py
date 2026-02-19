import os
import sqlite3
import json
import time
from typing import Any, Dict, Optional, Tuple


DEFAULT_DB_PATH = os.path.join(os.path.dirname(__file__), "route_planner_cache.sqlite3")


def _now_ts() -> int:
    return int(time.time())


class CacheDB:
    """
    Simple SQLite cache with TTL (time-to-live).

    Stores JSON blobs keyed by:
      - namespace (e.g., "geocode", "route", "traffic", "weather", "tolls")
      - cache_key (string)
    """

    def __init__(self, db_path: str = DEFAULT_DB_PATH) -> None:
        self.db_path = db_path
        self._ensure_schema()

    def _connect(self) -> sqlite3.Connection:
        con = sqlite3.connect(self.db_path)
        con.execute("PRAGMA journal_mode=WAL;")
        con.execute("PRAGMA synchronous=NORMAL;")
        return con

    def _ensure_schema(self) -> None:
        with self._connect() as con:
            con.execute(
                """
                CREATE TABLE IF NOT EXISTS cache_entries (
                    namespace TEXT NOT NULL,
                    cache_key TEXT NOT NULL,
                    value_json TEXT NOT NULL,
                    created_at INTEGER NOT NULL,
                    expires_at INTEGER NOT NULL,
                    PRIMARY KEY (namespace, cache_key)
                );
                """
            )
            con.execute("CREATE INDEX IF NOT EXISTS idx_cache_expires ON cache_entries(expires_at);")

            # Optional: saved/common lanes, office presets, etc.
            con.execute(
                """
                CREATE TABLE IF NOT EXISTS saved_lanes (
                    lane_id TEXT PRIMARY KEY,
                    origin_text TEXT NOT NULL,
                    destination_text TEXT NOT NULL,
                    stops_json TEXT NOT NULL,
                    vehicle_profile TEXT NOT NULL,
                    created_at INTEGER NOT NULL
                );
                """
            )

    # --------------------------
    # Cache API
    # --------------------------

    def get(self, namespace: str, cache_key: str) -> Optional[Dict[str, Any]]:
        now = _now_ts()
        with self._connect() as con:
            row = con.execute(
                """
                SELECT value_json, expires_at
                FROM cache_entries
                WHERE namespace=? AND cache_key=?
                """,
                (namespace, cache_key),
            ).fetchone()

        if not row:
            return None

        value_json, expires_at = row
        if expires_at < now:
            # expired -> treat as missing
            return None

        try:
            return json.loads(value_json)
        except Exception:
            return None

    def set(self, namespace: str, cache_key: str, value: Dict[str, Any], ttl_seconds: int) -> None:
        now = _now_ts()
        expires_at = now + int(ttl_seconds)
        payload = json.dumps(value, ensure_ascii=False)

        with self._connect() as con:
            con.execute(
                """
                INSERT INTO cache_entries(namespace, cache_key, value_json, created_at, expires_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(namespace, cache_key)
                DO UPDATE SET
                    value_json=excluded.value_json,
                    created_at=excluded.created_at,
                    expires_at=excluded.expires_at
                """,
                (namespace, cache_key, payload, now, expires_at),
            )

    def purge_expired(self) -> int:
        now = _now_ts()
        with self._connect() as con:
            cur = con.execute("DELETE FROM cache_entries WHERE expires_at < ?", (now,))
            return cur.rowcount

    # --------------------------
    # Saved lanes (optional)
    # --------------------------

    def save_lane(
        self,
        lane_id: str,
        origin_text: str,
        destination_text: str,
        stops: list,
        vehicle_profile: str,
    ) -> None:
        now = _now_ts()
        with self._connect() as con:
            con.execute(
                """
                INSERT INTO saved_lanes(lane_id, origin_text, destination_text, stops_json, vehicle_profile, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(lane_id)
                DO UPDATE SET
                    origin_text=excluded.origin_text,
                    destination_text=excluded.destination_text,
                    stops_json=excluded.stops_json,
                    vehicle_profile=excluded.vehicle_profile
                """,
                (
                    lane_id,
                    origin_text,
                    destination_text,
                    json.dumps(stops, ensure_ascii=False),
                    vehicle_profile,
                    now,
                ),
            )

    def get_lane(self, lane_id: str) -> Optional[Dict[str, Any]]:
        with self._connect() as con:
            row = con.execute(
                """
                SELECT origin_text, destination_text, stops_json, vehicle_profile
                FROM saved_lanes
                WHERE lane_id=?
                """,
                (lane_id,),
            ).fetchone()

        if not row:
            return None

        origin_text, destination_text, stops_json, vehicle_profile = row
        try:
            stops = json.loads(stops_json)
        except Exception:
            stops = []

        return {
            "lane_id": lane_id,
            "origin_text": origin_text,
            "destination_text": destination_text,
            "stops": stops,
            "vehicle_profile": vehicle_profile,
        }
