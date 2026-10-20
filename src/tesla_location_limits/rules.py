"""Pure domain logic: geofenced charge-limit rules.

No network here — `now` and all inputs are injected, so every branch is
unit-testable. The public entry point is ``decide()``, which takes a vehicle
reading and the persistent state and returns a Decision describing exactly
one of: set a limit, do nothing, or skip (e.g. car asleep).

Rules:
  * A "place" matches when the car is within its radius (boundary inclusive).
  * An *arrival* = first read where the car is at a ruled place it wasn't
    inside before. The place's limit is applied EXACTLY ONCE per arrival.
  * Trip-mode override: if the car's current limit is HIGHER than the place
    rule, we never lower it — the driver raised it for a trip. Log why.
  * A manually-set LOWER limit IS raised to the place rule (the rule wins;
    it only ever raises toward the battery-friendly ceiling).
  * Departure clears the per-place applied flag so the next arrival fires again.
  * Asleep cars are skipped (no-wake reads only — wakes cost money).
"""
from __future__ import annotations

import json
import math
import os
import time
from dataclasses import dataclass, field
from pathlib import Path

from .config import PlaceRule

EARTH_RADIUS_M = 6_371_000.0


# -- geometry ------------------------------------------------------------

def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in metres between two lat/lon points."""
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = (math.sin(dphi / 2) ** 2
         + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2)
    return 2 * EARTH_RADIUS_M * math.asin(math.sqrt(a))


def match_place(places: list[PlaceRule], lat: float, lon: float) -> PlaceRule | None:
    """Return the innermost (closest centre; ties broken by smaller radius)
    matching place, or None."""
    best: PlaceRule | None = None
    best_dist = math.inf
    for place in places:
        dist = haversine_m(lat, lon, place.lat, place.lon)
        if dist <= place.radius_m:
            better = dist < best_dist - 1e-9
            tie_smaller = (best is not None
                           and abs(dist - best_dist) <= 1e-9
                           and place.radius_m < best.radius_m)
            if best is None or better or tie_smaller:
                best, best_dist = place, dist
    return best


# -- persistent state ----------------------------------------------------

@dataclass
class VehicleState:
    inside_place: str | None = None     # place name the car is currently in
    applied: dict = field(default_factory=dict)  # place name -> True once applied/seen this visit
    last_applied_limit: int | None = None


class LimitState:
    """Small JSON state file: exactly-once enforcement per (car, place)."""

    def __init__(self, path: str):
        self.path = Path(os.path.expanduser(path))
        self.vehicles: dict[str, VehicleState] = {}
        self.meta: dict = {}
        self._load()

    def _load(self) -> None:
        try:
            raw = json.loads(self.path.read_text())
        except (OSError, json.JSONDecodeError):
            raw = {}
        if not isinstance(raw, dict):
            raw = {}
        self.meta = dict(raw.get("meta") or {})
        for vin, entry in (raw.get("vehicles") or {}).items():
            if not isinstance(entry, dict):
                continue
            self.vehicles[str(vin)] = VehicleState(
                inside_place=entry.get("inside_place"),
                applied=dict(entry.get("applied") or {}),
                last_applied_limit=entry.get("last_applied_limit"),
            )

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        data = {"meta": self.meta,
                "vehicles": {
            vin: {"inside_place": vs.inside_place,
                  "applied": vs.applied,
                  "last_applied_limit": vs.last_applied_limit}
            for vin, vs in self.vehicles.items()}}
        self.path.write_text(json.dumps(data, indent=2))
        try:
            os.chmod(self.path, 0o600)
        except OSError:
            pass

    def for_vin(self, vin: str) -> VehicleState:
        return self.vehicles.setdefault(vin, VehicleState())


# -- decision ------------------------------------------------------------

@dataclass
class VehicleReading:
    """One no-wake vehicle read."""
    name: str
    vin: str
    lat: float | None
    lon: float | None
    charge_limit_pct: int | None
    asleep: bool = False


@dataclass
class Decision:
    action: str          # "set_limit" | "none" | "skip"
    vin: str
    limit_pct: int | None = None
    place_name: str | None = None
    reason: str = ""
    trip_mode_override: bool = False

    def command_summary(self) -> str:
        if self.action == "set_limit":
            return f"set_charge_limit({self.vin}, {self.limit_pct})"
        return ""


def decide(reading: VehicleReading, places: list[PlaceRule],
           state: LimitState, now: float | None = None,
           default_limit_pct: int | None = None) -> Decision:
    """Decide what (if anything) to do about one vehicle's charge limit."""
    now = time.time() if now is None else now
    vin = reading.vin
    vs = state.for_vin(vin)

    if reading.asleep:
        return Decision(action="skip", vin=vin,
                        reason=f"{reading.name} is asleep — no-wake reads only; "
                               "will check again on the next cycle.")

    if reading.lat is None or reading.lon is None:
        return Decision(action="skip", vin=vin,
                        reason=f"{reading.name} has no location in this read — skipping.")

    place = match_place(places, reading.lat, reading.lon)

    # --- not at any ruled place ----------------------------------------
    if place is None:
        if vs.inside_place is not None:
            departed = vs.inside_place
            vs.inside_place = None
            vs.applied.pop(departed, None)
            if default_limit_pct is not None and reading.charge_limit_pct != default_limit_pct:
                return Decision(action="set_limit", vin=vin,
                                limit_pct=default_limit_pct, place_name=None,
                                reason=f"{reading.name} left {departed}; restoring "
                                       f"default limit {default_limit_pct}%.")
            return Decision(action="none", vin=vin,
                            reason=f"{reading.name} left {departed}; no rule for "
                                   "this location — leaving the limit alone.")
        return Decision(action="none", vin=vin,
                        reason=f"{reading.name} is not at a ruled place — no action.")

    # --- at a ruled place ------------------------------------------------
    current = reading.charge_limit_pct if reading.charge_limit_pct is not None else -1

    if vs.inside_place == place.name and place.name in vs.applied:
        # Re-read while parked here: already handled this arrival.
        return Decision(action="none", vin=vin, place_name=place.name,
                        reason=f"{reading.name} is still at {place.name}; the rule "
                               f"({place.limit_pct}%) was already applied this visit — "
                               "not sending it again.")

    # New arrival at this place.
    vs.inside_place = place.name

    if place.name in vs.applied:
        # State survived but a fresh arrival was detected after a missed
        # departure; treat as already applied for safety (exactly-once).
        return Decision(action="none", vin=vin, place_name=place.name,
                        reason=f"{reading.name} arrived at {place.name}; rule already "
                               "applied for this visit — exactly-once kept.")

    vs.applied[place.name] = True

    if current == place.limit_pct:
        vs.last_applied_limit = place.limit_pct
        return Decision(action="none", vin=vin, place_name=place.name,
                        reason=f"{reading.name} arrived at {place.name}; limit is "
                               f"already {place.limit_pct}% — nothing to send.")

    if current > place.limit_pct:
        # Trip-mode override: the driver set a higher limit (e.g. road trip);
        # never fight it — lowering would cut their trip charge.
        vs.last_applied_limit = current
        return Decision(action="none", vin=vin, place_name=place.name,
                        trip_mode_override=True,
                        reason=f"{reading.name} arrived at {place.name}, but its limit "
                               f"is {current}% — higher than this place's "
                               f"{place.limit_pct}% rule. Leaving it alone so your "
                               "trip charge isn't cut (trip-mode override).")

    # current < place.limit_pct (or unknown): enforce the place rule.
    vs.last_applied_limit = place.limit_pct
    current_text = f" (it reads {current}% right now)" if current >= 0 else ""
    return Decision(action="set_limit", vin=vin, limit_pct=place.limit_pct,
                    place_name=place.name,
                    reason=f"{reading.name} arrived at {place.name} — setting charge "
                           f"limit to {place.limit_pct}%{current_text}.")
