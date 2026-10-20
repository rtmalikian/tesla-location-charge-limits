"""Fake fleet for tests and demos. No API calls, no real cars.

Drives pretend cars between Home, Office and Cabin over virtual time and
runs the REAL rules engine, so the printed timeline proves the core
behaviors: limit set exactly once per arrival, trip-mode override honored,
asleep cars skipped (no-wake).
"""
from __future__ import annotations

from dataclasses import dataclass

from .config import PlaceRule, Settings, VehicleConfig
from .rules import LimitState

# Pretend geography (DEMO DATA — not anyone's real address).
HOME = (34.5794, -118.1185)
OFFICE = (34.6934, -118.1380)
CABIN = (34.2445, -118.1027)
ELSEWHERE = (34.6100, -118.1500)   # on the road, no rule


def demo_places() -> list[PlaceRule]:
    return [
        PlaceRule(name="Home", address="123 Main St, Palmdale, CA",
                  lat=HOME[0], lon=HOME[1], radius_m=150, limit_pct=80),
        PlaceRule(name="Office", address="1 Corporate Way, Palmdale, CA",
                  lat=OFFICE[0], lon=OFFICE[1], radius_m=150, limit_pct=80),
        PlaceRule(name="Cabin", address="7 Pine Trail, Acton, CA",
                  lat=CABIN[0], lon=CABIN[1], radius_m=150, limit_pct=90),
    ]


# (clock, vin, (lat, lon), limit_pct, asleep)
SCENARIO = [
    ("8:00 AM",  "VINY", HOME,      70,  True),    # asleep at home -> skipped (no-wake)
    ("8:30 AM",  "VINY", HOME,      70,  False),   # arrival, rule 80 -> COMMAND
    ("9:00 AM",  "VINY", HOME,      80,  False),   # still here -> no repeat (exactly-once)
    ("9:30 AM",  "VINY", ELSEWHERE, 80,  False),   # departure clears the applied flag
    ("10:00 AM", "VINY", CABIN,     80,  False),   # arrival at cabin, rule 90 -> COMMAND
    ("10:30 AM", "VINY", CABIN,     90,  False),   # still here -> no repeat
    ("12:00 PM", "VINY", ELSEWHERE, 90,  False),   # leaves for a road trip...
    ("12:30 PM", "VINY", CABIN,     100, False),   # ...driver cranked to 100 for the trip
                                                  # -> re-arrival, but 100 > 90: TRIP-MODE OVERRIDE, no command
    ("1:00 PM",  "VINY", CABIN,     100, False),   # still here -> no repeat
    ("2:00 PM",  "VIN3", OFFICE,    95,  False),   # Model 3 arrives at office (rule 80)
                                                  # with a manually-set 95 -> OVERRIDE, no command
    ("2:30 PM",  "VIN3", OFFICE,    95,  False),   # still here -> no repeat
    ("3:00 PM",  "VIN3", ELSEWHERE, 95,  False),   # departure
    ("3:30 PM",  "VIN3", OFFICE,    70,  False),   # re-arrives at office with 70 (below rule)
                                                  # -> rule wins, raised: COMMAND to 80
    ("4:00 PM",  "VIN3", OFFICE,    80,  False),   # still here -> no repeat
]


@dataclass
class FakeVehicle:
    vin: str
    name: str
    lat: float | None = None
    lon: float | None = None
    limit_pct: int = 80
    asleep: bool = False


class FakeTeslaClient:
    """Stand-in for TeslaClient with the methods the app uses."""

    def __init__(self, vehicles: list[FakeVehicle]):
        self.vehicles = {v.vin: v for v in vehicles}
        self.command_log: list[tuple[str, str, int]] = []

    def vehicle_data(self, vin: str) -> dict:
        v = self.vehicles[vin]
        return {
            "state": "asleep" if v.asleep else "online",
            "drive_state": {"latitude": v.lat, "longitude": v.lon,
                            "shift_state": None if v.asleep else "P"},
            "charge_state": {"charge_limit_soc": v.limit_pct,
                             "battery_level": 60,
                             "charging_state": "Stopped"},
        }

    def set_charge_limit(self, vin: str, limit_pct: int) -> dict:
        self.vehicles[vin].limit_pct = limit_pct
        self.command_log.append(("set_charge_limit", vin, limit_pct))
        return {"result": True}


