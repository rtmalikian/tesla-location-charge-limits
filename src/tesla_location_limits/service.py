"""Transport-independent charge-limit service: takes OpenAI-shaped request
dicts, returns OpenAI-shaped response dicts. The HTTP server (server.py) is
a thin wrapper; tests drive this class directly.

Chat flow for a user message:
  1. Rules engine answers place/rule questions directly (no LLM needed):
     "what's the limit at the cabin?", "which places have rules?",
     "why is my limit 80% right now?"
  2. Natural-language rule edits ("set the cabin to 90") are parsed
     rules-first, then via the optional LLM backend emitting an
     allowlisted JSON command block {action: set_place_limit, ...}.
     Commands are validated (place must exist, limit clamped 50–100)
     before the rule is updated.
  3. ai_mode=never (or LLM failure) → capability-list answer.

run_cycle() performs one enforcement pass over the fleet: no-wake reads,
rules.decide() per vehicle, exactly-once set_charge_limit commands.
"""
from __future__ import annotations

import itertools
import os
import re
import time
from pathlib import Path

from .config import MIN_LIMIT, MAX_LIMIT
from .fleet import find_place, find_vehicle, snapshot_fleet
from .llm import LlmBackend, extract_rule_command
from .rules import Decision, LimitState, decide, match_place

CAPABILITY_BLURB = (
    "I manage per-place charge limits for your Teslas — Rivian-style "
    "geofenced limits Tesla never shipped. I can tell you which places have "
    "rules, what the limit is at a place, and why a car's limit is what it is "
    "right now — and I can change rules in plain language. "
    "Try: \"what's the limit at the cabin?\" or \"set the cabin to 90\". "
    "Connect an LLM backend (TLCL_AI_BASE_URL) and I can handle free-form "
    "questions too. I never touch a real car — rules are enforced by "
    "`tlcl run` on a schedule."
)

# Rules-first patterns (checked before the LLM). Order matters: edits first.
_EDIT_PATTERNS = [
    # "set the cabin to 90", "change home limit to 80%", "set cabin at 75 percent"
    re.compile(r"(?:set|change|make)\s+(?:the\s+)?(.+?)\s+(?:to|at)\s+(\d{1,3})\s*(?:percent|%|pct)?\s*$"),
    # "home should be 80 percent", "cabin: 90"
    re.compile(r"(.+?)\s+should\s+be\s+(\d{1,3})\s*(?:percent|%|pct)?\s*$"),
]

_QUESTION_PATTERNS = {
    "list": re.compile(r"(which|what|list|show)\s+(places|locations|rules)"),
    "limit_at": re.compile(r"(?:limit|charge).{0,20}(?:at|for)\s+(?:the\s+)?(.+?)[?!.]?\s*$"),
    "why": re.compile(r"\bwhy\b.{0,60}\b(limit|80|90|100|charge)\b"),
}


