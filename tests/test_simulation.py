"""End-to-end: the simulated fleet day asserts the core invariant — on
arrival at a ruled place the limit is set EXACTLY ONCE to the place's
limit, and a manually-set higher limit is NEVER lowered."""
import json
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

from tesla_location_limits.config import Settings, VehicleConfig
from tesla_location_limits.server import make_handler
from tesla_location_limits.service import LimitService
from tesla_location_limits.simulator import (FakeTeslaClient, demo_places,
                                             demo_vehicles, run_demo,
                                             run_simulation)


def _settings(**kw):
    return Settings(vehicles=[VehicleConfig("Model Y", "VINY"),
                              VehicleConfig("Model 3", "VIN3")],
                    ai_mode="never", **kw)


def test_end_to_end_invariant():
    """The headline promise of the app, over a full simulated day."""
    result = run_simulation(_settings())

    # Exactly three commands in the whole scenario...
    assert result["commands"] == [
        "set_charge_limit(VINY, 80)",   # 8:30 arrival at Home
        "set_charge_limit(VINY, 90)",   # 10:00 arrival at Cabin
        "set_charge_limit(VIN3, 80)",   # 3:30 re-arrival at Office below rule
    ]

    # ...each matching its place's rule — never a raise to the wrong value,
    # never a lowering of a driver's manual setting.
    timeline = "\n".join(result["timeline"])
    override_lines = [l for l in result["timeline"] if "trip-mode override" in l]
    assert len(override_lines) == 2  # 12:30 cabin re-arrival, 2:00 office arrival
    assert any("12:30 PM" in l for l in override_lines)
    assert any("2:00 PM" in l for l in override_lines)
    # The 2:00 PM office arrival at 95 > 80 also sent no command — the
    # override held; the raise only happened after a departure + re-arrival.
    office_lines = [l for l in result["timeline"] if "Model 3" in l]
    assert any("2:00 PM" in l and "Leaving it alone" in l for l in office_lines)


def test_no_command_without_arrival():
    result = run_simulation(_settings())
    # 14 scenario steps, only 3 commands: everything else was skip/no-op.
    assert len(result["commands"]) == 3
    assert len(result["timeline"]) == 14


def test_simulate_cli_output_contains_cost_estimate():
    lines = run_demo(_settings())
    text = "\n".join(lines)
    assert "Estimated daily API usage" in text
    assert "wake-ups" in text
    assert "what's the limit at the cabin?" in text
    assert "90%" in text


def test_http_chat_roundtrip_and_shapes():
    fake = FakeTeslaClient(demo_vehicles())
    settings = _settings()
    settings.places = demo_places()
    service = LimitService(settings, client_factory=lambda: fake,
                           llm_factory=lambda: None)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(service))
    port = httpd.server_address[1]
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        body = json.dumps({"model": "tesla-location-charge-limits", "messages": [
            {"role": "user", "content": "which places have rules?"}]}).encode()
        req = urllib.request.Request(f"http://127.0.0.1:{port}/v1/chat/completions",
                                     data=body,
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
        assert "Home" in data["choices"][0]["message"]["content"]

        with urllib.request.urlopen(f"http://127.0.0.1:{port}/v1/models",
                                    timeout=10) as resp:
            models = json.loads(resp.read())
        assert models["data"][0]["id"] == "tesla-location-charge-limits"

        # OpenAI-style error shape on unknown paths.
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/v1/embeddings",
                                   timeout=10)
            raise AssertionError("expected 404")
        except urllib.error.HTTPError as e:
            assert e.code == 404
            err = json.loads(e.read())
            assert err["error"]["type"] == "invalid_request_error"
            assert "message" in err["error"]

        with urllib.request.urlopen(f"http://127.0.0.1:{port}/health",
                                    timeout=10) as resp:
            assert json.loads(resp.read())["status"] == "ok"
    finally:
        httpd.shutdown()


def test_http_auth_required_when_key_set():
    fake = FakeTeslaClient(demo_vehicles())
    service = LimitService(_settings(server_api_key="secret"),
                           client_factory=lambda: fake, llm_factory=lambda: None)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(service))
    port = httpd.server_address[1]
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/v1/models", timeout=10)
            raise AssertionError("expected 401")
        except urllib.error.HTTPError as e:
            assert e.code == 401
        req = urllib.request.Request(
            f"http://127.0.0.1:{port}/v1/models",
            headers={"Authorization": "Bearer secret"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            assert resp.status == 200
    finally:
        httpd.shutdown()
