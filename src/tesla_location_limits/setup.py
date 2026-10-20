"""Guided first-run setup: plain-language questions, writes .env itself.

Built for Tesla owners, not developers — defaults everywhere, numbered
choices, places by name + street address (geocoded automatically), and
every value explained in everyday words. Testable: input, output, and the
geocoder are injectable. Never shows a stack trace to the user.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import requests

NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
DEFAULT_LIMIT_HINTS = {"home": 80, "office": 80, "work": 80, "cabin": 90}


def _ask(input_fn, out, prompt: str, default: str = "") -> str:
    suffix = f" [{default}]" if default else ""
    out(f"{prompt}{suffix}")
    answer = input_fn("> ").strip()
    return answer or default


def _ask_int(input_fn, out, prompt: str, default: int) -> int:
    while True:
        raw = _ask(input_fn, out, prompt, str(default))
        try:
            return int(raw)
        except ValueError:
            out(f"Please type a whole number (like {default}) — or press Enter to keep {default}.")


def _ask_required(input_fn, out, prompt: str, help_text: str) -> str:
    while True:
        out(help_text)
        answer = input_fn(f"{prompt} ").strip()
        if answer:
            return answer
        out("That one's required — without it the app can't talk to your car.")


def geocode_nominatim(address: str, timeout: float = 12.0) -> list[tuple[str, float, float]]:
    """Look up a street address; returns [(display_name, lat, lon), ...].

    Raises GeocodeError (never a raw exception) with plain-words detail.
    """
    try:
        resp = requests.get(NOMINATIM_URL,
                            params={"q": address, "format": "json", "limit": 5,
                                    "addressdetails": 0},
                            headers={"User-Agent": "tesla-location-charge-limits/0.1.0"},
                            timeout=timeout)
    except requests.RequestException as exc:
        raise GeocodeError(
            "Couldn't reach the address lookup service — check your internet "
            "connection and try again.") from exc
    if resp.status_code != 200:
        raise GeocodeError(
            "The address lookup service isn't cooperating right now (it answered "
            "with an error). Wait a minute and try again.")
    try:
        results = resp.json()
    except ValueError as exc:
        raise GeocodeError(
            "The address lookup service gave a garbled answer. Try again in a bit.") from exc
    out_list = []
    for item in results or []:
        try:
            out_list.append((str(item["display_name"]),
                             float(item["lat"]), float(item["lon"])))
        except (KeyError, TypeError, ValueError):
            continue
    return out_list


class GeocodeError(Exception):
    pass


def _resolve_place(input_fn, out, geocode_fn, address: str) -> tuple[str, float, float] | None:
    """Turn an address into a confirmed (display_name, lat, lon).

    Returns None if the user gives up; handles every failure in plain words.
    """
    if geocode_fn is None:
        geocode_fn = geocode_nominatim
    try:
        matches = geocode_fn(address)
    except GeocodeError as exc:
        out(f"Hmm — {exc}")
        out("Options:")
        out("  1) Try the address again (maybe with the city spelled out)")
        out("  2) Type the coordinates yourself (find them on Google Maps: "
            "right-click the spot → the numbers at the top are lat, lon)")
        choice = _ask(input_fn, out, "Pick 1 or 2 — or press Enter to skip this place:", "")
        if choice == "1":
            return _resolve_place(input_fn, out, geocode_fn,
                                  _ask_required(input_fn, out, "Address:",
                                                "Type the street address again:"))
        if choice == "2":
            coords = _ask_required(input_fn, out, "Coordinates (like 34.57, -118.11):",
                                   "Type latitude and longitude, separated by a comma:")
            try:
                lat_s, lon_s = coords.split(",", 1)
                return (address, float(lat_s), float(lon_s))
            except ValueError:
                out("That didn't look like coordinates — skipping this place.")
                return None
        out("Skipping this place — you can add it later with `tlcl places add`.")
        return None
    if not matches:
        out(f"I couldn't find \"{address}\" — double-check the spelling or add the city.")
        again = _ask_required(input_fn, out, "Address (or press Enter to skip):",
                              "Try a slightly different address:")
        if not again:
            return None
        return _resolve_place(input_fn, out, geocode_fn, again)
    if len(matches) == 1:
        name, lat, lon = matches[0]
        confirm = _ask(input_fn, out, f"Found: {name}. Is that right? (y/n)", "y")
        if confirm.lower() in ("y", "yes"):
            return name, lat, lon
        out("No problem — try the address again with more detail.")
        return _resolve_place(
            input_fn, out, geocode_fn,
            _ask_required(input_fn, out, "Address:", "Type the street address again:"))
    out("A few places match — pick one:")
    for i, (name, _lat, _lon) in enumerate(matches, 1):
        out(f"  {i}) {name}")
    out(f"  {len(matches) + 1}) None of these — try a different address")
    while True:
        choice = _ask(input_fn, out, f"Pick 1–{len(matches) + 1}:", "1")
        try:
            n = int(choice)
        except ValueError:
            n = 0
        if 1 <= n <= len(matches):
            return matches[n - 1]
        if n == len(matches) + 1:
            new_addr = _ask_required(input_fn, out, "Address:",
                                     "Type the street address again:")
            return _resolve_place(input_fn, out, geocode_fn, new_addr)
        out(f"Please pick a number from 1 to {len(matches) + 1}.")


def ask_place(input_fn, out, geocode_fn=None, suggested_limit: int | None = None) -> dict | None:
    """Add one place rule interactively. Returns the place dict or None."""
    name = _ask(input_fn, out, "Place name (e.g. Home, Cabin) — or press Enter if done:")
    if not name:
        return None
    address = _ask_required(
        input_fn, out, "Street address (house number, street, city):",
        f"Where is {name}? A full street address finds it most reliably:")
    resolved = _resolve_place(input_fn, out, geocode_fn, address)
    if resolved is None:
        return None
    display_name, lat, lon = resolved
    default_limit = suggested_limit
    if default_limit is None:
        for hint, pct in DEFAULT_LIMIT_HINTS.items():
            if hint in name.lower():
                default_limit = pct
                break
        default_limit = default_limit if default_limit is not None else 80
    out("Daily driving is happiest around 80%; 90–100% is for trip days "
        "(Tesla's own guidance).")
    limit = _ask_int(input_fn, out,
                     f"Charge limit at {name} (percent):", default_limit)
    limit = max(50, min(100, limit))
    radius = _ask_int(input_fn, out,
                      "How close does the car need to be before the rule kicks in? "
                      "(metres, about a block = 150):", 150)
    out(f"Got it: at {display_name.split(',')[0]} I'll set the limit to "
        f"{limit}% whenever the car is within {radius} metres.")
    return {"name": name, "address": address, "lat": lat, "lon": lon,
            "radius_m": radius, "limit_pct": limit}


def _default_limit_suggestion(name: str) -> int:
    for hint, pct in DEFAULT_LIMIT_HINTS.items():
        if hint in name.lower():
            return pct
    return 80


def run_setup(input_fn=input, out=print, env_path: str = ".env",
              geocode_fn=None) -> Path:
    out("")
    out("Welcome to Tesla Location Charge Limits setup — I'll ask a few")
    out("questions and write the settings file for you. Press Enter to")
    out("accept a [default]. Nothing you type leaves this computer.")
    out("")

    path = Path(env_path)
    if path.exists():
        again = _ask(input_fn, out,
                     "A settings file (.env) already exists. Replace it? (y/n)", "n")
        if again.lower() not in ("y", "yes"):
            out("Keeping your existing settings. Nothing changed.")
            return path

    out("STEP 1 of 5 — Connect to Tesla")
    out("Create a free developer app at developer.tesla.com (sign in, then")
    out("'Create App'). Tesla will show you a Client ID and Client Secret.")
    client_id = _ask_required(input_fn, out, "Paste your Client ID:",
                              "First, the Client ID:")
    client_secret = _ask_required(input_fn, out, "Paste your Client Secret:",
                                  "Now the Client Secret (it stays on this computer):")

    out("")
    out("STEP 2 of 5 — Your cars")
    out("For each car: a nickname and its VIN. Find the VIN in your Tesla")
    out("app under Controls → Software (or on the car's touchscreen).")
    vehicles: list[tuple[str, str]] = []
    while True:
        n = len(vehicles) + 1
        name = _ask(input_fn, out,
                    f"Car {n} nickname (e.g. Model Y) — or press Enter if done:",
                    "" if vehicles else "")
        if not name:
            if vehicles:
                break
            out("Add at least one car to continue.")
            continue
        vin = _ask_required(input_fn, out, f"VIN for {name}:",
                            f"Now the VIN for {name}:")
        vehicles.append((name, vin))

    out("")
    out("STEP 3 of 5 — Your places")
    out("Tell me where your cars park and what charge limit each spot gets.")
    out("I'll set the limit automatically whenever a car arrives.")
    places = []
    while True:
        place = ask_place(input_fn, out, geocode_fn)
        if place is None:
            if places:
                break
            out("One place is enough to start — home, say. (Or you can add "
                "places later with `tlcl places add`.)")
            continue
        places.append(place)

    out("")
    out("STEP 4 of 5 — The AI brain (optional)")
    out("  1) Built-in answers only — no AI, nothing leaves this computer")
    out("  2) Ollama on this computer — free local AI (default)")
    out("  3) Another AI service (OpenAI, Groq, LM Studio…) — you'll paste its details")
    choice = _ask(input_fn, out, "Pick 1, 2 or 3:", "2")
    ai_mode, ai_url, ai_key, ai_model = "auto", "http://localhost:11434/v1", "", "llama3.1"
    if choice == "1":
        ai_mode = "never"
    elif choice == "3":
        ai_url = _ask_required(input_fn, out, "Service address (base URL):",
                               "Paste the service's API address:")
        ai_key = _ask(input_fn, out, "API key for that service (stored only here):")
        ai_model = _ask(input_fn, out, "Model name:", "gpt-4o-mini")

    out("")
    out("STEP 5 of 5 — The chat server")
    port = _ask(input_fn, out, "Port for the chat address:", "8643")
    server_key = _ask(input_fn, out,
                      "Optional password for the chat address (blank = none):")

    lines = [
        "# Written by `tlcl setup` — edit by re-running setup any time.",
        f"TESLA_CLIENT_ID={client_id}",
        f"TESLA_CLIENT_SECRET={client_secret}",
        "TESLA_REGION=na",
        "TESLA_REDIRECT_URI=http://localhost:8080/callback",
        "TLCL_VEHICLES=" + ",".join(f"{name}={vin}" for name, vin in vehicles),
        "TLCL_PLACES=" + json.dumps(places, separators=(",", ":")),
        "TLCL_DEFAULT_LIMIT_PCT=",
        "TLCL_SERVER_HOST=127.0.0.1",
        f"TLCL_SERVER_PORT={port}",
        f"TLCL_SERVER_API_KEY={server_key}",
        "TLCL_MODEL_ID=tesla-location-charge-limits",
        f"TLCL_AI_BASE_URL={ai_url}",
        f"TLCL_AI_API_KEY={ai_key}",
        f"TLCL_AI_MODEL={ai_model}",
        f"TLCL_AI_MODE={ai_mode}",
        "TLCL_AI_TIMEOUT_SECONDS=20",
        "TLCL_POLL_SECONDS=300",
        "TLCL_TOKEN_FILE=~/.tlcl/tokens.json",
        "TLCL_STATE_FILE=~/.tlcl/state.json",
        "TLCL_DRY_RUN=0",
    ]
    path.write_text("\n".join(lines) + "\n")
    os.chmod(path, 0o600)

    out("")
    out("All set — settings saved to .env (only readable by you).")
    out("Next steps:")
    out("  1) tlcl auth       — sign in to Tesla once (opens a link)")
    out("  2) tlcl simulate   — see the rules in action with pretend cars")
    out("  3) tlcl run --dry-run — preview what would happen with your real cars")
    out("  4) tlcl serve      — start the chat server (ask about your rules)")
    return path
