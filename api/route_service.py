# api/route_service.py
"""
RoutePlanner service layer.
"""

from __future__ import annotations

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
# CONDITIONS (stub)
# ----------------------------
def _compute_conditions(req: RouteRequest) -> ConditionsSummary:
    return ConditionsSummary(
        weather_summary="Weather not integrated yet.",
        traffic_summary="Traffic not integrated yet.",
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
    return 10.0, "LOW", []


# ----------------------------
# HIGHWAY EXTRACTION
# ----------------------------
HIGHWAY_PATTERN = re.compile(r"\bI[- ]?\d+\b", re.IGNORECASE)

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
def _build_driver_notes(highways: List[str], states: List[str]) -> List[str]:
    notes: List[str] = []

    if any(h in {"I-90", "I-94"} for h in highways):
        notes.append("northern corridor freight route")

    if "MT" in states:
        notes.append("mountain terrain early")

    if any(s in {"ND", "SD", "WY", "MT", "NE"} for s in states):
        notes.append("high crosswind exposure across plains")

    if "IL" in states:
        notes.append("heavy traffic congestion approaching Chicago")

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
) -> str:
    hours = int(eta_hours)
    minutes = int((eta_hours - hours) * 60)

    highways_str = ", ".join(highways) if highways else "regional highways"
    states_str = " → ".join(states) if states else "multi-state route"
    notes_str = "\n".join(f"- {note}" for note in notes)

    return (
        f"{int(distance_miles)} miles (~{hours}h {minutes}m)\n\n"
        f"Primary Highways:\n- {highways_str}\n\n"
        f"States:\n- {states_str}\n\n"
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

    conditions = _compute_conditions(req)
    risk_score, risk_band, risk_components = _compute_risk(
        req=req,
        distance_miles=distance_miles,
        conditions=conditions,
    )

    highways = _extract_highways(segments)
    states = _infer_states_from_highways(highways)
    notes = _build_driver_notes(highways, states)

    explanation = _build_route_explanation(
        distance_miles=distance_miles,
        eta_hours=eta_hours,
        highways=highways,
        states=states,
        notes=notes,
        risk_band=risk_band,
    )

    meta = {
        "origin": req.origin,
        "destination": req.destination,
        "geometry": geometry,
        "highways": highways,
        "states": states,
        "driver_notes": notes,
        "explanation": explanation,
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