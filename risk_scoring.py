from __future__ import annotations

from typing import List, Dict, Any, Tuple
import re


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _normalize(text: str) -> str:
    return (text or "").lower()


def _contains_any(text: str, needles: List[str]) -> bool:
    t = _normalize(text)
    return any(n in t for n in needles)


def _extract_count(label: str, text: str) -> int:
    """
    Try to extract a numeric count for lines like:

        "Road works: 14"
        "Accident: 3"
        "Closures: 2"

    Falls back to 0 if no clear match.
    """
    pattern = rf"{re.escape(label)}\s*:\s*(\d+)"
    m = re.search(pattern, text, flags=re.IGNORECASE)
    if m:
        try:
            return int(m.group(1))
        except ValueError:
            return 0
    return 0


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def compute_route_risk(
    distance_miles: float,
    duration_minutes: float,
    weather_summary: str,
    traffic_summary: str,
    traffic_stats: Dict[str, Any] | None = None,
) -> Tuple[int, str, str, List[str], Dict[str, Any]]:
    """
    ROUTE RISK INTERFACE v1.0

    Returns:
        score       int   0–100 inclusive
        label       str   "LOW" | "MODERATE" | "ELEVATED" | "HIGH"
        explanation str   short natural-language summary of main drivers
        actions     list  concrete dispatcher guidance lines
        stats       dict  structured fields for future use
    """

    # -----------------------------------------------------------------------
    # Baseline from distance/time
    # -----------------------------------------------------------------------
    score = 0
    factors: List[str] = []
    actions: List[str] = []

    miles = float(distance_miles or 0.0)
    minutes = float(duration_minutes or 0.0)
    hours = minutes / 60.0 if minutes > 0 else 0.0

    if miles <= 0 or minutes <= 0:
        # Degenerate; no distance/time → UNKNOWN but safe default
        score = 0
        factors.append("no distance/time information")
    else:
        # Long-haul baseline
        if miles > 1000:
            score += 15
            factors.append("very long-distance route")
        elif miles > 600:
            score += 10
            factors.append("long-distance route")
        elif miles > 300:
            score += 5
            factors.append("moderate trip length")

        # Time-based fatigue baseline
        if hours > 16:
            score += 15
            factors.append("very long drive window")
        elif hours > 11:
            score += 10
            factors.append("extended drive window")
        elif hours > 8:
            score += 5
            factors.append("full-shift drive window")

    # -----------------------------------------------------------------------
    # Weather analysis
    # -----------------------------------------------------------------------
    w = _normalize(weather_summary)

    heavy_snow = _contains_any(w, ["heavy snow", "blizzard"])
    snow = heavy_snow or _contains_any(w, ["snow", "wintry mix", "sleet"])
    ice = _contains_any(w, ["freezing rain", "black ice", "icy", "ice"])
    fog = _contains_any(w, ["fog", "mist"])
    heavy_rain = _contains_any(w, ["heavy rain", "downpour"])
    rain = heavy_rain or _contains_any(w, ["rain", "showers", "drizzle"])
    high_wind = _contains_any(w, ["wind 6", "wind 7", "gust", "gale", "strong wind"])
    extreme_cold = _contains_any(w, ["-10", "-20", "dangerously cold", "arctic"])
    extreme_heat = _contains_any(w, ["triple digit", "excessive heat", "heat advisory"])

    if heavy_snow or ice:
        score += 25
        factors.append("severe winter conditions")
        actions.append("Consider delay or reroute due to snow/ice risk.")
        actions.append("Require chains and strict speed discipline if operating.")
    elif snow:
        score += 15
        factors.append("winter precipitation on route")
        actions.append("Increase following distance; plan lower cruise speeds.")
    if fog:
        score += 10
        factors.append("visibility reduced by fog/mist")
        actions.append("Increase spacing and avoid high-speed maneuvers in fog.")
    if heavy_rain:
        score += 10
        factors.append("heavy rain / downpours")
        actions.append("Watch for hydroplaning; expect reduced traction.")
    elif rain:
        score += 5
        factors.append("wet road conditions")
    if high_wind:
        score += 10
        factors.append("elevated crosswind risk")
        actions.append("Use caution for high-profile trailers; avoid light loads in exposed corridors.")
    if extreme_cold:
        score += 5
        factors.append("extreme cold (equipment/traction risk)")
    if extreme_heat:
        score += 5
        factors.append("extreme heat (equipment/driver fatigue risk)")

    # -----------------------------------------------------------------------
    # Traffic incidents / road works
    # -----------------------------------------------------------------------
    t = traffic_summary or ""
    t_norm = _normalize(t)

    roadworks_count = _extract_count("Road works", t)
    closures_count = _extract_count("Road closed", t) + _extract_count("Closure", t)
    accident_count = _extract_count("Accident", t)

    # If counts not explicitly present, approximate from text
    if roadworks_count == 0 and "road works" in t_norm:
        roadworks_count = 5
    if closures_count == 0 and _contains_any(t_norm, ["road closed", "closure"]):
        closures_count = 1
    if accident_count == 0 and "accident" in t_norm:
        accident_count = 1

    if accident_count > 0:
        score += min(20, 5 * accident_count)
        factors.append(f"{accident_count} accident(s) reported near corridor")
        actions.append("Check live feeds / ELD messages for major incidents before dispatch.")

    if closures_count > 0:
        score += min(20, 5 * closures_count)
        factors.append(f"{closures_count} closure(s) / blocked segments")
        actions.append("Verify detours; confirm route still legally/physically passable.")

    if roadworks_count > 0:
        if roadworks_count > 50:
            score += 15
            factors.append(f"heavy construction: ~{roadworks_count} work zones")
        elif roadworks_count > 10:
            score += 10
            factors.append(f"moderate construction: ~{roadworks_count} work zones")
        else:
            score += 5
            factors.append(f"light construction: ~{roadworks_count} work zones")
        actions.append("Expect intermittent slowdowns and lane shifts in work zones.")

    # -----------------------------------------------------------------------
    # Optional structured traffic stats hook
    # -----------------------------------------------------------------------
    if traffic_stats and isinstance(traffic_stats, dict):
        # Example: bump score a bit if provider reports heavy congestion
        congestion_level = _normalize(str(traffic_stats.get("congestion_level", "")))
        if "severe" in congestion_level:
            score += 10
            factors.append("severe congestion reported by traffic provider")
        elif "moderate" in congestion_level:
            score += 5
            factors.append("moderate congestion reported by traffic provider")

    # -----------------------------------------------------------------------
    # Clamp, label, explanation
    # -----------------------------------------------------------------------
    score = max(0, min(int(round(score)), 100))

    if score >= 80:
        label = "HIGH"
    elif score >= 60:
        label = "ELEVATED"
    elif score >= 40:
        label = "MODERATE"
    else:
        label = "LOW"

    if factors:
        explanation = "; ".join(factors)
    else:
        explanation = "no significant risk factors detected"

    # De-dupe actions while preserving order
    seen: set[str] = set()
    actions_out: List[str] = []
    for a in actions:
        if a not in seen:
            seen.add(a)
            actions_out.append(a)

    # -----------------------------------------------------------------------
    # Stats payload for future dashboards / logging
    # -----------------------------------------------------------------------
    stats: Dict[str, Any] = {
        "distance_miles": round(miles, 1),
        "duration_minutes": round(minutes, 1),
        "hours": round(hours, 2),
        "weather": {
            "heavy_snow": heavy_snow,
            "snow": snow,
            "ice": ice,
            "fog": fog,
            "heavy_rain": heavy_rain,
            "rain": rain,
            "high_wind": high_wind,
            "extreme_cold": extreme_cold,
            "extreme_heat": extreme_heat,
        },
        "traffic": {
            "roadworks_count": roadworks_count,
            "closures_count": closures_count,
            "accident_count": accident_count,
        },
        "raw": {
            "weather_summary": weather_summary,
            "traffic_summary": traffic_summary,
        },
    }

    return score, label, explanation, actions_out, stats