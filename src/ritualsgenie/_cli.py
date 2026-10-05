"""Entry point for the optional CLI, with an install hint if it's missing."""

from __future__ import annotations

_CLI_EXTRA_MODULES = frozenset({"typer", "rich"})


def main() -> None:
    """Invoke the Typer CLI with a graceful error if extras are missing."""
    try:
        # Deferred, so this works without the optional cli extra installed.
        # pylint: disable-next=import-outside-toplevel
        from ritualsgenie.cli import cli  # noqa: PLC0415
    except ModuleNotFoundError as err:  # pragma: no cover
        # Only a missing optional dependency gets the install hint; anything
        # else is a real error.
        missing_module: str = err.name or ""
        missing_root = missing_module.split(".", 1)[0]  # pylint: disable=no-member
        if missing_root not in _CLI_EXTRA_MODULES:
            raise
        msg = (
            "The Rituals Perfume Genie CLI requires the 'cli' extra. "
            "Install it with: pip install 'ritualsgenie[cli]'"
        )
        raise SystemExit(msg) from err
    cli()