class LimitService:
    def __init__(self, settings, client_factory=None, llm_factory=None,
                 env_path: str | None = None, preview_only: bool = False):
        self.settings = settings
        self.client_factory = client_factory or (lambda: None)
        self.llm_factory = llm_factory
        self.env_path = Path(env_path) if env_path else None
        self.preview_only = preview_only
        self._ids = itertools.count(1)

    # -- plumbing ------------------------------------------------------
    def _client(self):
        return self.client_factory()

    def _llm(self):
        if self.llm_factory is not None:
            return self.llm_factory()
        if self.settings.ai_mode == "never":
            return None
        if not self.settings.ai_base_url:
            return None
        return LlmBackend(self.settings.ai_base_url, self.settings.ai_api_key,
                          self.settings.ai_model, self.settings.ai_timeout_seconds)

    def _next_id(self, prefix: str) -> str:
        return f"{prefix}-{int(time.time())}-{next(self._ids)}"

    # -- OpenAI surface ------------------------------------------------
    def models_list(self) -> dict:
        return {"object": "list", "data": [{
            "id": self.settings.model_id,
            "object": "model",
            "created": int(time.time()),
            "owned_by": "tesla-location-charge-limits",
        }]}

    def chat_completions(self, body: dict) -> dict:
        messages = body.get("messages") or []
        user_text = ""
        for m in reversed(messages):
            if m.get("role") == "user":
                user_text = str(m.get("content") or "")
                break
        text = self.answer(user_text)
        return {
            "id": self._next_id("chatcmpl"),
            "object": "chat.completion",
            "created": int(time.time()),
            "model": self.settings.model_id,
            "choices": [{
                "index": 0,
                "message": {"role": "assistant", "content": text},
                "finish_reason": "stop",
            }],
            "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
        }

    # -- rules-first chat -----------------------------------------------
    def places_brief(self) -> str:
        if not self.settings.places:
            return "(no places configured yet — add them with `tlcl places add`)"
        return "\n".join(f"- {p.summary()}" for p in self.settings.places)

    def answer(self, user_text: str) -> str:
        """Answer a chat message using rules first, LLM as fallback."""
        text = user_text.strip()
        if not text:
            return CAPABILITY_BLURB
        lowered = text.lower()

        # 1) natural-language rule edits
        for pat in _EDIT_PATTERNS:
            m = pat.search(lowered)
            if m:
                return self._apply_rule_edit(m.group(1), m.group(2))

        # 2) questions
        if _QUESTION_PATTERNS["list"].search(lowered):
            return "Here's where I set charge limits:\n" + self.places_brief()
        m = _QUESTION_PATTERNS["limit_at"].search(lowered)
        if m:
            return self._answer_limit_at(m.group(1))
        if _QUESTION_PATTERNS["why"].search(lowered):
            return self._answer_why()

        # 3) LLM fallback
        return self._llm_answer(user_text)

    def _answer_limit_at(self, mention: str) -> str:
        place = find_place(self.settings.places, mention.strip())
        if place is None:
            names = ", ".join(p.name for p in self.settings.places) or "none yet"
            return (f"I don't have a place called \"{mention.strip()}\" yet. "
                    f"My places: {names}. Add it with `tlcl places add`.")
        line = f"The {place.name} rule charges to {place.limit_pct}%."
        # Add live context if we can reach the fleet.
        client = self._client()
        if client is not None:
            try:
                snap = snapshot_fleet(client, self.settings.vehicles)
            except Exception:
                snap = None
            if snap is not None:
                there = [r.name for r in snap.readings
                         if r.lat is not None and r.lon is not None
                         and match_place([place], r.lat, r.lon) is not None]
                if there:
                    line += f" Right now, {', '.join(there)} {'is' if len(there) == 1 else 'are'} there."
        return line

    def _answer_why(self) -> str:
        client = self._client()
        if client is None:
            return CAPABILITY_BLURB
        try:
            snap = snapshot_fleet(client, self.settings.vehicles)
        except Exception:
            return CAPABILITY_BLURB
        if not snap.readings:
            return "I couldn't reach any car just now — check `tlcl status` and try again."
        lines = []
        for r in snap.readings:
            if r.lat is None or r.lon is None:
                lines.append(f"{r.name}: no location in the last read.")
                continue
            place = match_place(self.settings.places, r.lat, r.lon)
            limit = f"{r.charge_limit_pct}%" if r.charge_limit_pct is not None else "unknown"
            if place:
                lines.append(f"{r.name} is at {place.name}, so the {place.limit_pct}% "
                             f"rule applies — its limit reads {limit}.")
            else:
                lines.append(f"{r.name} isn't at a ruled place, so no rule applies — "
                             f"its limit reads {limit} (whatever it was last set to).")
        return "\n".join(lines)

    def _apply_rule_edit(self, place_mention: str, limit_raw: str) -> str:
        place = find_place(self.settings.places, place_mention.strip())
        if place is None:
            names = ", ".join(p.name for p in self.settings.places) or "none yet"
            return (f"I don't have a place called \"{place_mention.strip()}\" yet "
                    f"(my places: {names}). Add it first with `tlcl places add`.")
        try:
            limit = int(float(limit_raw))
        except (TypeError, ValueError):
            return f"\"{limit_raw}\" isn't a number — say the limit as a percent, like \"{place.name} to 90\"."
        clamped = max(MIN_LIMIT, min(MAX_LIMIT, limit))
        old = place.limit_pct
        if self.preview_only:
            note = (" (preview only — not saved; re-run without preview to apply)")
            if clamped != limit:
                return (f"I'd set {place.name} to {clamped}% (Tesla only allows 50–100%, "
                        f"so {limit}% would be clamped). Currently {old}%.{note}")
            return f"I'd set {place.name} to {clamped}% (currently {old}%).{note}"
        place.limit_pct = clamped
        self._persist_places()
        if clamped != limit:
            return (f"Set {place.name} to {clamped}% (Tesla only allows 50–100%, "
                    f"so {limit}% was clamped). Was {old}%.")
        return f"Set {place.name} to {clamped}% (was {old}%)."

    def _persist_places(self) -> None:
        """Write the (possibly edited) place list back to the .env file."""
        if self.env_path is None or not self.env_path.exists():
            return
        import json
        data = [{"name": p.name, "address": p.address, "lat": p.lat, "lon": p.lon,
                 "radius_m": p.radius_m, "limit_pct": p.limit_pct}
                for p in self.settings.places]
        value = json.dumps(data, separators=(",", ":"))
        lines = []
        seen = False
        for line in self.env_path.read_text().splitlines():
            if line.startswith("TLCL_PLACES="):
                lines.append(f"TLCL_PLACES={value}")
                seen = True
            else:
                lines.append(line)
        if not seen:
            lines.append(f"TLCL_PLACES={value}")
        self.env_path.write_text("\n".join(lines) + "\n")

    # -- LLM fallback ----------------------------------------------------
    def _llm_answer(self, user_text: str) -> str:
        llm = self._llm()
        if llm is None:
            return CAPABILITY_BLURB
        try:
            reply = llm.chat(self.places_brief(), user_text)
        except Exception:
            return CAPABILITY_BLURB + " (The AI backend didn't respond just now.)"
        cmd = extract_rule_command(reply)
        if cmd is None:
            return reply
        _action, place_name, limit_pct = cmd
        return self._apply_rule_edit(place_name, str(limit_pct))

    # -- enforcement cycle ----------------------------------------------
    def run_cycle(self, state: LimitState | None = None,
                  now: float | None = None,
                  dry_run: bool | None = None) -> tuple[list[Decision], list[str]]:
        """One enforcement pass: no-wake reads → decide → command (unless
        dry-run). Returns (decisions, commands_actually_sent)."""
        client = self._client()
        if client is None:
            raise RuntimeError("No Tesla client — run `tlcl auth` first.")
        dry_run = self.settings.dry_run if dry_run is None else dry_run
        state = state or LimitState(self.settings.state_file)
        snap = snapshot_fleet(client, self.settings.vehicles)
        decisions: list[Decision] = []
        commands: list[str] = []
        for reading in snap.readings:
            decision = decide(reading, self.settings.places, state, now=now,
                              default_limit_pct=self.settings.default_limit_pct)
            decisions.append(decision)
            if decision.action == "set_limit":
                if dry_run:
                    commands.append(f"[dry-run] {decision.command_summary()}")
                else:
                    client.set_charge_limit(decision.vin, decision.limit_pct)
                    commands.append(decision.command_summary())
        state.save()
        return decisions, commands
