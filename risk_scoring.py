from __future__ import annotations

from typing import Tuple, List, Optional, Dict, Any
import re


def _normalize_text(text: str) -> str:
    return (text or "").lower()


def _extract_count(label: str, text: str) -> int:
    """
    Extracts aggregated counts like:
        'Road closed: 12'
        'Accident: 4'
        'Road works: 14'
    Falls back to substring count if no numeric match is found.
    """
    pattern = rf"{re.escape(label)}:\s*(\d+)"
    match = re.search(pattern, text, flags=re.IGNORECASE)
    if match:
        try:
            return int(match.group(1))
        except ValueError:
            pass

    return text.lower().count(label.lower())


def _extract_wind_kmh(weather_text: str) -> Optional[float]:
    """
    Attempts to parse wind speed from your standardized weather line format:
        'wind 8 km/h'
    Returns km/h if found.
    """
    m = re.search(r"wind\s+(\d+(?:\.\d+)?)\s*km\/h", weather_text, flags=re.IGNORECASE)
    if not m:
        return None
    try:
        return float(m.group(1))
    except ValueError:
        return None


def _extract_temps_f(weather_text: str) -> List[float]:
    """
    Extract all Fahrenheit temps like:
        '35.6°F'
    Returns list of floats (may be empty).
    """
    temps: List[float] = []
    for m in re.finditer(r"(-?\d+(?:\.\d+)?)\s*°f", weather_text, flags=re.IGNORECASE):
        try:
            temps.append(float(m.group(1)))
        except ValueError:
            continue
    return temps


def _has_precip_signal(weather_text: str) -> bool:
    """
    Heuristic: if the description includes precip-like terms.
    Your current mapping uses: Drizzle, Rain, Snow, Rain showers, Thunderstorm.
    """
    wt = weather_text.lower()
    return any(
        k in wt
        for k in (
            "drizzle",
            "rain",
            "snow",
            "showers",
            "thunderstorm",
            "sleet",
            "freezing",
        )
    )


