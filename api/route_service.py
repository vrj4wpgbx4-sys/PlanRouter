# api/route_service.py
"""
RoutePlanner service layer.
"""

from __future__ import annotations

from dataclasses import asdict
import re
from typing import Tuple, List, Dict, Any

from routing_client import RoutingClient, RoutingError

from .models import (
    RouteRequest,
    RouteResponse,
    ConditionsSummary,
    RiskComponent,
)


# ----------------------------
# ROUTE METRICS
# ----------------------------
def _compute_route_metrics(req: RouteRequest) -> Tuple[float, float, Dict[str, Any]]:
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

    distance_miles = float(summary.get("distance_miles") or 0.0)
    if distance_miles <= 0:
        raise RoutingError("Routing returned zero distance for primary route.")

    avg_speed = req.avg_speed_mph if req.avg_speed_mph > 0 else 1.0
    eta_hours = distance_miles / avg_speed

    return distance_miles, eta_hours, geometry


# ----------------------------
# CONDITIONS (stub)
# ----------------------------
def _compute_conditions(req: RouteRequest) -> ConditionsSummary:
    return ConditionsSummary(
        weather_summary="Weather data not yet wired into API layer.",
        traffic_summary="Traffic data not yet wired into API layer.",
        alerts=[],
    )


# ----------------------------
# RISK (baseline)
# ----------------------------
def _compute_risk(
    req: RouteRequest,
    distance_miles: float,
    conditions: ConditionsSummary,
) -> Tuple[float, str, List[RiskComponent]]:
    risk_score = 10.0
    risk_band = "LOW"

    if risk_score < 33:
        risk_band = "LOW"
    elif risk_score < 67:
        risk_band = "MEDIUM"
    else:
        risk_band = "HIGH"

    return risk_score, risk_band, []


# ----------------------------
# LOCATION / STATE HELPERS
# ----------------------------
_STATE_ALIASES: Dict[str, str] = {
    "alabama": "AL",
    "al": "AL",
    "alaska": "AK",
    "ak": "AK",
    "arizona": "AZ",
    "az": "AZ",
    "arkansas": "AR",
    "ar": "AR",
    "california": "CA",
    "ca": "CA",
    "colorado": "CO",
    "co": "CO",
    "connecticut": "CT",
    "ct": "CT",
    "delaware": "DE",
    "de": "DE",
    "florida": "FL",
    "fl": "FL",
    "georgia": "GA",
    "ga": "GA",
    "idaho": "ID",
    "id": "ID",
    "illinois": "IL",
    "il": "IL",
    "indiana": "IN",
    "in": "IN",
    "iowa": "IA",
    "ia": "IA",
    "kansas": "KS",
    "ks": "KS",
    "kentucky": "KY",
    "ky": "KY",
    "louisiana": "LA",
    "la": "LA",
    "maine": "ME",
    "me": "ME",
    "maryland": "MD",
    "md": "MD",
    "massachusetts": "MA",
    "ma": "MA",
    "michigan": "MI",
    "mi": "MI",
    "minnesota": "MN",
    "mn": "MN",
    "mississippi": "MS",
    "ms": "MS",
    "missouri": "MO",
    "mo": "MO",
    "montana": "MT",
    "mt": "MT",
    "nebraska": "NE",
    "ne": "NE",
    "nevada": "NV",
    "nv": "NV",
    "new hampshire": "NH",
    "nh": "NH",
    "new jersey": "NJ",
    "nj": "NJ",
    "new mexico": "NM",
    "nm": "NM",
    "new york": "NY",
    "ny": "NY",
    "north carolina": "NC",
    "nc": "NC",
    "north dakota": "ND",
    "nd": "ND",
    "ohio": "OH",
    "oh": "OH",
    "oklahoma": "OK",
    "ok": "OK",
    "oregon": "OR",
    "or": "OR",
    "pennsylvania": "PA",
    "pa": "PA",
    "rhode island": "RI",
    "ri": "RI",
    "south carolina": "SC",
    "sc": "SC",
    "south dakota": "SD",
    "sd": "SD",
    "tennessee": "TN",
    "tn": "TN",
    "texas": "TX",
    "tx": "TX",
    "utah": "UT",
    "ut": "UT",
    "vermont": "VT",
    "vt": "VT",
    "virginia": "VA",
    "va": "VA",
    "washington": "WA",
    "wa": "WA",
    "west virginia": "WV",
    "wv": "WV",
    "wisconsin": "WI",
    "wi": "WI",
    "wyoming": "WY",
    "wy": "WY",
}

