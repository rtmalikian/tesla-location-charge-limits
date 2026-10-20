# Tesla Location Charge Limits — Geofenced Per-Place Charge Limits for Model 3/Y/S/X (Rivian-Style)

**Free, open-source app that gives your Teslas per-location charge limits (home 80%, cabin 90%, road-trip charger 100%) — the feature Rivian ships and Tesla never has — set automatically on arrival through the official Tesla Fleet API, with an OpenAI-compatible chat surface to ask about and edit the rules in plain language.**

▶ **[Try the live demo](https://rtmalikian.github.io/tesla-location-charge-limits/)** — drag a pretend car between Home, Office and Cabin in your browser and watch the rules fire (DEMO DATA, no real car).

Works with Model 3, Model Y, Model S, Model X, and Cybertruck. Part of a Tesla AI app series: every app embeds OpenAI-compatible endpoints with a pluggable LLM backend and a deterministic offline rules engine.

## Why?

- **Rivian has per-location charge limits. Tesla has ONE global limit.** Every Tesla owner knows the dance: crank to 100% before a road trip, forget to turn it back down, and spend weeks at 100% — the one thing every battery-health guide says to avoid.
- **"Why can't my car just know it's home?"** — owners type this into forums constantly. Now it can.
- **Never fights you on a trip:** if you raised the limit yourself for a road trip, the app leaves it alone (trip-mode override). It only ever enforces, never cuts.
- **Exact, not nagging:** the limit is set exactly once per arrival. No repeat commands, no spam.
- **Ask it like a person:** "what's the limit at the cabin?" or "set the cabin to 90" — answered by a rules engine, with optional local AI for free-form questions.

## How it works

Every few minutes (your schedule, your poll interval), the app takes a no-wake read of each car's location and current charge limit. When a car **arrives** at one of your places (within its geofence radius), the place's limit is applied — exactly once per visit. The moment it leaves, the applied flag resets.

```
Timeline (from `tlcl simulate` — pretend fleet, real rules):

  8:00 AM   Model Y asleep at Home ── skipped (no-wake reads only)
★ 8:30 AM   Model Y arrives Home ──── set_charge_limit → 80%
  9:00 AM   Model Y still at Home ─── nothing (already applied this visit)
  9:30 AM   Model Y leaves ────────── flag reset
★ 10:00 AM  Model Y arrives Cabin ─── set_charge_limit → 90%
  12:30 PM  Model Y re-arrives Cabin with a manual 100% ── LEFT ALONE
             (trip-mode override: never cuts your trip charge)
★ 3:30 PM   Model 3 re-arrives Office at 70% ── raised to 80% (rule wins)

Legend: ★ sent a charge-limit command
```

**Expected API cost:** ~$1.15/day (~$35/month, 2 cars) at the default 5-minute poll — ~576 no-wake data reads + ~3 commands + 0 wake-ups at community-reported rates ($1/500 data calls, $1/1,000 commands). Wakes are zero *by design* (asleep cars are skipped, never woken). Stretch `TLCL_POLL_SECONDS` to 900 (15 min) for roughly a third of that. The simulator prints this estimate for your settings.

## Quickstart

```bash
git clone https://github.com/rtmalikian/tesla-location-charge-limits.git
cd tesla-location-charge-limits
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
cp .env.example .env
.venv/bin/python -m tesla_location_limits.cli setup   # plain-language wizard, writes .env for you
.venv/bin/python -m tesla_location_limits.cli auth     # one-time Tesla sign-in
.venv/bin/python -m tesla_location_limits.cli simulate # see the rules on a pretend fleet
.venv/bin/python -m tesla_location_limits.cli run --dry-run  # preview with your real cars
```

Then run `tlcl run` on a schedule (cron, every 5–15 minutes) and `tlcl serve` for the chat API. Install the CLI once: `pip install -e .` gives you the `tlcl` command.

### Setup wizard (no developers required)

`tlcl setup` asks five rounds of plain-language questions and writes `.env` itself:

1. **Connect to Tesla** — paste your Client ID/Secret from developer.tesla.com.
2. **Your cars** — a nickname + VIN for each (find the VIN in the Tesla app under Controls → Software).
3. **Your places** — a name and a *street address* for each spot. Addresses are geocoded automatically; if the lookup fails you can retry or paste coordinates from Google Maps.
4. **The AI brain (optional)** — built-in answers only, local Ollama, or another AI service.
5. **The chat server** — port + optional password.

### Daily commands

| Command | What it does |
|---|---|
| `tlcl setup` | Guided setup wizard (writes `.env`) |
| `tlcl auth` | One-time Tesla account authorization |
| `tlcl run [--dry-run]` | One enforcement cycle; first real send asks for confirmation |
| `tlcl serve` | Start the OpenAI-compatible chat server (default port 8643) |
| `tlcl ask "…"` | One-shot Q&A — always a dry run, nothing saved |
| `tlcl status` | Each car's current limit vs its place rule |
| `tlcl places add/list/remove` | Manage place rules (address is geocoded for you) |
| `tlcl simulate` | Demo timeline + chat against a pretend fleet |

### Tesla developer setup

1. Create an app at [developer.tesla.com](https://developer.tesla.com) and copy the Client ID/Secret into the wizard.
2. Scopes needed: `vehicle_device_data`, `vehicle_location`, `vehicle_cmds`.
3. Run `tlcl auth`, open the printed URL, approve, paste back the code.
4. Pair a virtual key for `set_charge_limit` commands: https://developer.tesla.com/docs/fleet-api#pairing

## AI endpoints

The app exposes an OpenAI-compatible surface (rules engine first — works with no LLM at all; optional LLM backend for free-form questions via `TLCL_AI_*` env vars, default local Ollama). **Vehicle data goes only to your configured backend — nowhere else.**

```bash
# What model is this?
curl http://127.0.0.1:8643/v1/models

# Ask about the rules
curl http://127.0.0.1:8643/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{"model":"tesla-location-charge-limits",
       "messages":[{"role":"user","content":"what'"'"'s the limit at the cabin?"}]}'

# Change a rule in plain language
curl http://127.0.0.1:8643/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{"model":"tesla-location-charge-limits",
       "messages":[{"role":"user","content":"set the cabin to 90"}]}'
```

Chat understands questions ("what's the limit at the cabin?", "which places have rules?", "why is my limit 80% right now?") and natural-language rule edits ("set the cabin to 90", "home should be 80 percent"). If an LLM backend is enabled, the model may emit an allowlisted JSON command block (`{action: "set_place_limit", place, limit_pct}`), validated before execution. Streaming is not supported (`stream` is ignored).

There is deliberately **no `/v1/embeddings`**: this app has no semantic-search or vector use case, so we don't ship surface for its own sake.

## `.env` reference

| Variable | Default | Meaning |
|---|---|---|
| `TLCL_VEHICLES` | — | `name=VIN` pairs, comma-separated |
| `TLCL_PLACES` | — | JSON list of `{name, address, lat, lon, radius_m, limit_pct}` — written by `tlcl setup`/`places add`, never hand-edited |
| `TLCL_DEFAULT_LIMIT_PCT` | blank | Limit to restore when a car leaves a ruled place; blank = leave alone |
| `TLCL_POLL_SECONDS` | `300` | Seconds between `run` checks (via your scheduler) |
| `TLCL_STATE_FILE` | `~/.tlcl/state.json` | Exactly-once bookkeeping (survives restarts) |
| `TLCL_SERVER_HOST/PORT` | `127.0.0.1` / `8643` | Chat server bind |
| `TLCL_SERVER_API_KEY` | blank | Optional `Authorization: Bearer` on `/v1/*` |
| `TLCL_AI_BASE_URL` | `http://localhost:11434/v1` | Pluggable LLM backend (Ollama default) |
| `TLCL_AI_API_KEY` / `TLCL_AI_MODEL` | blank / `llama3.1` | Backend credentials |
| `TLCL_AI_MODE` | `auto` | `auto` = rules first, LLM for the rest · `always` · `never` |
| `TLCL_DRY_RUN` | `0` | `1` = report commands instead of sending them |
| `TLCL_TOKEN_FILE` | `~/.tlcl/tokens.json` | OAuth tokens (never committed) |

## FAQ

**Does this wake my car?** No. Asleep cars are skipped — the app only reads, never wakes. Wakes are the priciest API call, so the design treats "asleep" as "check back later."

**What if I set a higher limit myself for a trip?** It stays. If your limit is higher than the place rule when you arrive, the app assumes you raised it for a reason and never lowers it (trip-mode override), and it tells you so in plain English.

**What if I set a lower limit manually?** The place rule wins — it's raised back to the place limit on arrival. The rule is a floor for battery health, not a ceiling on your plans.

**Will it keep sending the command every poll?** No — exactly once per arrival. A second visit after leaving re-applies the rule, but parking in place never re-sends.

**What happens when the car leaves a place?** Nothing, unless you set `TLCL_DEFAULT_LIMIT_PCT` — then that limit is restored on departure.

**Do I need AI for this to work?** No. The rules engine handles every question and edit above; the LLM is only for free-form phrasing. `TLCL_AI_MODE=never` runs it fully offline.

**How accurate is the geofence?** Radius is configurable per place (default 150 m, roughly a block). The boundary is inclusive; overlapping places resolve to the innermost one.

## License

MIT — Raphael Malikian. See [LICENSE](LICENSE).
