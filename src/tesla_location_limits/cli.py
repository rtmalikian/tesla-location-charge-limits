"""CLI: tlcl setup | auth | serve | ask | run | status | simulate | places"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys

from .config import ConfigError, PlaceRule, load_settings

__version__ = "0.1.0"


def _setup_logging(verbose: bool) -> None:
    logging.basicConfig(level=logging.DEBUG if verbose else logging.INFO,
                        format="%(levelname)s %(name)s: %(message)s")


def _client(settings):
    from .tesla_client import TeslaClient
    return TeslaClient(settings.client_id, settings.client_secret,
                       region=settings.region, redirect_uri=settings.redirect_uri,
                       token_file=settings.token_file)


def _env_path(args) -> str:
    if getattr(args, "env_file", None):
        return args.env_file
    if os.path.exists(".env"):
        return ".env"
    return ".env"


def cmd_auth(args, settings) -> int:
    from .tesla_client import TeslaClient
    client = _client(settings)
    print("Open this URL in your browser and authorize:")
    print(client.authorize_url())
    print()
    print("After authorizing, paste the 'code' from the redirected URL into")
    print("`tlcl` when it asks. Pairing a virtual key (needed for commands like")
    print("set_charge_limit): https://developer.tesla.com/docs/fleet-api#pairing")
    return 0


def cmd_setup(args, settings) -> int:
    from .setup import run_setup
    run_setup(env_path=_env_path(args))
    return 0


def cmd_serve(args, settings) -> int:
    from .server import serve
    from .service import LimitService

    def client_factory():
        return _client(settings)

    service = LimitService(settings, client_factory=client_factory,
                           env_path=_env_path(args))
    print("Vehicle data goes ONLY to your configured LLM backend "
          f"(TLCL_AI_BASE_URL={settings.ai_base_url or 'none'}); "
          "rules engine works with no backend at all.")
    serve(service)
    return 0


def cmd_ask(args, settings) -> int:
    """One-shot Q&A — always a dry run: answers only, nothing is saved or sent."""
    from .service import LimitService

    def client_factory():
        try:
            return _client(settings)
        except Exception:
            return None

    service = LimitService(settings, client_factory=client_factory,
                           llm_factory=lambda: None, preview_only=True)
    print(service.answer(args.question))
    print()
    print("(Dry run: nothing was saved and no car was touched.)")
    return 0


def cmd_run(args, settings) -> int:
    """One enforcement cycle: preview first, confirm before the first real send."""
    from .service import LimitService
    from .rules import LimitState

    if not settings.vehicles:
        print("TLCL_VEHICLES is not set — run `tlcl setup` first.", file=sys.stderr)
        return 2
    if not settings.places:
        print("No places configured yet — add one with `tlcl places add`.")
        return 0

    dry_run = args.dry_run or settings.dry_run
    client = _client(settings)
    service = LimitService(settings, client_factory=lambda: client)
    state = LimitState(settings.state_file)

    print("Checking your cars against their place rules…")
    try:
        decisions, commands = service.run_cycle(state=state, dry_run=dry_run)
    except Exception as exc:
        print(f"Couldn't check your cars just now: {exc}", file=sys.stderr)
        print("If this mentions tokens, run `tlcl auth` first.", file=sys.stderr)
        return 1

    for d in decisions:
        print(f"  • {d.reason}")
    if not commands:
        print("Nothing to change right now — every car is where it should be.")
        return 0

    print()
    print("Commands to send:")
    for c in commands:
        print(f"  {c}")
    if dry_run:
        print()
        print("Dry run — nothing was sent to any car.")
        return 0

    if not state.meta.get("first_run_confirmed") and not args.yes:
        answer = input("Send these commands to your real cars? (y/n) > ").strip().lower()
        if answer not in ("y", "yes"):
            print("OK — nothing was sent. Re-run any time.")
            return 0
        state.meta["first_run_confirmed"] = True
        state.save()
        print("Confirmed — you won't be asked again for future runs.")
    print("Sent.")
    return 0


def cmd_status(args, settings) -> int:
    from .fleet import snapshot_fleet
    from .rules import match_place
    client = _client(settings)
    snap = snapshot_fleet(client, settings.vehicles)
    for r in snap.readings:
        state = "asleep" if r.asleep else "awake"
        limit = f"{r.charge_limit_pct}%" if r.charge_limit_pct is not None else "unknown"
        if r.lat is not None and r.lon is not None:
            place = match_place(settings.places, r.lat, r.lon)
            where = f"at {place.name} (rule: {place.limit_pct}%)" if place else "not at a ruled place"
        else:
            where = "location unknown"
        print(f"{r.name}: charge limit {limit}, {state}, {where}")
    for vin, err in snap.unreachable.items():
        print(f"{vin}: couldn't reach it — {err}")
    return 0


def cmd_simulate(args, settings) -> int:
    from .simulator import run_demo
    for line in run_demo(settings):
        print(line)
    return 0


def _write_places(env_file: str, places: list[PlaceRule]) -> None:
    data = [{"name": p.name, "address": p.address, "lat": p.lat, "lon": p.lon,
             "radius_m": p.radius_m, "limit_pct": p.limit_pct} for p in places]
    value = json.dumps(data, separators=(",", ":"))
    path = env_file
    if not os.path.exists(path):
        print(f"No {path} found — run `tlcl setup` first.", file=sys.stderr)
        raise SystemExit(2)
    lines = []
    seen = False
    with open(path) as fh:
        for line in fh.read().splitlines():
            if line.startswith("TLCL_PLACES="):
                lines.append(f"TLCL_PLACES={value}")
                seen = True
            else:
                lines.append(line)
    if not seen:
        lines.append(f"TLCL_PLACES={value}")
    with open(path, "w") as fh:
        fh.write("\n".join(lines) + "\n")
    os.chmod(path, 0o600)


def cmd_places(args, settings) -> int:
    from .fleet import find_place
    from .setup import ask_place

    env_file = _env_path(args)
    if args.places_cmd == "list":
        if not settings.places:
            print("No places yet — add one with `tlcl places add`.")
            return 0
        print("Place rules:")
        for p in settings.places:
            print(f"  • {p.summary()}")
        return 0
    if args.places_cmd == "add":
        place = ask_place(input, print)
        if place is None:
            print("No place added.")
            return 0
        settings.places.append(PlaceRule(**place))
        _write_places(env_file, settings.places)
        print(f"Saved. `{place['name']}` is now in your .env.")
        return 0
    if args.places_cmd == "remove":
        target = find_place(settings.places, args.name or "")
        if target is None:
            names = ", ".join(p.name for p in settings.places) or "none"
            print(f"No place named \"{args.name}\" (my places: {names}).")
            return 1
        keep = [p for p in settings.places if p is not target]
        _write_places(env_file, keep)
        print(f"Removed {target.name}.")
        return 0
    return 1


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="tlcl",
                                description="Geofenced charge limits for your Teslas.")
    p.add_argument("--version", action="version", version=f"tlcl {__version__}")
    p.add_argument("--env-file", default=None)
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="command", required=True)
    sub.add_parser("setup", help="Guided first-run setup (plain-language questions)")
    sub.add_parser("auth", help="Authorize with your Tesla account (one-time)")
    sub.add_parser("serve", help="Run the OpenAI-compatible API server")
    ask_p = sub.add_parser("ask", help="One-shot question (dry run — nothing saved)")
    ask_p.add_argument("question", help="What to ask, e.g. \"what's the limit at the cabin?\"")
    run_p = sub.add_parser("run", help="One enforcement cycle over the fleet")
    run_p.add_argument("--dry-run", action="store_true",
                       help="Preview only — report commands instead of sending them")
    run_p.add_argument("--yes", "-y", action="store_true",
                       help="Skip the first-run confirmation prompt")
    sub.add_parser("status", help="Show each car's current limit vs its place rule")
    sub.add_parser("simulate", help="Demo the rules against a pretend fleet")
    places_p = sub.add_parser("places", help="Manage place rules")
    psub = places_p.add_subparsers(dest="places_cmd", required=True)
    psub.add_parser("add", help="Add a place (name + address, geocoded)")
    psub.add_parser("list", help="List place rules")
    rm_p = psub.add_parser("remove", help="Remove a place rule")
    rm_p.add_argument("name", help="Place name to remove")
    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    _setup_logging(args.verbose)
    env = dict(os.environ)
    if args.env_file:
        from dotenv import load_dotenv
        load_dotenv(args.env_file, override=True)
        env = dict(os.environ)
    else:
        from dotenv import load_dotenv
        load_dotenv(override=False)
        env = dict(os.environ)
    try:
        settings = load_settings(env)
    except ConfigError as exc:
        print(f"Config error: {exc}", file=sys.stderr)
        return 2
    handlers = {"setup": cmd_setup, "auth": cmd_auth, "serve": cmd_serve,
                "ask": cmd_ask, "run": cmd_run, "status": cmd_status,
                "simulate": cmd_simulate, "places": cmd_places}
    try:
        return handlers[args.command](args, settings)
    except SystemExit as exc:
        return int(exc.code or 0)
    except BrokenPipeError:
        return 0
    except KeyboardInterrupt:
        print("\nCancelled.")
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