_NORTHERN_STATE_ORDER = ["WA", "ID", "MT", "ND", "MN", "WI", "IL"]
_CENTRAL_STATE_ORDER = ["WA", "OR", "ID", "MT", "WY", "SD", "MN", "WI", "IL"]
_SOUTHERN_STATE_ORDER = ["CA", "AZ", "NM", "TX", "OK", "AR", "TN", "NC"]


def _extract_state_from_location(location_text: str) -> str | None:
    text = location_text.strip().lower()

    # Prefer the last comma-separated segment, e.g. "Chicago, IL"
    parts = [p.strip() for p in text.split(",") if p.strip()]
    candidates = []

    if parts:
        candidates.append(parts[-1])

    candidates.append(text)

    for candidate in candidates:
        candidate = re.sub(r"\s+", " ", candidate).strip()

        if candidate in _STATE_ALIASES:
            return _STATE_ALIASES[candidate]

        # Match exact two-letter token only, not substrings inside words like Chicago
        match = re.search(r"\b([a-z]{2})\b$", candidate)
        if match:
            token = match.group(1)
            if token in _STATE_ALIASES:
                return _STATE_ALIASES[token]

    return None


def _infer_state_path(origin_state: str | None, destination_state: str | None) -> List[str]:
    if not origin_state or not destination_state:
        return [s for s in [origin_state, destination_state] if s]

    if origin_state == destination_state:
        return [origin_state]

    northern_positions = {s: i for i, s in enumerate(_NORTHERN_STATE_ORDER)}
    if origin_state in northern_positions and destination_state in northern_positions:
        start = northern_positions[origin_state]
        end = northern_positions[destination_state]
        lo, hi = sorted((start, end))
        path = _NORTHERN_STATE_ORDER[lo : hi + 1]
        return path if start <= end else list(reversed(path))

    central_positions = {s: i for i, s in enumerate(_CENTRAL_STATE_ORDER)}
    if origin_state in central_positions and destination_state in central_positions:
        start = central_positions[origin_state]
        end = central_positions[destination_state]
        lo, hi = sorted((start, end))
        path = _CENTRAL_STATE_ORDER[lo : hi + 1]
        return path if start <= end else list(reversed(path))

    southern_positions = {s: i for i, s in enumerate(_SOUTHERN_STATE_ORDER)}
    if origin_state in southern_positions and destination_state in southern_positions:
        start = southern_positions[origin_state]
        end = southern_positions[destination_state]
        lo, hi = sorted((start, end))
        path = _SOUTHERN_STATE_ORDER[lo : hi + 1]
        return path if start <= end else list(reversed(path))

    return [origin_state, destination_state]


# ----------------------------
# CORRIDOR DETECTION
# ----------------------------
def _detect_corridor(states: List[str]) -> str:
    state_set = set(states)

    if {"MT", "ND", "MN", "WI", "IL"}.issubset(state_set):
        return "Northern freight corridor (I-90 / I-94)"
    if {"CA", "AZ", "NM", "TX"}.issubset(state_set):
        return "Southern corridor (I-10 / I-40 patterns)"
    if {"MT", "SD", "MN", "WI", "IL"}.issubset(state_set):
        return "Northern-central corridor (I-90 patterns)"
    return "Mixed regional highway network"


