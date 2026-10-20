"""Live fleet snapshot: no-wake reads of each configured vehicle, distilled
into readings the rules engine, LLM context builder, and command executor
share. Location + charge-limit + asleep state are the fields that matter."""
from __future__ import annotations

from dataclasses import dataclass, field

from .rules import VehicleReading


@dataclass
class FleetSnapshot:
    readings: list[VehicleReading] = field(default_factory=list)
    unreachable: dict = field(default_factory=dict)  # vin -> error text

    def brief(self, places) -> str:
        """Compact text the LLM receives as context."""
        lines = []
        for r in self.readings:
            limit = f"{r.charge_limit_pct}%" if r.charge_limit_pct is not None else "unknown"
            state = "asleep" if r.asleep else "awake"
            place_hit = ""
            from .rules import match_place
            hit = match_place(places, r.lat, r.lon) if r.lat is not None and r.lon is not None else None
            if hit:
                place_hit = f", currently at {hit.name} (rule: {hit.limit_pct}%)"
            lines.append(f"- {r.name}: charge limit {limit}, {state}{place_hit}")
        for vin, err in self.unreachable.items():
            lines.append(f"- {vin}: unreachable ({err})")
        return "\n".join(lines) or "(no vehicles)"


def _drive(d: dict) -> dict:
    return d.get("drive_state") or {}


def _charge(d: dict) -> dict:
    return d.get("charge_state") or {}


def snapshot_fleet(client, vehicles: list) -> FleetSnapshot:
    """Read every configured vehicle (no-wake reads only) and build a snapshot."""
    snap = FleetSnapshot()
    for vc in vehicles:
        try:
            data = client.vehicle_data(vc.vin)
        except Exception as exc:  # never let one car break the whole fleet view
            snap.unreachable[vc.vin] = str(exc)[:120]
            continue
        dr, cs = _drive(data), _charge(data)
        asleep = str(data.get("state") or "").lower() == "asleep"
        lat = dr.get("latitude")
        lon = dr.get("longitude")
        snap.readings.append(VehicleReading(
            name=vc.name,
            vin=vc.vin,
            lat=float(lat) if lat is not None else None,
            lon=float(lon) if lon is not None else None,
            charge_limit_pct=(int(cs.get("charge_limit_soc"))
                              if cs.get("charge_limit_soc") is not None else None),
            asleep=asleep,
        ))
    return snap


def find_vehicle(snap: FleetSnapshot, mention: str) -> VehicleReading | None:
    """Best-effort vehicle resolution from a free-text mention."""
    needle = mention.strip().lower()
    if not needle:
        return None
    for r in snap.readings:
        names = [r.name.lower()]
        for nm in names:
            if nm and (needle in nm or nm in needle):
                return r
    return None


def find_place(places, mention: str):
    """Best-effort place resolution from a free-text mention."""
    needle = mention.strip().lower()
    if not needle:
        return None
    for p in places:
        if p.name.lower() in needle or needle in p.name.lower():
            return p
    return None
