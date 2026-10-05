"""Tests for the Rituals Perfume Genie CLI."""

# pylint: disable=redefined-outer-name
from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from typer.testing import CliRunner

from ritualsgenie import (
    RitualsGenieAuthenticationError,
    RitualsGenieConnectionError,
    RitualsGenieError,
    RitualsGenieHub,
    RitualsGenieHublot,
    RitualsGenieRateLimitError,
    RitualsGenieResponseError,
    RitualsGenieSensor,
    RitualsGenieSensors,
)
from ritualsgenie.cli import cli

if TYPE_CHECKING:
    from collections.abc import Generator

    from syrupy.assertion import SnapshotAssertion

FIXTURES_DIR = Path(__file__).parent.parent / "fixtures"
CREDENTIALS = ["--email", "user@example.com", "--password", "secret"]


def _load_fixture(name: str) -> Any:
    """Load a fixture file and return parsed JSON."""
    return json.loads((FIXTURES_DIR / name).read_text(encoding="utf-8"))


@pytest.fixture(autouse=True)
def stable_terminal(monkeypatch: pytest.MonkeyPatch) -> None:
    """Force deterministic Rich rendering for stable snapshots."""
    monkeypatch.setenv("COLUMNS", "120")
    monkeypatch.setenv("NO_COLOR", "1")
    monkeypatch.setenv("TERM", "dumb")