# ----------------------------
# EXPLANATION ENGINE
# ----------------------------
def _build_route_explanation(
    req: RouteRequest,
    distance_miles: float,
    eta_hours: float,
    risk_band: str,
) -> str:
    hours = int(eta_hours)
    minutes = int((eta_hours - hours) * 60)
    time_str = f"{hours}h {minutes}m"

    origin_state = _extract_state_from_location(req.origin)
    destination_state = _extract_state_from_location(req.destination)
    states = _infer_state_path(origin_state, destination_state)
    corridor = _detect_corridor(states)

    terrain_start = (
        "mountain terrain early"
        if origin_state in {"MT", "ID", "WY", "CO", "UT"}
        else "mixed terrain early"
    )

    terrain_end = (
        "flatter midwest terrain approaching destination"
        if destination_state in {"IL", "IN", "OH", "WI", "MN", "IA"}
        else "mixed terrain late"
    )

    wind_note = (
        "high crosswind exposure across plains"
        if any(state in {"ND", "SD", "WY", "MT", "NE"} for state in states)
        else "normal wind exposure"
    )

    urban_note = ""
    if destination_state == "IL":
        urban_note = "heavy traffic congestion approaching Chicago"

    risk_text = {
        "LOW": "low operational risk",
        "MEDIUM": "moderate operational risk",
        "HIGH": "elevated operational risk",
    }[risk_band]

    states_str = " → ".join(states) if states else "multi-state route"

    lines = [
        f"{int(distance_miles)} miles (~{time_str})",
        "",
        "Corridor:",
        f"- {corridor}",
        "",
        "States:",
        f"- {states_str}",
        "",
        "Driver Notes:",
        f"- {terrain_start}",
        f"- {terrain_end}",
        f"- {wind_note}",
    ]

    if urban_note:
        lines.append(f"- {urban_note}")

    lines.extend(["", f"Overall: {risk_text}"])

    return "\n".join(lines)


# ----------------------------
# ACTION
# ----------------------------
def _derive_recommended_action(
    mode: str,
    risk_band: str,
    conditions: ConditionsSummary,
) -> str:
    if mode == "dispatcher":
        if risk_band == "LOW":
            return "Route acceptable."
        if risk_band == "MEDIUM":
            return "Monitor conditions and consider adjustments."
        return "Re-evaluate route before dispatch."

    if risk_band == "LOW":
        return "Good to go."
    if risk_band == "MEDIUM":
        return "Stay alert."
    return "Coordinate with dispatch."


# ----------------------------
# MAIN ENTRY
# ----------------------------
def plan_route(req: RouteRequest) -> RouteResponse:
    distance_miles, eta_hours, geometry = _compute_route_metrics(req)
    conditions = _compute_conditions(req)

    risk_score, risk_band, risk_components = _compute_risk(
        req=req,
        distance_miles=distance_miles,
        conditions=conditions,
    )

    explanation = _build_route_explanation(
        req=req,
        distance_miles=distance_miles,
        eta_hours=eta_hours,
        risk_band=risk_band,
    )

    meta = {
        "origin": req.origin,
        "destination": req.destination,
        "stops": [asdict(s) for s in req.stops],
        "mode": req.mode,
        "avg_speed_mph": req.avg_speed_mph,
        "vehicle_profile": req.vehicle_profile,
        "geometry": geometry,
        "explanation": explanation,
        "origin_state": origin_state if False else _extract_state_from_location(req.origin),
        "destination_state": destination_state if False else _extract_state_from_location(req.destination),
    }

    return RouteResponse(
        distance_miles=distance_miles,
        eta_hours=eta_hours,
        risk_score=risk_score,
        risk_band=risk_band,
        conditions=conditions,
        recommended_action=_derive_recommended_action(
            req.mode, risk_band, conditions
        ),
        risk_components=risk_components,
        meta=meta,
    )