import json

import pytest

from tesla_location_limits.config import ConfigError, load_settings

BASE = {"TLCL_VEHICLES": "Model Y=VINY,Model 3=VIN3"}
PLACES = json.dumps([
    {"name": "Home", "address": "123 Main St", "lat": 34.5, "lon": -118.1,
     "radius_m": 150, "limit_pct": 80},
    {"name": "Cabin", "lat": 34.2, "lon": -118.1, "limit_pct": 90},
])


def test_parse_vehicles():
    s = load_settings(dict(BASE))
    assert [v.name for v in s.vehicles] == ["Model Y", "Model 3"]
    assert s.server_port == 8643 and s.ai_mode == "auto"
    assert s.ai_base_url == "http://localhost:11434/v1"


def test_parse_places():
    s = load_settings({**BASE, "TLCL_PLACES": PLACES})
    assert [p.name for p in s.places] == ["Home", "Cabin"]
    assert s.places[0].limit_pct == 80
    assert s.places[1].radius_m == 150  # defaulted


def test_limit_clamped_high_and_low():
    raw = json.dumps([
        {"name": "A", "lat": 1, "lon": 1, "limit_pct": 120},
        {"name": "B", "lat": 2, "lon": 2, "limit_pct": 30},
    ])
    s = load_settings({**BASE, "TLCL_PLACES": raw})
    assert s.places[0].limit_pct == 100
    assert s.places[1].limit_pct == 50


def test_bad_places_json():
    with pytest.raises(ConfigError):
        load_settings({**BASE, "TLCL_PLACES": "{not json"})


def test_places_must_be_list():
    with pytest.raises(ConfigError):
        load_settings({**BASE, "TLCL_PLACES": '{"name": "Home"}'})


def test_place_needs_lat_lon():
    with pytest.raises(ConfigError):
        load_settings({**BASE, "TLCL_PLACES": '[{"name": "Home", "lat": "x"}]'})


def test_place_needs_name():
    with pytest.raises(ConfigError):
        load_settings({**BASE, "TLCL_PLACES": '[{"lat": 1, "lon": 2}]'})


def test_empty_places_ok():
    s = load_settings(dict(BASE))
    assert s.places == []


def test_default_limit_optional():
    s = load_settings({**BASE, "TLCL_DEFAULT_LIMIT_PCT": "85"})
    assert s.default_limit_pct == 85
    assert load_settings(dict(BASE)).default_limit_pct is None


def test_bad_region():
    with pytest.raises(ConfigError):
        load_settings({**BASE, "TESLA_REGION": "moon"})


def test_bad_ai_mode():
    with pytest.raises(ConfigError):
        load_settings({**BASE, "TLCL_AI_MODE": "sometimes"})


def test_ai_base_trailing_slash_trimmed():
    s = load_settings({**BASE, "TLCL_AI_BASE_URL": "http://x/v1/"})
    assert s.ai_base_url == "http://x/v1"


def test_dry_run_parse():
    assert load_settings({**BASE, "TLCL_DRY_RUN": "1"}).dry_run is True