def demo_vehicles() -> list[FakeVehicle]:
    return [FakeVehicle(vin="VINY", name="Model Y"),
            FakeVehicle(vin="VIN3", name="Model 3")]


def run_simulation(settings: Settings, state_path: str = ":memory:") -> dict:
    """Drive the scenario through the real service/run_cycle path.

    Returns a dict with timeline lines, commands, and an API cost estimate
    for a full day at the configured poll cadence.
    """
    from .service import LimitService

    settings = _demo_settings(settings)
    fake = FakeTeslaClient(demo_vehicles())
    service = LimitService(settings, client_factory=lambda: fake,
                           llm_factory=lambda: None)
    state = _ephemeral_state(settings, state_path)

    timeline: list[str] = []
    all_commands: list[str] = []
    minute = 0
    for clock, vin, (lat, lon), limit, asleep in SCENARIO:
        minute += 30
        car = fake.vehicles[vin]
        car.lat, car.lon, car.asleep = lat, lon, asleep
        if not asleep:
            car.limit_pct = limit  # driver's manual setting (if any)
        decisions, commands = service.run_cycle(state=state, now=float(minute * 60))
        decision = next(d for d in decisions if d.vin == vin)
        marker = " "
        if commands:
            marker = "★"
            all_commands.extend(commands)
        elif decision.trip_mode_override:
            marker = "⤺"
        timeline.append(f"{marker} {clock}  {car.name:8s}  {decision.reason}")
    return {"timeline": timeline, "commands": all_commands,
            "cost_lines": _estimate_daily_cost(settings),
            "poll_seconds": settings.poll_seconds}


def _demo_settings(settings: Settings) -> Settings:
    settings.vehicles = [VehicleConfig(name=v.name, vin=v.vin)
                         for v in demo_vehicles()]
    settings.places = demo_places()
    settings.dry_run = False  # fake fleet; actually "execute" to show commands
    return settings


def _ephemeral_state(settings: Settings, state_path: str) -> LimitState:
    if state_path == ":memory:":
        import tempfile
        state_path = tempfile.mktemp(prefix="tlcl-sim-state-")
    return LimitState(state_path)


def _estimate_daily_cost(settings: Settings) -> list[str]:
    polls = 86400 / max(1, settings.poll_seconds)
    n = max(1, len(settings.vehicles))
    data_calls = int(polls * n)
    commands = 3  # observed in the scenario: ~2-4 arrivals/day is typical
    day = data_calls / 500.0 * 1.0 + commands / 1000.0 * 1.0  # community-reported rates
    lines = [
        f"Estimated daily API usage at the {settings.poll_seconds}s poll ({n} cars):",
        f"  ~{data_calls} data calls (no-wake reads) + ~{commands} commands + 0 wake-ups",
        f"  ≈ ${day:.2f}/day ≈ ${day * 30:.0f}/month at community-reported rates "
        "($1 per 500 data calls, $1 per 1,000 commands).",
        "  Wakes are zero by design — the app never wakes a sleeping car.",
        "  Stretch TLCL_POLL_SECONDS (e.g. 900 = 15 min) to cut the bill roughly in proportion.",
    ]
    return lines


def run_demo(settings: Settings) -> list[str]:
    """Human-readable demo output for `tlcl simulate`: timeline + chat Q&A
    against the fake fleet, using the real service (LLM off)."""
    from .service import LimitService

    result = run_simulation(settings)
    out = ["", "Pretend fleet, pretend places — the REAL rules engine:",
           "  Home → 80% · Office → 80% · Cabin → 90% (radius 150 m each)", ""]
    out.extend(result["timeline"])
    out += ["",
            "Legend: ★ sent a charge-limit command · ⤺ trip-mode override (left alone)",
            "", *result["cost_lines"], "", "Chat demo (rules engine, no AI needed):"]
    fake = FakeTeslaClient(demo_vehicles())
    for vin, (lat, lon) in (("VINY", CABIN), ("VIN3", OFFICE)):
        fake.vehicles[vin].lat, fake.vehicles[vin].lon = lat, lon
    settings2 = _demo_settings(Settings())
    service = LimitService(settings2, client_factory=lambda: fake,
                           llm_factory=lambda: None, preview_only=True)
    for q in ["what's the limit at the cabin?",
              "which places have rules?",
              "set the cabin to 90",
              "why is my limit 80% right now?"]:
        out.append(f"> {q}")
        out.append(f"  {service.answer(q)}")
    out.append("")
    return out
