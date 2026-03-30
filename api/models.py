# api/models.py
"""
Core data models for the RoutePlanner API.

These are pure-Python dataclasses with no external dependencies.
They are the "contract" between:

- The internal routing logic (route_service.py)
- The external API layer (api_main.py)
- Any future clients (mobile app, desktop UI, etc.)

Do NOT put any network or filesystem logic in this file.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional, Literal, Dict, Any


# -------------------------
# Basic route components
# -------------------------


@dataclass(frozen=True)
class Stop:
    """
    Represents a stop on the route.

    For now, keep it simple: a string that your existing routing client
    can interpret (ZIP, city, full address, etc.).
    """

    location: str
    label: Optional[str] = None  # e.g. "Pickup", "Drop 1", etc.


@dataclass(frozen=True)
class RouteRequest:
    """
    Normalized request from any client (desktop, mobile, API).

    This is what the core route service (plan_route) consumes.
    """

    origin: str
    destination: str
    stops: List[Stop] = field(default_factory=list)

    # "driver" – optimized for in-cab use (ETA, fuel, concise summary)
    # "dispatcher" – potentially more detail, alt routes, richer risk breakdown
    mode: Literal["driver", "dispatcher"] = "driver"

    # For drivers you usually assume 70 mph; dispatcher can override if needed.
    avg_speed_mph: float = 70.0

    # Optional vehicle profile (HGV, weight class, etc.).
    vehicle_profile: Optional[str] = None

    # Optional free-form notes; not used by routing logic but can be logged.
    notes: Optional[str] = None


# -------------------------
# Risk & conditions
# -------------------------


@dataclass(frozen=True)
class RiskComponent:
    """
    A single element of the risk score, e.g. "Weather", "Traffic", "Distance".
    """

    name: str
    score: float  # 0–100 or any normalized scale you use internally
    explanation: str


@dataclass(frozen=True)
class ConditionsSummary:
    """
    High-level conditions along the route (for driver display).
    """

    weather_summary: str  # e.g. "Light snow near Billings, clear elsewhere"
    traffic_summary: str  # e.g. "Minor slowdowns near Chicago metro"
    alerts: List[str] = field(default_factory=list)  # e.g. ["High winds near Fargo"]


# -------------------------
# Route response
# -------------------------


@dataclass(frozen=True)
class RouteResponse:
    """
    Canonical response from the core route planner.

    The API layer will turn this into JSON; mobile apps / desktop UIs
    should not need to know about your internal modules.
    """

    # Core outcome
    distance_miles: float
    eta_hours: float

    # Aggregate risk
    risk_score: float  # e.g. 0–100
    risk_band: Literal["LOW", "MEDIUM", "HIGH"]

    # Human-readable conditions + guidance
    conditions: ConditionsSummary
    recommended_action: str  # e.g. "Good to go", "Delay 2–3 hours", etc.

    # Optional detailed breakdown (for dispatcher / debugging / future JADE audit)
    risk_components: List[RiskComponent] = field(default_factory=list)

    # Place to tuck extra metadata without breaking clients
    meta: Dict[str, Any] = field(default_factory=dict)