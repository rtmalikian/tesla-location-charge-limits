"""Unit tests for the geofence rules engine. No network, no time-of-day
dependence — `now` is injected everywhere."""
import math

import pytest

from tesla_location_limits.config import PlaceRule
from tesla_location_limits.rules import (VehicleReading, decide, haversine_m,
                                         LimitState, match_place)

HOME = PlaceRule(name="Home", lat=34.5794, lon=-118.1185, radius_m=150, limit_pct=80)
CABIN = PlaceRule(name="Cabin", lat=34.2445, lon=-118.1027, radius_m=150, limit_pct=90)


def reading(vin="VINY", name="Model Y", lat=34.5794, lon=-118.1185,
            limit=70, asleep=False):
    return VehicleReading(name=name, vin=vin, lat=lat, lon=lon,
                          charge_limit_pct=limit, asleep=asleep)


def fresh_state(tmp_path):
    return LimitState(str(tmp_path / "state.json"))


def test_haversine_sanity():
    # ~150 m radius: a point 150 m north of home centre must match.
    d_lat = 150 / 111_320.0
    assert haversine_m(HOME.lat, HOME.lon, HOME.lat + d_lat, HOME.lon) == pytest.approx(150, abs=2)
    assert match_place([HOME], HOME.lat + d_lat, HOME.lon) is HOME


def test_boundary_is_inside():
    d_lat = 150 / 111_320.0
    # Just outside the radius: no match.
    d_lat_out = 151 / 111_320.0
    assert match_place([HOME], HOME.lat + d_lat_out, HOME.lon) is None
    # Just inside: match.
    assert match_place([HOME], HOME.lat + d_lat - 1e-6, HOME.lon) is HOME


def test_innermost_place_wins():
    small = PlaceRule(name="Driveway", lat=HOME.lat, lon=HOME.lon,
                      radius_m=30, limit_pct=90)
    assert match_place([HOME, small], HOME.lat, HOME.lon) is small


def test_no_places_no_action(tmp_path):
    d = decide(reading(), [], fresh_state(tmp_path), now=1.0)
    assert d.action == "none"


def test_arrival_sets_place_limit(tmp_path):
    st = fresh_state(tmp_path)
    d = decide(reading(limit=70), [HOME], st, now=1.0)
    assert d.action == "set_limit" and d.limit_pct == 80
    assert "Home" in d.reason


def test_no_repeat_while_parked(tmp_path):
    st = fresh_state(tmp_path)
    d1 = decide(reading(limit=70), [HOME], st, now=1.0)
    assert d1.action == "set_limit"
    d2 = decide(reading(limit=80), [HOME], st, now=2.0)
    assert d2.action == "none"  # already applied this visit: exactly-once
    d3 = decide(reading(limit=80), [HOME], st, now=3.0)
    assert d3.action == "none"  # still no second command


def test_departure_rearms(tmp_path):
    st = fresh_state(tmp_path)
    decide(reading(limit=70), [HOME], st, now=1.0)
    d = decide(reading(lat=34.9, lon=-118.3, limit=80), [HOME], st, now=2.0)
    assert d.action == "none"
    d = decide(reading(limit=70), [HOME], st, now=3.0)  # re-arrive
    assert d.action == "set_limit" and d.limit_pct == 80


def test_manual_higher_limit_never_lowered(tmp_path):
    st = fresh_state(tmp_path)
    d = decide(reading(limit=100, lat=CABIN.lat, lon=CABIN.lon), [CABIN], st, now=1.0)
    assert d.action == "none"
    assert d.trip_mode_override is True
    assert "100" in d.reason and "90" in d.reason
    # And it stays that way on the next cycle.
    d2 = decide(reading(limit=100, lat=CABIN.lat, lon=CABIN.lon), [CABIN], st, now=2.0)
    assert d2.action == "none"


def test_manual_lower_limit_raised_to_rule(tmp_path):
    st = fresh_state(tmp_path)
    d = decide(reading(limit=70, lat=CABIN.lat, lon=CABIN.lon), [CABIN], st, now=1.0)
    assert d.action == "set_limit" and d.limit_pct == 90


def test_limit_already_correct(tmp_path):
    st = fresh_state(tmp_path)
    d = decide(reading(limit=80), [HOME], st, now=1.0)
    assert d.action == "none" and d.place_name == "Home"


def test_asleep_car_skipped(tmp_path):
    st = fresh_state(tmp_path)
    d = decide(reading(limit=70, asleep=True), [HOME], st, now=1.0)
    assert d.action == "skip"
    # Waking later still counts as one arrival.
    d2 = decide(reading(limit=70), [HOME], st, now=2.0)
    assert d2.action == "set_limit"


def test_no_location_skipped(tmp_path):
    st = fresh_state(tmp_path)
    r = reading(limit=70)
    r.lat, r.lon = None, None
    d = decide(r, [HOME], st, now=1.0)
    assert d.action == "skip"


def test_default_limit_on_departure(tmp_path):
    st = fresh_state(tmp_path)
    decide(reading(limit=70), [HOME], st, now=1.0)
    d = decide(reading(lat=34.9, lon=-118.3, limit=80), [HOME], st, now=2.0,
               default_limit_pct=85)
    assert d.action == "set_limit" and d.limit_pct == 85
    # And no default configured: left alone.
    st2 = fresh_state(tmp_path / "s2.json")
    decide(reading(limit=70), [HOME], st2, now=1.0)
    d2 = decide(reading(lat=34.9, lon=-118.3, limit=80), [HOME], st2, now=2.0)
    assert d2.action == "none"


def test_state_persists_across_instances(tmp_path):
    path = str(tmp_path / "state.json")
    st = LimitState(path)
    decide(reading(limit=70), [HOME], st, now=1.0)
    st.save()
    st2 = LimitState(path)
    d = decide(reading(limit=80), [HOME], st2, now=2.0)
    assert d.action == "none"  # exactly-once survives process restarts


def test_one_degree_latitude_is_111km():
    assert haversine_m(0, 0, 1, 0) == pytest.approx(111_195, abs=500)