@pytest.fixture(autouse=True)
def token_cache(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Keep the token cache of the tests away from the real one."""
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    monkeypatch.delenv("RITUALS_NO_TOKEN_CACHE", raising=False)
    return tmp_path / "ritualsgenie"


@pytest.fixture
def runner() -> CliRunner:
    """Return a CLI runner for invoking the Typer app."""
    return CliRunner()


@pytest.fixture
def client() -> Generator[AsyncMock, None, None]:
    """Patch the CLI's client with a mock backed by the fixtures."""
    raw_hubs = _load_fixture("hubs.json")

    mock = AsyncMock()
    mock.raw_hubs.return_value = raw_hubs
    mock.hubs.return_value = [RitualsGenieHub.from_dict(hub) for hub in raw_hubs]
    mock.raw_sensor.return_value = _load_fixture("sensor_fill.json")
    mock.hublot.return_value = RitualsGenieHublot.from_dict(
        _load_fixture("hublot.json")
    )
    mock.sensors.return_value = RitualsGenieSensors(
        fill=RitualsGenieSensor.from_dict(_load_fixture("sensor_fill.json")),
        perfume=RitualsGenieSensor.from_dict(_load_fixture("sensor_perfume.json")),
        wifi=RitualsGenieSensor.from_dict(_load_fixture("sensor_wifi.json")),
        battery=RitualsGenieSensor.from_dict(_load_fixture("sensor_battery.json")),
        version=RitualsGenieSensor.from_dict(_load_fixture("sensor_version.json")),
    )

    instance = MagicMock()
    instance.__aenter__ = AsyncMock(return_value=mock)
    instance.__aexit__ = AsyncMock(return_value=None)

    with patch(
        "ritualsgenie.cli.helpers.RitualsGenie", return_value=instance
    ) as constructor:
        mock.constructor = constructor
        yield mock


def test_hubs(
    runner: CliRunner, client: AsyncMock, snapshot: SnapshotAssertion
) -> None:
    """Test listing the diffusers as a table."""
    result = runner.invoke(cli, ["hubs", *CREDENTIALS])

    assert result.exit_code == 0, result.output
    assert result.output == snapshot
    client.hubs.assert_awaited_once()


def test_hubs_json(runner: CliRunner, client: AsyncMock) -> None:
    """Test listing the diffusers as JSON."""
    result = runner.invoke(cli, ["hubs", *CREDENTIALS, "--json"])

    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert [hub["attributeValues"]["roomnamec"] for hub in data] == [
        "Woonkamer",
        "Slaapkamer Genie",
    ]
    client.hubs.assert_awaited_once()


def test_sensors(
    runner: CliRunner, client: AsyncMock, snapshot: SnapshotAssertion
) -> None:
    """Test showing the sensors of a diffuser, found by name."""
    result = runner.invoke(cli, ["sensors", "woonkamer", *CREDENTIALS])

    assert result.exit_code == 0, result.output
    assert result.output == snapshot
    assert client.sensors.await_args.args[0].hublot == "LOT000-00-00000-00001"


def test_sensors_json(runner: CliRunner, client: AsyncMock) -> None:
    """Test showing the sensors of a diffuser as JSON."""
    client.sensors.return_value = RitualsGenieSensors(
        fill=RitualsGenieSensor(title="70-80%")
    )

    result = runner.invoke(cli, ["sensors", "Slaapkamer Genie", *CREDENTIALS, "--json"])

    assert result.exit_code == 0, result.output
    assert json.loads(result.output) == {
        "battery": None,
        "fill": {"title": "70-80%"},
        "perfume": None,
        "version": None,
        "wifi": None,
    }


def test_unknown_hub(runner: CliRunner, client: AsyncMock) -> None:
    """Test a diffuser that doesn't exist exits with an error."""
    result = runner.invoke(cli, ["on", "Kitchen", *CREDENTIALS])

    assert result.exit_code == 1
    assert "No diffuser found" in result.output
    client.turn_on.assert_not_awaited()


@pytest.mark.parametrize(
    ("args", "method", "expected"),
    [
        (["on", "LOT000-00-00000-00001"], "turn_on", ()),
        (["off", "1" * 64], "turn_off", ()),
        (["perfume-amount", "Woonkamer", "2"], "set_perfume_amount", (2,)),
        (["room-size", "Woonkamer", "42"], "set_room_square_meters", (42,)),
    ],
)
def test_control(
    runner: CliRunner,
    client: AsyncMock,
    args: list[str],
    method: str,
    expected: tuple[object, ...],
) -> None:
    """Test controlling a diffuser, found by hublot, hash or name."""
    result = runner.invoke(cli, [*args, *CREDENTIALS])

    assert result.exit_code == 0, result.output
    getattr(client, method).assert_awaited_once_with("1" * 64, *expected)


def test_room_size_invalid(runner: CliRunner, client: AsyncMock) -> None:
    """Test an impossible room size is refused before any request."""
    result = runner.invoke(cli, ["room-size", "Woonkamer", "0", *CREDENTIALS])

    assert result.exit_code == 2
    client.hubs.assert_not_awaited()


@pytest.mark.parametrize(("state", "enabled"), [("on", True), ("off", False)])
def test_standby_led(
    runner: CliRunner, client: AsyncMock, state: str, *, enabled: bool
) -> None:
    """Test switching the standby LED."""
    result = runner.invoke(cli, ["standby-led", "Woonkamer", state, *CREDENTIALS])

    assert result.exit_code == 0, result.output
    client.set_standby_led.assert_awaited_once_with("1" * 64, enabled=enabled)


def test_standby_led_unsupported(runner: CliRunner, client: AsyncMock) -> None:
    """Test the standby LED is refused on diffusers that can't switch it."""
    client.hubs.return_value[1].attributes["ledc"] = False
    result = runner.invoke(
        cli, ["standby-led", "Slaapkamer Genie", "off", *CREDENTIALS]
    )

    assert result.exit_code == 1
    assert "doesn't support" in result.output
    client.set_standby_led.assert_not_awaited()


def test_update_firmware(runner: CliRunner, client: AsyncMock) -> None:
    """Test starting a firmware update when one is available."""
    result = runner.invoke(cli, ["update-firmware", "Slaapkamer Genie", *CREDENTIALS])

    assert result.exit_code == 0, result.output
    assert "update to 5.4 started" in result.output
    client.update_firmware.assert_awaited_once_with("2" * 64)


def test_update_firmware_latest(runner: CliRunner, client: AsyncMock) -> None:
    """Test no update is started when the firmware is already the latest."""
    result = runner.invoke(cli, ["update-firmware", "Woonkamer", *CREDENTIALS])

    assert result.exit_code == 0, result.output
    assert "already runs the latest" in result.output
    client.update_firmware.assert_not_awaited()


def test_info(
    runner: CliRunner, client: AsyncMock, snapshot: SnapshotAssertion
) -> None:
    """Test showing the details of a diffuser."""
    result = runner.invoke(cli, ["info", "Woonkamer", *CREDENTIALS])

    assert result.exit_code == 0, result.output
    assert result.output == snapshot
    client.hublot.assert_awaited_once_with("LOT000-00-00000-00001")


def test_info_json(runner: CliRunner, client: AsyncMock) -> None:
    """Test showing the details of a diffuser as JSON.

    Unlike dump, this is your own data for your own use, so not redacted.
    """
    result = runner.invoke(cli, ["info", "Woonkamer", *CREDENTIALS, "--json"])

    assert result.exit_code == 0, result.output
    data = json.loads(result.stdout)
    assert data["hublot"]["deviceVersion"] == "Genie 2.1"
    assert data["hub"]["hash"] == "1" * 64
    client.hublot.assert_awaited_once()


def test_dump(runner: CliRunner, client: AsyncMock) -> None:
    """Test dumping the raw hubs, with identifiers redacted."""
    result = runner.invoke(cli, ["dump", *CREDENTIALS])

    assert result.exit_code == 0, result.output
    data = json.loads(result.stdout)
    hubs = [entry["hub"] for entry in data]
    assert [hub["hash"] for hub in hubs] == ["**REDACTED**"] * 2
    assert [hub["hublot"] for hub in hubs] == ["**REDACTED**"] * 2
    assert {hub["attributeValues"]["roomnamec"] for hub in hubs} == {"**REDACTED**"}
    assert {hub["attributeValues"]["fspacenamec"] for hub in hubs} == {"**REDACTED**"}
    # Not identifying, and useful in a bug report.
    assert hubs[0]["attributeValues"]["speedc"] == "3"
    assert "sensors" not in data[0]
    client.raw_sensor.assert_not_awaited()


def test_dump_with_sensors(runner: CliRunner, client: AsyncMock) -> None:
    """Test dumping the raw hubs including their supported sensors."""
    result = runner.invoke(cli, ["dump", *CREDENTIALS, "--sensors"])

    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert list(data[0]["sensors"]) == ["fillc", "rfidc", "versionc", "wific"]
    assert list(data[1]["sensors"]) == ["battc", "fillc", "rfidc", "wific"]
    assert client.raw_sensor.await_count == 8


@pytest.mark.parametrize(
    ("exception", "title"),
    [
        (RitualsGenieAuthenticationError, "Authentication error"),
        (RitualsGenieConnectionError, "Connection error"),
        (RitualsGenieRateLimitError, "Rate limited"),
        (RitualsGenieResponseError, "Rituals API error"),
        (RitualsGenieError, "Error"),
    ],
)
def test_error_handlers(
    capsys: pytest.CaptureFixture[str], exception: type[Exception], title: str
) -> None:
    """Test errors are shown as a friendly panel and exit with 1."""
    # The CLI runner bypasses AsyncTyper.__call__, so call the handlers directly.
    handler = cli.error_handlers[exception]

    with pytest.raises(SystemExit) as exc_info:
        handler(exception("boom"))

    assert exc_info.value.code == 1
    assert title in capsys.readouterr().err


@pytest.mark.parametrize(
    "exception",
    [RitualsGenieAuthenticationError, RitualsGenieResponseError, RitualsGenieError],
)
def test_error_handlers_show_message(
    capsys: pytest.CaptureFixture[str], exception: type[Exception]
) -> None:
    """Test the message of the API ends up in front of the user."""
    with pytest.raises(SystemExit):
        cli.error_handlers[exception](exception("Value 4 is not a valid option"))

    assert "Value 4 is not a valid option" in capsys.readouterr().err


def test_rate_limit_handler_retry_after(capsys: pytest.CaptureFixture[str]) -> None:
    """Test the rate limit panel tells how long to wait, when known."""
    handler = cli.error_handlers[RitualsGenieRateLimitError]

    with pytest.raises(SystemExit):
        handler(RitualsGenieRateLimitError("locked out", retry_after=1296))

    assert "wait 22 minutes" in capsys.readouterr().err


def test_dump_with_unparsable_hub(runner: CliRunner, client: AsyncMock) -> None:
    """Test dump still captures hubs the models can't parse."""
    client.raw_hubs.return_value = [{"sensors": {"fillc": True}}, {"hash": "x"}]

    result = runner.invoke(cli, ["dump", *CREDENTIALS, "--sensors"])

    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert "sensors" not in data[0]
    assert data[1]["sensors"] == {}


def test_dump_redacts_pin(runner: CliRunner, client: AsyncMock) -> None:
    """Test a PIN, when there is one, doesn't end up in a dump."""
    client.raw_hubs.return_value[0]["pin"] = "123456"

    result = runner.invoke(cli, ["dump", *CREDENTIALS])

    assert result.exit_code == 0, result.output
    data = json.loads(result.stdout)
    assert data[0]["hub"]["pin"] == "**REDACTED**"
    assert data[1]["hub"]["pin"] is None


def test_token_is_cached(
    runner: CliRunner, client: AsyncMock, token_cache: Path
) -> None:
    """Test the token is saved, readable by the user only, and reused."""
    client.token = "new-token"

    result = runner.invoke(cli, ["hubs", *CREDENTIALS])
    assert result.exit_code == 0, result.output
    assert client.constructor.call_args.kwargs["token"] is None

    (cached,) = token_cache.iterdir()
    assert cached.read_text(encoding="utf-8") == "new-token"
    assert cached.stat().st_mode & 0o777 == 0o600
    assert token_cache.stat().st_mode & 0o777 == 0o700

    result = runner.invoke(cli, ["hubs", *CREDENTIALS])
    assert result.exit_code == 0, result.output
    assert client.constructor.call_args.kwargs["token"] == "new-token"


def test_token_cache_per_account(
    runner: CliRunner, client: AsyncMock, token_cache: Path
) -> None:
    """Test accounts don't share a cached token."""
    client.token = "token-one"
    runner.invoke(cli, ["hubs", *CREDENTIALS])

    other = ["--email", "other@example.com", "--password", "secret"]
    runner.invoke(cli, ["hubs", *other])

    assert client.constructor.call_args.kwargs["token"] is None
    assert len(list(token_cache.iterdir())) == 2


def test_token_cache_disabled(
    runner: CliRunner,
    client: AsyncMock,
    token_cache: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Test the token cache can be turned off."""
    monkeypatch.setenv("RITUALS_NO_TOKEN_CACHE", "1")
    client.token = "new-token"

    result = runner.invoke(cli, ["hubs", *CREDENTIALS])

    assert result.exit_code == 0, result.output
    assert not token_cache.exists()


def test_token_cache_unwritable(
    runner: CliRunner, client: AsyncMock, token_cache: Path
) -> None:
    """Test a cache that can't be written doesn't fail the command."""
    token_cache.parent.mkdir(parents=True, exist_ok=True)
    token_cache.write_text("not a directory", encoding="utf-8")
    client.token = "new-token"

    result = runner.invoke(cli, ["hubs", *CREDENTIALS])

    assert result.exit_code == 0, result.output


@pytest.mark.usefixtures("client")
def test_errors_go_to_stderr(runner: CliRunner) -> None:
    """Test JSON output stays clean when something goes wrong."""
    result = runner.invoke(cli, ["sensors", "Kitchen", *CREDENTIALS, "--json"])

    assert result.exit_code == 1
    assert result.stdout == ""
    assert "No diffuser found" in result.stderr


def test_names_are_not_markup(runner: CliRunner, client: AsyncMock) -> None:
    """Test a name with brackets is printed as is, after the command worked."""
    client.hubs.return_value[0].attribute_values["roomnamec"] = "Room [/broken]"

    result = runner.invoke(cli, ["on", "Room [/broken]", *CREDENTIALS])

    assert result.exit_code == 0, result.output
    assert "Room [/broken] turned on." in result.stdout
    client.turn_on.assert_awaited_once()


def test_ambiguous_name(runner: CliRunner, client: AsyncMock) -> None:
    """Test two diffusers with the same name are not picked at random."""
    for hub in client.hubs.return_value:
        hub.attribute_values["roomnamec"] = "Genie"

    result = runner.invoke(cli, ["off", "genie", *CREDENTIALS])

    assert result.exit_code == 1
    assert "LOT000-00-00000-00001, LOT000-00-00000-00002" in result.stderr
    client.turn_off.assert_not_awaited()


def test_identifier_wins_over_name(runner: CliRunner, client: AsyncMock) -> None:
    """Test a hublot finds its diffuser, even if another one is named like it."""
    client.hubs.return_value[0].attribute_values["roomnamec"] = "LOT000-00-00000-00002"

    result = runner.invoke(cli, ["on", "LOT000-00-00000-00002", *CREDENTIALS])

    assert result.exit_code == 0, result.output
    client.turn_on.assert_awaited_once_with("2" * 64)


def test_update_firmware_unknown(runner: CliRunner, client: AsyncMock) -> None:
    """Test missing firmware info isn't presented as the latest firmware."""
    client.hubs.return_value[0].firmware = None

    result = runner.invoke(cli, ["update-firmware", "Woonkamer", *CREDENTIALS])

    assert result.exit_code == 1
    assert "doesn't tell" in result.stderr
    client.update_firmware.assert_not_awaited()


@pytest.mark.parametrize(
    ("exception", "title"),
    [
        (RitualsGenieAuthenticationError("Wrong credentials"), "Authentication error"),
        (RitualsGenieRateLimitError("Slow down", retry_after=60), "Rate limited"),
        (RitualsGenieResponseError("Value 4 is not valid"), "Rituals API error"),
        (RitualsGenieConnectionError("Down"), "Connection error"),
    ],
)
def test_error_dispatch(
    capsys: pytest.CaptureFixture[str],
    client: AsyncMock,
    exception: Exception,
    title: str,
) -> None:
    """Test errors from a command reach the matching handler, end to end."""
    client.hubs.side_effect = exception

    with pytest.raises(SystemExit) as exc_info:
        cli(["hubs", *CREDENTIALS], prog_name="ritualsgenie")

    assert exc_info.value.code == 1
    captured = capsys.readouterr()
    assert title in captured.err
    assert captured.out == ""
