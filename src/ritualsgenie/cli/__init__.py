"""Command-line interface for the Rituals Perfume Genie API."""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Any

import typer
from rich.table import Table

from ritualsgenie.const import Sensor
from ritualsgenie.exceptions import (
    RitualsGenieAuthenticationError,
    RitualsGenieConnectionError,
    RitualsGenieError,
    RitualsGenieRateLimitError,
    RitualsGenieResponseError,
)

from .async_typer import AsyncTyper
from .helpers import (
    client,
    console,
    display_firmware,
    display_name,
    display_room_size,
    display_text,
    display_yes_no,
    emit_json,
    err_console,
    error_panel,
    fail,
    find_hub,
    redact_hub,
)

cli = AsyncTyper(
    help="Rituals Perfume Genie CLI",
    no_args_is_help=True,
    add_completion=False,
)


class OnOff(StrEnum):
    """On or off, as a CLI argument."""

    ON = "on"
    OFF = "off"


Email = Annotated[
    str,
    typer.Option(
        help="Email address of your Rituals account",
        prompt="Email address",
        show_default=False,
        envvar="RITUALS_EMAIL",
    ),
]
Password = Annotated[
    str,
    typer.Option(
        help="Password of your Rituals account",
        prompt="Password",
        hide_input=True,
        show_default=False,
        envvar="RITUALS_PASSWORD",
    ),
]
Hub = Annotated[
    str,
    typer.Argument(
        help="Name, hublot or hash of the diffuser",
        show_default=False,
    ),
]
JsonFlag = Annotated[
    bool,
    typer.Option(
        "--json",
        help="Emit machine-readable JSON output",
    ),
]
SensorsFlag = Annotated[
    bool,
    typer.Option(
        "--sensors",
        help="Include raw sensor responses (one request per sensor!)",
    ),
]


@cli.error_handler(RitualsGenieAuthenticationError)
def authentication_error_handler(error: RitualsGenieAuthenticationError) -> None:
    """Handle authentication errors."""
    error_panel(
        f"{error}\n\nPlease check your email address and password. Note: Rituals\n"
        "asked everyone to reset their password in 2025.",
        "Authentication error",
    )


@cli.error_handler(RitualsGenieRateLimitError)
def rate_limit_error_handler(error: RitualsGenieRateLimitError) -> None:
    """Handle rate limit errors."""
    wait = "a while"
    if error.retry_after is not None:
        wait = f"{error.retry_after // 60 + 1} minutes"

    error_panel(
        "The Rituals API rate limit has been hit, which also happens after\n"
        f"too many logins in a short time. Please wait {wait} and try again.",
        "Rate limited",
    )


@cli.error_handler(RitualsGenieConnectionError)
def connection_error_handler(_: RitualsGenieConnectionError) -> None:
    """Handle connection errors."""
    error_panel(
        "Could not connect to the Rituals Perfume Genie API. Please check\n"
        "your internet connection and try again.",
        "Connection error",
    )


@cli.error_handler(RitualsGenieResponseError)
def response_error_handler(error: RitualsGenieResponseError) -> None:
    """Handle errors the API responded with."""
    error_panel(str(error), "Rituals API error")


@cli.error_handler(RitualsGenieError)
def error_handler(error: RitualsGenieError) -> None:
    """Handle any other error from the library."""
    error_panel(str(error), "Error")


@cli.command("hubs")
async def hubs_command(
    email: Email,
    password: Password,
    output_json: JsonFlag = False,  # noqa: FBT002
) -> None:
    """List all diffusers on the account and their current state."""
    async with client(email, password) as genie:
        hubs = await genie.hubs()

    if output_json:
        emit_json([hub.to_dict() for hub in hubs])
        return

    table = Table(title="Perfume Genie diffusers")
    table.add_column("Name", style="cyan bold")
    table.add_column("Hublot")
    table.add_column("Online")
    table.add_column("On")
    table.add_column("Perfume amount")
    table.add_column("Room size")
    table.add_column("Firmware")

    for hub in hubs:
        table.add_row(
            display_text(hub.name or "-"),
            display_text(hub.hublot),
            display_yes_no(hub.is_online),
            display_yes_no(hub.is_on),
            str(hub.perfume_amount or "-"),
            display_room_size(hub),
            display_firmware(hub),
        )

    console.print(table)


