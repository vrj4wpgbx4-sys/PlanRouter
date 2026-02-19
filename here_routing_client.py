import os
import re
from dataclasses import dataclass
from typing import Any, Dict, List, Tuple, Optional

import requests


class HereRoutingError(Exception):
    pass


@dataclass
class HereRoute:
    id: str
    distance_miles: float
    duration_minutes: float
    geometry_polyline: str
    tolls: Dict[str, Any]  # best-effort toll summary/details


def _meters_to_miles(m: float) -> float:
    return m / 1609.344


def _seconds_to_minutes(s: float) -> float:
    return s / 60.0


def _clean_text(t: str) -> str:
    t = (t or "").strip()
    t = re.sub(r"\s+", " ", t)
    return t


def _is_zip(text: str) -> bool:
    t = _clean_text(text)
    return bool(re.fullmatch(r"\d{5}(-\d{4})?", t))


class HereRoutingClient:
    """
    HERE Routing v8 + HERE Geocoding.

    Environment:
      HERE_API_KEY=...

    Notes:
      - We request truck routing.
      - We request tolls in the return fields when available.
    """

    ROUTE_URL = "https://router.hereapi.com/v8/routes"
    GEOCODE_URL = "https://geocode.search.hereapi.com/v1/geocode"

    def __init__(self, api_key: Optional[str] = None) -> None:
        self.api_key = api_key or os.environ.get("HERE_API_KEY")
        if not self.api_key:
            raise HereRoutingError("HERE API key not configured. Set HERE_API_KEY in your environment.")
        self.session = requests.Session()

    def geocode(self, text: str) -> Tuple[float, float]:
        """
        Returns (lat, lon)
        """
        text = _clean_text(text)
        if not text:
            raise HereRoutingError("Empty location provided.")

        q = f"{text}, USA" if _is_zip(text) else text

        try:
            resp = self.session.get(
                self.GEOCODE_URL,
                params={"q": q, "apiKey": self.api_key, "limit": 1},
                timeout=10,
            )
        except requests.RequestException:
            raise HereRoutingError("Network error while contacting HERE geocoding.")

        if resp.status_code != 200:
            raise HereRoutingError(f"HERE geocoding error (status {resp.status_code}).")

        data = resp.json()
        items = data.get("items", [])
        if not items:
            raise HereRoutingError(f"No geocoding result for '{text}'.")

        pos = items[0].get("position") or {}
        lat = pos.get("lat")
        lon = pos.get("lng")
        if lat is None or lon is None:
            raise HereRoutingError("HERE geocoding returned invalid coordinates.")
        return float(lat), float(lon)

    def route_truck(
        self,
        origin_text: str,
        destination_text: str,
        stops: Optional[List[str]] = None,
        alternatives: int = 1,
    ) -> Dict[str, Any]:
        """
        Returns:
          {
            "routes": [ {id, summary:{distance_miles,duration_minutes}, geometry:<encoded polyline>}... ],
            "toll": {available, toll_likely, note, details?}
          }
        """
        stops = stops or []

        origin_lat, origin_lon = self.geocode(origin_text)
        dest_lat, dest_lon = self.geocode(destination_text)

        # HERE uses "lat,lon" strings
        origin = f"{origin_lat},{origin_lon}"
        destination = f"{dest_lat},{dest_lon}"

        via = []
        for s in stops:
            s = _clean_text(s)
            if not s:
                continue
            lat, lon = self.geocode(s)
            via.append(f"{lat},{lon}")

        # Request fields
        # - summary: distance/duration
        # - polyline: encoded polyline
        # - tolls: return toll details when supported
        # NOTE: If tolls are not available for a region, HERE may omit them.
        return_fields = "summary,polyline,tolls"

        params = {
            "transportMode": "truck",
            "origin": origin,
            "destination": destination,
            "return": return_fields,
            "apiKey": self.api_key,
        }

        # multi-stop: repeated via parameters
        # requests supports list values
        if via:
            params["via"] = via

        # alternatives: HERE uses "alternatives"
        # We keep this simple; if you request 3 and only 1 exists, you just get 1.
        if alternatives and alternatives > 1 and not via:
            params["alternatives"] = str(int(alternatives))

        try:
            resp = self.session.get(self.ROUTE_URL, params=params, timeout=20)
        except requests.RequestException:
            raise HereRoutingError("Network error while contacting HERE routing service.")

        if resp.status_code != 200:
            # Keep the message short but useful
            raise HereRoutingError(f"HERE routing error (status {resp.status_code}).")

        data = resp.json()
        routes = data.get("routes", [])
        if not routes:
            raise HereRoutingError("HERE returned no routes for this request.")

        parsed_routes: List[Dict[str, Any]] = []
        any_tolls = False
        toll_details: List[Dict[str, Any]] = []

        for idx, r in enumerate(routes):
            sections = r.get("sections", [])
            if not sections:
                continue

            # For now, we treat the entire response as 1 route with 1+ sections.
            # We accumulate distance/duration across sections and keep a single polyline.
            total_m = 0.0
            total_s = 0.0

            # For mapping, we keep the first section polyline (most common).
            # Later, we can stitch polylines across sections if needed.
            polyline = sections[0].get("polyline")

            for sec in sections:
                summ = sec.get("summary") or {}
                total_m += float(summ.get("length", 0.0) or 0.0)
                total_s += float(summ.get("duration", 0.0) or 0.0)

                t = sec.get("tolls")
                if t:
                    any_tolls = True
                    toll_details.append(t)

            if not isinstance(polyline, str) or not polyline:
                # If missing, do not crash the app
                polyline = ""

            parsed_routes.append(
                {
                    "id": f"route-{idx+1}",
                    "summary": {
                        "distance_miles": _meters_to_miles(total_m),
                        "duration_minutes": _seconds_to_minutes(total_s),
                    },
                    "geometry": polyline,
                }
            )

        if not parsed_routes:
            raise HereRoutingError("HERE returned routes, but none could be parsed.")

        toll_block = {
            "available": True,
            "toll_likely": bool(any_tolls),
            "note": "Tolls detected on this route." if any_tolls else "No tolls reported on this route.",
        }

        # Include raw toll details for future enhancements (cost breakdown, plazas, etc.)
        if toll_details:
            toll_block["details"] = toll_details

        return {"routes": parsed_routes, "toll": toll_block}
