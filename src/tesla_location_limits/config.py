"""Configuration for Tesla Location Charge Limits. All settings come from the
environment (see .env.example). Copy .env.example to .env — .env is
git-ignored so secrets never leave your machine.

TLCL_PLACES is a JSON list of place rules, each:
  {"name": "Home", "address": "123 Main St, Palmdale, CA",
   "lat": 34.57, "lon": -118.11, "radius_m": 150, "limit_pct": 80}
Limits are clamped to 50–100. radius_m defaults to 150 when omitted.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field


class ConfigError(Exception):
    pass


MIN_LIMIT = 50
MAX_LIMIT = 100
DEFAULT_RADIUS_M = 150.0


@dataclass
class VehicleConfig:
    name: str
    vin: str


@dataclass
class PlaceRule:
    name: str
    address: str = ""
    lat: float = 0.0
    lon: float = 0.0
    radius_m: float = DEFAULT_RADIUS_M
    limit_pct: int = 80

    def summary(self) -> str:
        addr = f" ({self.address})" if self.address else ""
        return f"{self.name}{addr}: charge to {self.limit_pct}% within {int(self.radius_m)} m"


@dataclass
class Settings:
    client_id: str = ""
    client_secret: str = ""
    region: str = "na"
    redirect_uri: str = "http://localhost:8080/callback"
    token_file: str = "~/.tlcl/tokens.json"
    state_file: str = "~/.tlcl/state.json"
    vehicles: list[VehicleConfig] = field(default_factory=list)
    places: list[PlaceRule] = field(default_factory=list)
    default_limit_pct: int | None = None   # applied nowhere when None (leave car alone)
    # The OpenAI-compatible server this app exposes.
    server_host: str = "127.0.0.1"
    server_port: int = 8643
    server_api_key: str = ""               # optional bearer token for /v1/*
    model_id: str = "tesla-location-charge-limits"
    # Pluggable LLM backend (any OpenAI-compatible API; default: local Ollama).
    ai_base_url: str = "http://localhost:11434/v1"
    ai_api_key: str = ""
    ai_model: str = "llama3.1"
    ai_mode: str = "auto"                  # auto | always | never
    ai_timeout_seconds: float = 20.0
    poll_seconds: int = 300                # how often `run` re-checks (via cron)
    dry_run: bool = False


def _parse_vehicles(raw: str) -> list[VehicleConfig]:
    """Format: name=VIN,name2=VIN2 (or bare VINs)."""
    vehicles = []
    for idx, item in enumerate(raw.split(",")):
        item = item.strip()
        if not item:
            continue
        name = None
        if "=" in item:
            name, item = item.split("=", 1)
            name = name.strip()
        vin = item.strip()
        if not vin:
            raise ConfigError("Empty VIN in TLCL_VEHICLES")
        vehicles.append(VehicleConfig(name=name or f"car-{idx + 1}", vin=vin))
    if not vehicles:
        raise ConfigError("TLCL_VEHICLES is empty — add at least one name=VIN entry")
    return vehicles


def _clamp_limit(raw, name: str) -> int:
    try:
        pct = int(float(raw))
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"limit for place {name!r} must be a number 50–100, got {raw!r}") from exc
    return max(MIN_LIMIT, min(MAX_LIMIT, pct))


def _parse_places(raw: str) -> list[PlaceRule]:
    if not raw or not raw.strip():
        return []
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ConfigError(f"TLCL_PLACES is not valid JSON: {exc}") from exc
    if not isinstance(data, list):
        raise ConfigError("TLCL_PLACES must be a JSON list of place objects")
    places = []
    for i, item in enumerate(data):
        if not isinstance(item, dict):
            raise ConfigError(f"TLCL_PLACES entry #{i + 1} must be an object")
        name = str(item.get("name", "")).strip()
        if not name:
            raise ConfigError(f"TLCL_PLACES entry #{i + 1} needs a name")
        try:
            lat = float(item.get("lat"))
            lon = float(item.get("lon"))
        except (TypeError, ValueError) as exc:
            raise ConfigError(
                f"place {name!r} needs numeric lat/lon, got lat={item.get('lat')!r} "
                f"lon={item.get('lon')!r}") from exc
        try:
            radius = float(item.get("radius_m", DEFAULT_RADIUS_M))
        except (TypeError, ValueError) as exc:
            raise ConfigError(f"place {name!r}: radius_m must be a number") from exc
        if radius <= 0:
            raise ConfigError(f"place {name!r}: radius_m must be positive")
        limit_pct = _clamp_limit(item.get("limit_pct", 80), name)
        places.append(PlaceRule(name=name,
                                address=str(item.get("address", "")).strip(),
                                lat=lat, lon=lon, radius_m=radius, limit_pct=limit_pct))
    return places


def _int(env: dict, key: str, default: int) -> int:
    raw = env.get(key)
    if raw is None or not str(raw).strip():
        return default
    try:
        return int(raw)
    except (ValueError, TypeError) as exc:
        raise ConfigError(f"{key} must be an integer, got {raw!r}") from exc


def _float(env: dict, key: str, default: float) -> float:
    raw = env.get(key)
    if raw is None or not str(raw).strip():
        return default
    try:
        return float(raw)
    except (ValueError, TypeError) as exc:
        raise ConfigError(f"{key} must be a number, got {raw!r}") from exc


def _bool(env: dict, key: str, default: bool = False) -> bool:
    raw = env.get(key)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def load_settings(env: dict | None = None) -> Settings:
    env = os.environ if env is None else env
    s = Settings()
    s.client_id = env.get("TESLA_CLIENT_ID", "")
    s.client_secret = env.get("TESLA_CLIENT_SECRET", "")
    s.region = env.get("TESLA_REGION", "na").strip().lower()
    if s.region not in ("na", "eu", "cn"):
        raise ConfigError(f"TESLA_REGION must be na|eu|cn, got {s.region!r}")
    s.redirect_uri = env.get("TESLA_REDIRECT_URI", s.redirect_uri)
    s.token_file = env.get("TLCL_TOKEN_FILE", s.token_file)
    s.state_file = env.get("TLCL_STATE_FILE", s.state_file)
    raw_vehicles = env.get("TLCL_VEHICLES", "")
    if raw_vehicles.strip():
        s.vehicles = _parse_vehicles(raw_vehicles)
    s.places = _parse_places(env.get("TLCL_PLACES", ""))
    raw_default = env.get("TLCL_DEFAULT_LIMIT_PCT", "").strip()
    if raw_default:
        s.default_limit_pct = _clamp_limit(raw_default, "<default>")
    s.server_host = env.get("TLCL_SERVER_HOST", s.server_host)
    s.server_port = _int(env, "TLCL_SERVER_PORT", s.server_port)
    s.server_api_key = env.get("TLCL_SERVER_API_KEY", "")
    s.model_id = env.get("TLCL_MODEL_ID", s.model_id)
    s.ai_base_url = env.get("TLCL_AI_BASE_URL", s.ai_base_url).rstrip("/")
    s.ai_api_key = env.get("TLCL_AI_API_KEY", "")
    s.ai_model = env.get("TLCL_AI_MODEL", s.ai_model)
    s.ai_mode = env.get("TLCL_AI_MODE", s.ai_mode).strip().lower()
    if s.ai_mode not in ("auto", "always", "never"):
        raise ConfigError(f"TLCL_AI_MODE must be auto|always|never, got {s.ai_mode!r}")
    s.ai_timeout_seconds = _float(env, "TLCL_AI_TIMEOUT_SECONDS", s.ai_timeout_seconds)
    s.poll_seconds = _int(env, "TLCL_POLL_SECONDS", s.poll_seconds)
    s.dry_run = _bool(env, "TLCL_DRY_RUN")
    return s
