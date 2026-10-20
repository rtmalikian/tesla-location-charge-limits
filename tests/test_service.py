"""Service-level tests: OpenAI response shapes, rules-first Q&A, natural
language rule edits, LLM command-block allowlisting, and the enforcement
cycle (dry-run honored)."""
import json

from tesla_location_limits.config import PlaceRule, Settings, VehicleConfig
from tesla_location_limits.rules import LimitState
from tesla_location_limits.service import LimitService
from tesla_location_limits.simulator import (CABIN, HOME, FakeTeslaClient,
                                             FakeVehicle)


def _settings(**over):
    s = Settings()
    s.vehicles = [VehicleConfig(name="Model Y", vin="VINY")]
    s.places = [PlaceRule(name="Home", lat=34.5794, lon=-118.1185,
                          radius_m=150, limit_pct=80),
                PlaceRule(name="Cabin", lat=34.2445, lon=-118.1027,
                          radius_m=150, limit_pct=90)]
    for k, v in over.items():
        setattr(s, k, v)
    return s


def _service(**over):
    fake = FakeTeslaClient([FakeVehicle(vin="VINY", name="Model Y")])
    svc = LimitService(_settings(**over), client_factory=lambda: fake,
                       llm_factory=lambda: None)
    return svc, fake


# -- OpenAI surface -------------------------------------------------------

def test_models_list_shape():
    svc, _ = _service()
    resp = svc.models_list()
    assert resp["object"] == "list"
    model = resp["data"][0]
    assert model["id"] == "tesla-location-charge-limits"
    assert model["object"] == "model"
    assert "created" in model


def test_chat_completions_shape():
    svc, _ = _service()
    resp = svc.chat_completions(
        {"model": "tesla-location-charge-limits",
         "messages": [{"role": "user", "content": "which places have rules?"}]})
    assert resp["object"] == "chat.completion"
    choice = resp["choices"][0]
    assert choice["message"]["role"] == "assistant"
    assert "Home" in choice["message"]["content"]
    assert choice["finish_reason"] == "stop"
    assert resp["usage"]["total_tokens"] >= 0


def test_chat_empty_message_gives_capabilities():
    svc, _ = _service()
    resp = svc.chat_completions({"messages": []})
    assert "charge limits" in resp["choices"][0]["message"]["content"].lower()


# -- rules-first Q&A -------------------------------------------------------

def test_limit_at_known_place():
    svc, _ = _service()
    assert "90%" in svc.answer("what's the limit at the cabin?")


def test_limit_at_unknown_place():
    svc, _ = _service()
    out = svc.answer("what's the limit at the moon?")
    assert "don't have a place" in out


def test_list_places():
    svc, _ = _service()
    out = svc.answer("which places have rules?")
    assert "Home" in out and "Cabin" in out and "80%" in out and "90%" in out


def test_why_question_uses_live_reading():
    svc, fake = _service()
    fake.vehicles["VINY"].lat, fake.vehicles["VINY"].lon = CABIN
    fake.vehicles["VINY"].limit_pct = 90
    out = svc.answer("why is my limit 90% right now?")
    assert "Cabin" in out


def test_rule_edit_set_cabin():
    svc, _ = _service()
    out = svc.answer("set the cabin to 95")
    assert "95%" in out
    assert next(p for p in svc.settings.places if p.name == "Cabin").limit_pct == 95


def test_rule_edit_should_be_form():
    svc, _ = _service()
    out = svc.answer("home should be 75 percent")
    assert "75%" in out
    assert next(p for p in svc.settings.places if p.name == "Home").limit_pct == 75


def test_rule_edit_clamped():
    svc, _ = _service()
    out = svc.answer("set home to 200")
    assert "100%" in out
    assert next(p for p in svc.settings.places if p.name == "Home").limit_pct == 100


def test_rule_edit_unknown_place():
    svc, _ = _service()
    out = svc.answer("set the moon base to 80")
    assert "don't have a place" in out


def test_rule_edit_preview_only_does_not_persist():
    svc, _ = _service()
    svc.preview_only = True
    out = svc.answer("set the cabin to 77")
    assert "preview" in out.lower()
    assert next(p for p in svc.settings.places if p.name == "Cabin").limit_pct == 90


def test_rule_edit_persists_to_env(tmp_path):
    env_file = tmp_path / ".env"
    places = [{"name": "Home", "lat": 34.5, "lon": -118.1,
               "radius_m": 150, "limit_pct": 80}]
    env_file.write_text(f"TLCL_PLACES={json.dumps(places)}\nOTHER=1\n")
    svc = LimitService(_settings(), client_factory=lambda: FakeTeslaClient([]),
                       llm_factory=lambda: None, env_path=str(env_file))
    svc.answer("set home to 70")
    raw = json.loads([line for line in env_file.read_text().splitlines()
                      if line.startswith("TLCL_PLACES=")][0].split("=", 1)[1])
    assert raw[0]["limit_pct"] == 70


# -- LLM fallback ----------------------------------------------------------

def test_llm_command_block_applies_allowlisted_edit():
    class FakeLlm:
        def chat(self, context, message):
            return ('```json\n{"action": "set_place_limit", '
                    '"place": "Cabin", "limit_pct": 88}\n```')

    svc = LimitService(_settings(), client_factory=lambda: None,
                       llm_factory=lambda: FakeLlm())
    out = svc.answer("the cabin rule is wrong, fix it")
    assert "88%" in out
    assert next(p for p in svc.settings.places if p.name == "Cabin").limit_pct == 88


def test_llm_non_allowlisted_action_ignored():
    class FakeLlm:
        def chat(self, context, message):
            return ('```json\n{"action": "set_sentry_mode", '
                    '"place": "Cabin", "limit_pct": 88}\n```')

    svc = LimitService(_settings(), client_factory=lambda: None,
                       llm_factory=lambda: FakeLlm())
    out = svc.answer("do something weird at the cabin")
    # The raw model text is returned; no rule was changed.
    assert next(p for p in svc.settings.places if p.name == "Cabin").limit_pct == 90
    assert "set_sentry_mode" in out


def test_llm_failure_falls_back_to_capabilities():
    class BrokenLlm:
        def chat(self, context, message):
            raise RuntimeError("down")

    svc = LimitService(_settings(), client_factory=lambda: None,
                       llm_factory=lambda: BrokenLlm())
    out = svc.answer("tell me about charging curves")
    assert "charge limits" in out.lower()


# -- enforcement cycle ------------------------------------------------------

def test_run_cycle_sends_command_once_and_honors_dry_run(tmp_path):
    state_path = str(tmp_path / "state.json")
    svc, fake = _service()
    fake.vehicles["VINY"].lat, fake.vehicles["VINY"].lon = HOME
    fake.vehicles["VINY"].limit_pct = 70

    decisions, commands = svc.run_cycle(state=LimitState(state_path), now=1.0)
    assert commands == ["set_charge_limit(VINY, 80)"]
    assert fake.vehicles["VINY"].limit_pct == 80

    # Second cycle: exactly-once.
    decisions, commands = svc.run_cycle(state=LimitState(state_path), now=2.0)
    assert commands == []

    # Dry run: a fresh arrival computes the decision, reports the command,
    # but does not execute it.
    fake.vehicles["VINY"].limit_pct = 70
    decisions, commands = svc.run_cycle(state=LimitState(str(tmp_path / "dry.json")),
                                        now=3.0, dry_run=True)
    assert fake.vehicles["VINY"].limit_pct == 70
    assert commands == ["[dry-run] set_charge_limit(VINY, 80)"]