@cli.command("sensors")
async def sensors_command(
    hub: Hub,
    email: Email,
    password: Password,
    output_json: JsonFlag = False,  # noqa: FBT002
) -> None:
    """Show the sensor readings of a diffuser."""
    async with client(email, password) as genie:
        diffuser = find_hub(await genie.hubs(), hub)
        sensors = await genie.sensors(diffuser)

    if output_json:
        readings = {
            "battery": sensors.battery,
            "fill": sensors.fill,
            "perfume": sensors.perfume,
            "version": sensors.version,
            "wifi": sensors.wifi,
        }
        emit_json(
            {
                name: reading.to_dict() if reading is not None else None
                for name, reading in readings.items()
            }
        )
        return

    table = Table(title=f"Sensors of {display_name(diffuser)}")
    table.add_column("Sensor", style="cyan bold")
    table.add_column("Reading")

    if sensors.perfume is not None:
        table.add_row("Perfume", display_text(sensors.perfume_name or "-"))

        if sensors.has_cartridge is not None:
            table.add_row("Cartridge inserted", display_yes_no(sensors.has_cartridge))

    if sensors.fill is not None:
        fill = display_text(sensors.fill_level or "-")
        if sensors.fill_percentage is not None:
            fill += f" (about {sensors.fill_percentage} %)"

        table.add_row("Fill level", fill)

    if sensors.wifi is not None:
        signal = display_text(sensors.wifi.title or "-")
        if sensors.wifi_rssi is not None:
            signal += f" ({sensors.wifi_rssi} dBm)"

        table.add_row("WiFi signal", signal)

    if sensors.battery is not None:
        level = sensors.battery_percentage

        table.add_row("Battery", f"{level} %" if level is not None else "-")
        table.add_row("Charging", display_yes_no(sensors.battery_charging))

    if sensors.version is not None:
        version = sensors.version.title or sensors.version.value or "-"
        table.add_row("Version", display_text(version))

    console.print(table)


@cli.command("on")
async def on_command(hub: Hub, email: Email, password: Password) -> None:
    """Turn on a diffuser."""
    async with client(email, password) as genie:
        diffuser = find_hub(await genie.hubs(), hub)
        await genie.turn_on(diffuser.hash)

    console.print(f"[green bold]{display_name(diffuser)} turned on.[/]")


@cli.command("off")
async def off_command(hub: Hub, email: Email, password: Password) -> None:
    """Turn off a diffuser."""
    async with client(email, password) as genie:
        diffuser = find_hub(await genie.hubs(), hub)
        await genie.turn_off(diffuser.hash)

    console.print(f"[yellow bold]{display_name(diffuser)} turned off.[/]")


@cli.command("perfume-amount")
async def perfume_amount_command(
    hub: Hub,
    amount: Annotated[
        int,
        typer.Argument(help="Perfume intensity, 1 (low) to 3 (high)", min=1, max=3),
    ],
    email: Email,
    password: Password,
) -> None:
    """Set the perfume intensity of a diffuser."""
    async with client(email, password) as genie:
        diffuser = find_hub(await genie.hubs(), hub)
        await genie.set_perfume_amount(diffuser.hash, amount)

    console.print(f"[green bold]Perfume amount set to {amount}.[/]")


@cli.command("room-size")
async def room_size_command(
    hub: Hub,
    square_meters: Annotated[
        int,
        typer.Argument(help="Room size in square meters", min=1),
    ],
    email: Email,
    password: Password,
) -> None:
    """Set the room size of a diffuser."""
    async with client(email, password) as genie:
        diffuser = find_hub(await genie.hubs(), hub)
        await genie.set_room_square_meters(diffuser.hash, square_meters)

    console.print(f"[green bold]Room size set to {square_meters} m².[/]")