def compute_route_risk(
    distance_miles: float,
    duration_minutes: float,
    weather_summary: str,
    traffic_summary: str,
    traffic_stats: Optional[Dict[str, Any]] = None,
) -> Tuple[int, str, str, List[str]]:
    """
    Returns: (score, label, explanation, actions)

    Backward compatibility note:
      - This function previously returned (score, label, explanation).
      - Your GUI should now unpack the 4th value (actions). If it doesn't,
        you will see a "too many values to unpack" error.
    """

    weather_text = _normalize_text(weather_summary)
    traffic_text = _normalize_text(traffic_summary)

    score = 10  # baseline

    # -------------------------------------------------------
    # Distance / Duration
    # -------------------------------------------------------
    if distance_miles > 1200:
        score += 20
    elif distance_miles > 800:
        score += 15
    elif distance_miles > 400:
        score += 8
    elif distance_miles > 200:
        score += 4

    if duration_minutes > 18 * 60:
        score += 12
    elif duration_minutes > 12 * 60:
        score += 8
    elif duration_minutes > 8 * 60:
        score += 5
    elif duration_minutes > 4 * 60:
        score += 3

    # -------------------------------------------------------
    # Weather
    # -------------------------------------------------------
    weather_factors: List[str] = []

    if "heavy snow" in weather_text:
        score += 25
        weather_factors.append("heavy snow")
    elif "moderate snow" in weather_text:
        score += 15
        weather_factors.append("moderate snow")
    elif "snow" in weather_text:
        score += 8
        weather_factors.append("snow")

    if "thunderstorm" in weather_text:
        score += 20
        weather_factors.append("thunderstorms")

    if "heavy rain" in weather_text:
        score += 15
        weather_factors.append("heavy rain")
    elif "moderate rain" in weather_text:
        score += 8
        weather_factors.append("moderate rain")

    # Your current weather strings always include "wind X km/h"
    # but we only escalate if wind is meaningful.
    wind_kmh = _extract_wind_kmh(weather_summary)
    if wind_kmh is not None:
        # mild contribution at any wind mention (keeps your existing behavior)
        if wind_kmh >= 0:
            score += 3
            weather_factors.append("wind")
    else:
        # fallback to old behavior if parsing fails
        if "wind" in weather_text:
            score += 3
            weather_factors.append("wind")

    # -------------------------------------------------------
    # Traffic (aggregated counts)
    # -------------------------------------------------------
    traffic_factors: List[str] = []

    closures = _extract_count("road closed", traffic_text)
    accidents = _extract_count("accident", traffic_text)
    roadworks = _extract_count("road works", traffic_text) + _extract_count("roadworks", traffic_text)

    if closures > 0:
        score += min(closures * 4, 30)
        traffic_factors.append(f"{closures} closure(s)")

    if accidents > 0:
        score += min(accidents * 8, 24)
        traffic_factors.append(f"{accidents} accident(s)")

    if roadworks > 0:
        score += min(roadworks * 3, 15)
        traffic_factors.append(f"{roadworks} work zone(s)")

    if "none reported" in traffic_text:
        score -= 5

    if "clear sky" in weather_text and not traffic_factors and not weather_factors:
        score -= 5

    # Clamp
    score = max(0, min(100, score))

    # -------------------------------------------------------
    # Label
    # -------------------------------------------------------
    if score <= 30:
        label = "LOW"
    elif score <= 60:
        label = "MODERATE"
    elif score <= 80:
        label = "ELEVATED"
    else:
        label = "HIGH"

    # -------------------------------------------------------
    # Explanation (existing behavior preserved)
    # -------------------------------------------------------
    factors: List[str] = []

    if distance_miles > 400:
        factors.append("long-distance route")

    if weather_factors:
        # de-dupe but keep stable order
        seen = set()
        wf = []
        for f in weather_factors:
            if f not in seen:
                seen.add(f)
                wf.append(f)
        factors.append("weather: " + ", ".join(wf))

    if traffic_factors:
        factors.append("traffic: " + ", ".join(traffic_factors))

    if not factors:
        factors.append("no significant issues detected")

    explanation = "; ".join(factors)

    # -------------------------------------------------------
    # Actionable Guidance (NEW)
    # -------------------------------------------------------
    actions: List[str] = []

    # Buffer guidance by label + traffic
    if label == "LOW":
        actions.append("Add a 10–15 minute buffer for normal variability.")
    elif label == "MODERATE":
        actions.append("Add a 30–45 minute buffer and expect speed reductions in impacted zones.")
    elif label == "ELEVATED":
        actions.append("Add a 60+ minute buffer; consider delaying departure or selecting an alternate corridor if possible.")
    else:  # HIGH
        actions.append("Strongly consider delaying departure; add 90+ minutes buffer and evaluate alternate routing.")

    if roadworks >= 10:
        actions.append(f"Work zones are heavy ({roadworks}). Expect rolling slowdowns and lane shifts; increase following distance.")
    elif roadworks >= 4:
        actions.append(f"Multiple work zones ({roadworks}). Expect intermittent slowdowns and possible merges.")
    elif roadworks > 0:
        actions.append(f"Work zones present ({roadworks}). Remain alert for reduced speeds and sudden stops.")

    if closures > 0:
        actions.append(f"Closures detected ({closures}). Verify detours and check for last-minute reroutes before departure.")
    if accidents > 0:
        actions.append(f"Accidents detected ({accidents}). Expect localized congestion; plan a contingency stop or bypass.")

    # Wind guidance (km/h thresholds are conservative; adjust later if you want)
    if wind_kmh is not None:
        if wind_kmh >= 45:
            actions.append(f"High wind risk (≈{wind_kmh:.0f} km/h). Use crosswind precautions; consider delay if loaded light/high profile.")
        elif wind_kmh >= 30:
            actions.append(f"Moderate winds (≈{wind_kmh:.0f} km/h). Expect steering correction on exposed stretches; reduce speed as needed.")
        elif wind_kmh >= 20:
            actions.append(f"Noticeable winds (≈{wind_kmh:.0f} km/h). Monitor gusts on ridgelines and open plains.")

    # Near-freezing precip advisory
    temps_f = _extract_temps_f(weather_summary)
    if temps_f:
        min_temp = min(temps_f)
        if min_temp <= 36.0 and _has_precip_signal(weather_summary):
            actions.append(f"Near-freezing precipitation risk (min ≈{min_temp:.1f}°F). Watch for slick spots/black ice, especially bridges and shaded grades.")

    # Traffic stats hook (if later you pass structured congestion)
    if traffic_stats and isinstance(traffic_stats, dict):
        # Placeholder: you can standardize fields later without changing GUI
        pass

    # Final de-dupe while preserving order
    seen_a = set()
    actions_out: List[str] = []
    for a in actions:
        if a not in seen_a:
            seen_a.add(a)
            actions_out.append(a)

    return score, label, explanation, actions_out
