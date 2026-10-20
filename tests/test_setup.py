"""The setup wizard must produce a config the app can load — no developer
knowledge required from the (scripted) user."""
import json

from tesla_location_limits.config import load_settings
from tesla_location_limits.setup import run_setup


def _scripted(answers):
    it = iter(answers)
    return lambda prompt="": next(it)


def _fake_geocode(matches):
    def fn(address, timeout=12.0):
        return matches
    return fn


def _parse_env(path):
    env = {}
    for line in path.read_text().splitlines():
        if "=" in line and not line.startswith("#"):
            k, v = line.split("=", 1)
            env[k] = v
    return env


def test_wizard_full_flow_writes_loadable_env(tmp_path):
    answers = [
        "cid-123", "secret-xyz",              # step 1: Tesla credentials
        "Model Y", "VINY", "",                # step 2: one car, then done
        "Home", "123 Main St, Palmdale, CA",  # step 3: place name + address
        "y",                                  #   confirm geocode match
        "",                                   #   limit default (home -> 80)
        "",                                   #   radius default (150)
        "",                                   #   no more places
        "1",                                  # step 4: no AI
        "", "",                               # step 5: default port, no password
    ]
    env_file = tmp_path / ".env"
    run_setup(input_fn=_scripted(answers), out=lambda *a: None,
              env_path=str(env_file),
              geocode_fn=_fake_geocode(
                  [("123 Main St, Palmdale, CA, USA", 34.5794, -118.1185)]))
    settings = load_settings(_parse_env(env_file))
    assert [v.name for v in settings.vehicles] == ["Model Y"]
    assert [p.name for p in settings.places] == ["Home"]
    assert settings.places[0].limit_pct == 80          # home hint default
    assert settings.places[0].lat == 34.5794
    assert settings.ai_mode == "never"
    assert settings.server_port == 8643


def test_wizard_geocode_failure_falls_back_to_coordinates(tmp_path):
    from tesla_location_limits.setup import GeocodeError

    def broken(address, timeout=12.0):
        raise GeocodeError("Couldn't reach the address lookup service")

    answers = [
        "cid", "sec", "Solo", "VIN1", "",
        "Cabin", "7 Pine Trail, Acton, CA",
        "2",                        # offline fallback: type coordinates
        "34.24, -118.10",
        "90",                       # limit
        "",                         # radius default
        "",                         # no more places
        "2", "", "",
    ]
    env_file = tmp_path / ".env"
    run_setup(input_fn=_scripted(answers), out=lambda *a: None,
              env_path=str(env_file), geocode_fn=broken)
    settings = load_settings(_parse_env(env_file))
    assert settings.places[0].name == "Cabin"
    assert settings.places[0].lat == 34.24
    assert settings.places[0].limit_pct == 90


def test_wizard_keeps_existing_when_declined(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text("TLCL_VEHICLES=Keep=VIN0\n")
    run_setup(input_fn=_scripted(["n"]), out=lambda *a: None,
              env_path=str(env_file))
    assert "Keep" in env_file.read_text()


def test_wizard_rejects_cabin_with_no_places_until_one_given(tmp_path):
    answers = [
        "cid", "sec", "Solo", "VIN1", "",
        "",                          # blank place name -> nudged to add one
        "Home", "123 Main St, Palmdale, CA",
        "y", "", "", "",
        "1", "", "",
    ]
    env_file = tmp_path / ".env"
    run_setup(input_fn=_scripted(answers), out=lambda *a: None,
              env_path=str(env_file),
              geocode_fn=_fake_geocode(
                  [("123 Main St, Palmdale, CA, USA", 34.5794, -118.1185)]))
    settings = load_settings(_parse_env(env_file))
    assert len(settings.places) == 1


def test_multiple_geocode_matches_numbered_choice(tmp_path):
    answers = [
        "cid", "sec", "Solo", "VIN1", "",
        "Home", "Main St",
        "2",                          # pick the 2nd match
        "", "", "",                   # limit/radius defaults, done
        "1", "", "",
    ]
    env_file = tmp_path / ".env"
    run_setup(input_fn=_scripted(answers), out=lambda *a: None,
              env_path=str(env_file),
              geocode_fn=_fake_geocode([
                  ("Main St, Lancaster, CA, USA", 34.6941, -118.1367),
                  ("Main St, Palmdale, CA, USA", 34.5794, -118.1185),
              ]))
    settings = load_settings(_parse_env(env_file))
    assert settings.places[0].lat == 34.5794