@cli.command("standby-led")
async def standby_led_command(
    hub: Hub,
    state: Annotated[OnOff, typer.Argument(help="Turn the standby LED on or off")],
    email: Email,
    password: Password,
) -> None:
    """Turn the LED that shows while the diffuser is on standby on or off."""
    async with client(email, password) as genie:
        diffuser = find_hub(await genie.hubs(), hub)

        if not diffuser.supports_standby_led:
            fail("This diffuser doesn't support switching the standby LED.")

        await genie.set_standby_led(diffuser.hash, enabled=state is OnOff.ON)

    console.print(f"[green bold]Standby LED turned {state}.[/]")


@cli.command("update-firmware")
async def update_firmware_command(hub: Hub, email: Email, password: Password) -> None:
    """Start a firmware update of a diffuser."""
    async with client(email, password) as genie:
        diffuser = find_hub(await genie.hubs(), hub)

        firmware = diffuser.firmware
        available = firmware.update_available if firmware is not None else None

        if available is None:
            fail("The API doesn't tell whether a firmware update is available.")

        if not available:
            err_console.print(
                "[yellow]The diffuser already runs the latest firmware.[/]"
            )
            return

        await genie.update_firmware(diffuser.hash)

    newest = firmware.newest if firmware is not None else None
    console.print(f"[green bold]Firmware update to {display_text(newest)} started.[/]")


@cli.command("info")
async def info_command(
    hub: Hub,
    email: Email,
    password: Password,
    output_json: JsonFlag = False,  # noqa: FBT002
) -> None:
    """Show the details of a diffuser, including its model."""
    async with client(email, password) as genie:
        diffuser = find_hub(await genie.hubs(), hub)
        details = await genie.hublot(diffuser.hublot)

    if output_json:
        emit_json({"hub": diffuser.to_dict(), "hublot": details.to_dict()})
        return

    table = Table(title=display_name(diffuser), show_header=False)
    table.add_column("Property", style="cyan bold")
    table.add_column("Value")

    table.add_row("Model", display_text(details.device_version or "-"))
    table.add_row("Generation", str(diffuser.generation or "-"))
    table.add_row("Color", display_text(diffuser.color or "-"))
    table.add_row("Hublot", display_text(diffuser.hublot))

    table.add_row("Online", display_yes_no(diffuser.is_online))
    table.add_row("On", display_yes_no(diffuser.is_on))
    table.add_row("Perfume amount", str(diffuser.perfume_amount or "-"))
    table.add_row("Room size", display_room_size(diffuser))

    if diffuser.supports_standby_led:
        table.add_row("Standby LED", display_yes_no(diffuser.standby_led))

    table.add_row("Firmware", display_firmware(diffuser))
    table.add_row("Schedules", display_yes_no(diffuser.has_schedules))
    table.add_row("Timezone", display_text(diffuser.timezone or "-"))

    console.print(table)


@cli.command("dump")
async def dump_command(
    email: Email,
    password: Password,
    with_sensors: SensorsFlag = False,  # noqa: FBT002
) -> None:
    """Dump raw API responses as JSON, with identifiers and names redacted."""
    dump: list[dict[str, Any]] = []

    async with client(email, password) as genie:
        for raw_hub in await genie.raw_hubs():
            entry: dict[str, Any] = {"hub": redact_hub(raw_hub)}

            # Raw data only: this has to work when the models don't.
            if with_sensors and isinstance(hub_hash := raw_hub.get("hash"), str):
                supported = raw_hub.get("sensors") or {}
                entry["sensors"] = {
                    str(sensor): await genie.raw_sensor(hub_hash, sensor)
                    for sensor in Sensor
                    if supported.get(sensor)
                }

            dump.append(entry)

    emit_json(dump)
