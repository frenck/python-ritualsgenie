"""Helpers for the Rituals Perfume Genie CLI."""

from __future__ import annotations

import hashlib
import json
import os
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Any, NoReturn

import typer
from rich.console import Console
from rich.markup import escape
from rich.panel import Panel

from ritualsgenie.ritualsgenie import RitualsGenie

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from ritualsgenie.models import RitualsGenieHub

# Errors and notices on stderr, so JSON output on stdout can be piped.
console = Console()
err_console = Console(stderr=True)

REDACTED = "**REDACTED**"

# What identifies the device or its owner. Room names tend to hold names.
REDACTED_HUB_FIELDS = ("hash", "hublot", "pin")
REDACTED_ATTRIBUTE_VALUES = ("roomnamec", "fspacenamec")


def error_panel(message: str, title: str) -> NoReturn:
    """Print an error panel and exit."""
    err_console.print(
        Panel(escape(message), expand=False, title=title, border_style="red bold")
    )
    sys.exit(1)


def fail(message: str) -> NoReturn:
    """Print an error message and exit."""
    err_console.print(f"[red]{escape(message)}[/red]")
    raise typer.Exit(code=1)


def emit_json(payload: object) -> None:
    """Emit a payload as indented JSON on stdout."""
    typer.echo(json.dumps(payload, indent=2, default=str))


# --- Reusing the token between commands ---


def token_cache_path(email: str) -> Path | None:
    """Return where the token of an account is cached, None if disabled.

    Without it, every command logs in, and a dozen logins lock the account out.
    """
    if os.environ.get("RITUALS_NO_TOKEN_CACHE"):
        return None

    cache_home = os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache"
    account = hashlib.sha256(email.casefold().encode()).hexdigest()[:16]

    return Path(cache_home) / "ritualsgenie" / f"token-{account}"


def read_cached_token(path: Path) -> str | None:
    """Read a cached token, if there is one."""
    try:
        return path.read_text(encoding="utf-8").strip() or None
    except OSError:
        return None


def write_cached_token(path: Path, token: str) -> None:
    """Cache a token, readable by the current user only; failing is fine."""
    try:
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)

        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as cache:
            cache.write(token)

    except OSError:
        pass


@asynccontextmanager
async def client(email: str, password: str) -> AsyncIterator[RitualsGenie]:
    """Create a client that reuses the cached token of the account."""
    cache = token_cache_path(email)
    cached_token = read_cached_token(cache) if cache else None

    async with RitualsGenie(
        email=email, password=password, token=cached_token
    ) as genie:
        try:
            yield genie
        finally:
            # Also when the command failed, the token is still good.
            if cache and isinstance(genie.token, str) and genie.token != cached_token:
                write_cached_token(cache, genie.token)


# --- Finding a diffuser ---


def find_hub(hubs: list[RitualsGenieHub], identifier: str) -> RitualsGenieHub:
    """Find a diffuser by hash or hublot, or else by its (unique) name."""
    for hub in hubs:
        if identifier in {hub.hash, hub.hublot}:
            return hub

    named = [
        hub
        for hub in hubs
        if hub.name is not None and hub.name.casefold() == identifier.casefold()
    ]

    if len(named) == 1:
        return named[0]

    if named:
        hublots = ", ".join(hub.hublot for hub in named)
        fail(
            f"Multiple diffusers are named {identifier}, "
            f"use the hublot instead: {hublots}"
        )

    fail(f"No diffuser found matching {identifier}.")


def redact_hub(hub: dict[str, Any]) -> dict[str, Any]:
    """Redact identifying information from a raw hub, for sharing a dump."""
    redacted = {**hub}

    for key in REDACTED_HUB_FIELDS:
        if redacted.get(key) is not None:
            redacted[key] = REDACTED

    if isinstance(attribute_values := redacted.get("attributeValues"), dict):
        redacted["attributeValues"] = {
            key: REDACTED if key in REDACTED_ATTRIBUTE_VALUES else value
            for key, value in attribute_values.items()
        }

    return redacted


# --- Formatting ---


def display_text(value: object) -> str:
    """Return API provided text, safe to print (names may contain brackets)."""
    return escape(str(value))


def display_name(hub: RitualsGenieHub) -> str:
    """Return the name of a diffuser to show, safe to print."""
    return display_text(hub.name or hub.hublot)


def display_yes_no(value: bool | None) -> str:
    """Return a human friendly representation of an optional boolean."""
    if value is None:
        return "[dim]unknown[/dim]"

    return "[green]yes[/green]" if value else "[red]no[/red]"


def display_room_size(hub: RitualsGenieHub) -> str:
    """Return the room size, preferring the square meters the app shows."""
    if hub.room_square_meters is not None:
        return f"{hub.room_square_meters} m²"

    if hub.room_size is not None:
        return f"up to {hub.room_size.square_meters} m²"

    return "-"


def display_firmware(hub: RitualsGenieHub) -> str:
    """Return the firmware version, mentioning an available update."""
    if hub.firmware is None or hub.firmware.current is None:
        return "-"

    version = display_text(hub.firmware.current)

    if hub.firmware.update_available:
        version += f" [yellow](update: {display_text(hub.firmware.newest)})[/yellow]"

    return version
