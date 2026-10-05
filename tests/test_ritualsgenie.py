"""Tests for the Rituals Perfume Genie API client."""

# pylint: disable=protected-access
import asyncio
import base64
import json
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import aiohttp
import pytest
from aioresponses import CallbackResult, aioresponses
from syrupy.assertion import SnapshotAssertion
from yarl import URL

from ritualsgenie import (
    Attribute,
    RitualsGenie,
    RitualsGenieAuthenticationError,
    RitualsGenieConnectionError,
    RitualsGenieConnectionTimeoutError,
    RitualsGenieHub,
    RitualsGenieRateLimitError,
    RitualsGenieResponseError,
    RitualsGenieSensor,
    RitualsGenieSensors,
    RitualsGenieValueError,
    RoomSize,
    Sensor,
)
from ritualsgenie.util import token_expired, utcnow

from .conftest import (
    API,
    HUB_ONE,
    HUB_TWO,
    HUBS_URL,
    LOGIN_URL,
    TOKEN,
    Clock,
    calls,
    load_fixture,
    make_token,
    mock_hubs,
    mock_login,
)

# --- Authentication ---


async def test_login(responses: aioresponses, genie: RitualsGenie) -> None:
    """Test logging in stores the token and sends the credentials."""
    mock_login(responses)

    await genie.login()

    assert genie.token == TOKEN
    (request,) = calls(responses, "POST", LOGIN_URL)
    assert request.kwargs["json"] == {"email": "user@example.com", "password": "secret"}
    assert "Authorization" not in request.kwargs["headers"]


@pytest.mark.parametrize("status", [400, 401, 422])
async def test_login_invalid_credentials(
    responses: aioresponses, genie: RitualsGenie, status: int
) -> None:
    """Test login with invalid credentials raises an authentication error."""
    responses.post(LOGIN_URL, status=status)

    with pytest.raises(RitualsGenieAuthenticationError):
        await genie.login()

    assert genie.token is None


@pytest.mark.parametrize("status", [403, 404])
async def test_login_blocked_is_not_bad_credentials(
    responses: aioresponses, genie: RitualsGenie, status: int
) -> None:
    """Test a firewall or moved endpoint doesn't look like a wrong password."""
    responses.post(LOGIN_URL, status=status)

    with pytest.raises(RitualsGenieResponseError) as excinfo:
        await genie.login()

    assert not isinstance(excinfo.value, RitualsGenieAuthenticationError)


async def test_login_refused(responses: aioresponses, genie: RitualsGenie) -> None:
    """Test an explicit success false is a wrong password."""
    responses.post(LOGIN_URL, status=200, payload={"success": False})

    with pytest.raises(RitualsGenieAuthenticationError):
        await genie.login()


@pytest.mark.parametrize(
    ("body", "content_type"),
    [
        ('{"message": "Wrong password"}', "application/json"),
        ("[]", "application/json"),
        ("<html>Down for maintenance</html>", "text/html"),
    ],
)
async def test_login_unexpected_response(
    responses: aioresponses, genie: RitualsGenie, body: str, content_type: str
) -> None:
    """Test a login response we don't understand isn't a wrong password."""
    responses.post(LOGIN_URL, status=200, body=body, content_type=content_type)

    with pytest.raises(RitualsGenieResponseError) as excinfo:
        await genie.login()

    assert not isinstance(excinfo.value, RitualsGenieAuthenticationError)


async def test_login_failure_keeps_token(
    responses: aioresponses, genie: RitualsGenie
) -> None:
    """Test a failed login doesn't throw away a token that may still work."""
    genie.token = "still-good"
    responses.post(LOGIN_URL, exception=TimeoutError())

    with pytest.raises(RitualsGenieConnectionTimeoutError):
        await genie.login()

    assert genie.token == "still-good"


async def test_concurrent_requests_share_failed_login(
    responses: aioresponses, genie: RitualsGenie
) -> None:
    """Test requests waiting on a failed login don't each log in again."""

    # A login that takes a moment, so the other requests are really waiting
    # on it, like they would be against the real API.
    async def slow_refusal(_url: URL, **_kwargs: Any) -> CallbackResult:
        await asyncio.sleep(0.01)
        return CallbackResult(status=200, payload={"success": False})

    responses.post(LOGIN_URL, callback=slow_refusal, repeat=True)

    results = await asyncio.gather(
        genie.hubs(), genie.hubs(), genie.hubs(), return_exceptions=True
    )

    assert all(isinstance(r, RitualsGenieAuthenticationError) for r in results)
    assert len(calls(responses, "POST", LOGIN_URL)) == 1


async def test_lockout_is_remembered(
    responses: aioresponses, genie: RitualsGenie
) -> None:
    """Test no login is attempted while still locked out."""
    responses.post(
        LOGIN_URL,
        status=200,
        body=load_fixture("error_login_locked_out.json"),
        content_type="application/json",
        repeat=True,
    )

    with pytest.raises(RitualsGenieRateLimitError):
        await genie.login()

    with pytest.raises(RitualsGenieRateLimitError) as excinfo:
        await genie.login()

    assert excinfo.value.retry_after is not None
    assert 0 < excinfo.value.retry_after <= 1296
    assert len(calls(responses, "POST", LOGIN_URL)) == 1


async def test_lockout_expires(responses: aioresponses, genie: RitualsGenie) -> None:
    """Test logging in is tried again once the lockout has passed."""
    genie._locked_out_until = datetime.now(tz=UTC) - timedelta(seconds=1)
    mock_login(responses)

    await genie.login()

    assert genie.token == TOKEN
    assert genie._locked_out_until is None


async def test_lockout_with_error_status(
    responses: aioresponses, genie: RitualsGenie
) -> None:
    """Test a lockout is recognized even if it ever comes with a 401."""
    responses.post(
        LOGIN_URL,
        status=401,
        body=load_fixture("error_login_locked_out.json"),
        content_type="application/json",
    )

    with pytest.raises(RitualsGenieRateLimitError):
        await genie.login()


async def test_login_locked_out(responses: aioresponses, genie: RitualsGenie) -> None:
    """Test a login lockout is a rate limit, not a wrong password.

    The API answers both with a 200 and success false.
    """
    responses.post(
        LOGIN_URL,
        status=200,
        body=load_fixture("error_login_locked_out.json"),
        content_type="application/json",
    )

    with pytest.raises(RitualsGenieRateLimitError) as excinfo:
        await genie.login()

    assert excinfo.value.retry_after == 1296
    assert not isinstance(excinfo.value, RitualsGenieAuthenticationError)
    assert genie.token is None


async def test_login_wrong_password_message(
    responses: aioresponses, genie: RitualsGenie
) -> None:
    """Test the message from the API ends up in the authentication error."""
    responses.post(
        LOGIN_URL,
        status=200,
        payload={"success": False, "message": "Wrong credentials"},
    )

    with pytest.raises(RitualsGenieAuthenticationError, match="Wrong credentials"):
        await genie.login()


async def test_login_server_error(responses: aioresponses, genie: RitualsGenie) -> None:
    """Test a server error during login is not mistaken for bad credentials."""
    responses.post(LOGIN_URL, status=500)

    with pytest.raises(RitualsGenieResponseError) as excinfo:
        await genie.login()

    assert not isinstance(excinfo.value, RitualsGenieAuthenticationError)


async def test_lazy_login_and_token_reuse(
    responses: aioresponses, genie: RitualsGenie
) -> None:
    """Test the client logs in on first use and reuses its token afterwards."""
    mock_login(responses)
    mock_hubs(responses)
    mock_hubs(responses)

    await genie.hubs()
    await genie.hubs()

    assert len(calls(responses, "POST", LOGIN_URL)) == 1
    hub_requests = calls(responses, "GET", HUBS_URL)
    assert len(hub_requests) == 2
    assert all(
        request.kwargs["headers"]["Authorization"] == TOKEN for request in hub_requests
    )


async def test_provided_token_is_used(responses: aioresponses) -> None:
    """Test a token passed in is used without logging in."""
    mock_hubs(responses)

    async with RitualsGenie(
        email="user@example.com", password="secret", token="stored"
    ) as genie:
        await genie.hubs()

    assert not calls(responses, "POST", LOGIN_URL)
    (request,) = calls(responses, "GET", HUBS_URL)
    assert request.kwargs["headers"]["Authorization"] == "stored"


async def test_expired_token_logs_in_again(
    responses: aioresponses, genie: RitualsGenie
) -> None:
    """Test an expired token is replaced before making the request."""
    genie.token = make_token(expires_in=timedelta(seconds=30))
    mock_login(responses)
    mock_hubs(responses)

    await genie.hubs()

    assert len(calls(responses, "POST", LOGIN_URL)) == 1
    assert genie.token == TOKEN


async def test_valid_jwt_is_reused(
    responses: aioresponses, genie: RitualsGenie
) -> None:
    """Test a token that is far from expiring is used as is."""
    genie.token = make_token(expires_in=timedelta(minutes=30))
    mock_hubs(responses)

    await genie.hubs()

    assert not calls(responses, "POST", LOGIN_URL)


async def test_rejected_token_logs_in_again(
    responses: aioresponses, genie: RitualsGenie
) -> None:
    """Test a 401 triggers a single new login and a retry."""
    genie.token = "stale"
    mock_hubs(responses, status=401)
    mock_login(responses)
    mock_hubs(responses)

    hubs = await genie.hubs()

    assert len(hubs) == 2
    assert len(calls(responses, "POST", LOGIN_URL)) == 1
    tokens = [
        r.kwargs["headers"]["Authorization"] for r in calls(responses, "GET", HUBS_URL)
    ]
    assert tokens == ["stale", TOKEN]


async def test_rejected_token_twice(
    responses: aioresponses, genie: RitualsGenie
) -> None:
    """Test a 401 after a fresh login gives up, without blaming the credentials.

    The API also uses 401 for a diffuser that isn't linked to the account.
    """
    genie.token = "stale"
    mock_hubs(responses, status=401)
    mock_login(responses)
    responses.get(
        HUBS_URL,
        status=401,
        body=load_fixture("error_not_linked.json"),
        content_type="application/json",
    )

    with pytest.raises(RitualsGenieResponseError, match="not linked") as excinfo:
        await genie.hubs()

    assert not isinstance(excinfo.value, RitualsGenieAuthenticationError)
    assert len(calls(responses, "POST", LOGIN_URL)) == 1


async def test_concurrent_requests_login_once(
    responses: aioresponses, genie: RitualsGenie
) -> None:
    """Test concurrent requests share a single login."""
    mock_login(responses)
    mock_hubs(responses)
    mock_hubs(responses)
    mock_hubs(responses)

    await asyncio.gather(genie.hubs(), genie.hubs(), genie.hubs())

    assert len(calls(responses, "POST", LOGIN_URL)) == 1


async def test_concurrent_rejections_login_once(
    responses: aioresponses, genie: RitualsGenie
) -> None:
    """Test concurrent requests hitting a 401 share a single new login."""
    genie.token = "stale"
    mock_login(responses)

    # The order in which the concurrent requests hit the API isn't fixed,
    # so answer based on the token each request carries.
    def hubs_callback(_url: URL, **kwargs: Any) -> CallbackResult:
        if kwargs["headers"]["Authorization"] == "stale":
            return CallbackResult(status=401)
        return CallbackResult(
            status=200,
            body=load_fixture("hubs.json"),
            content_type="application/json",
        )

    responses.get(HUBS_URL, callback=hubs_callback, repeat=True)

    await asyncio.gather(genie.hubs(), genie.hubs())

    assert len(calls(responses, "POST", LOGIN_URL)) == 1


# --- Request handling ---


async def test_internal_session(responses: aioresponses) -> None:
    """Test the client creates and closes its own session."""
    mock_login(responses)
    mock_hubs(responses)

    async with RitualsGenie(email="user@example.com", password="secret") as genie:
        await genie.hubs()
        assert genie.session is not None

    assert genie.session.closed


async def test_external_session_left_open(responses: aioresponses) -> None:
    """Test the client doesn't close a session it didn't create."""
    mock_login(responses)
    mock_hubs(responses)

    async with aiohttp.ClientSession() as session:
        async with RitualsGenie(
            email="user@example.com", password="secret", session=session
        ) as genie:
            await genie.hubs()

        assert not session.closed


async def test_user_agent(responses: aioresponses, genie: RitualsGenie) -> None:
    """Test the client identifies itself."""
    mock_login(responses)

    await genie.login()

    (request,) = calls(responses, "POST", LOGIN_URL)
    assert request.kwargs["headers"]["User-Agent"] == "PythonRitualsGenie"


async def test_timeout(responses: aioresponses, genie: RitualsGenie) -> None:
    """Test a timeout raises a timeout error, which is a connection error."""
    responses.post(LOGIN_URL, exception=TimeoutError())

    with pytest.raises(RitualsGenieConnectionTimeoutError):
        await genie.login()

    assert issubclass(RitualsGenieConnectionTimeoutError, RitualsGenieConnectionError)


async def test_connection_error(responses: aioresponses, genie: RitualsGenie) -> None:
    """Test a connection failure raises a connection error."""
    responses.post(LOGIN_URL, exception=aiohttp.ClientConnectionError())

    with pytest.raises(RitualsGenieConnectionError):
        await genie.login()


@pytest.mark.parametrize(
    ("headers", "retry_after"),
    [({"Retry-After": "120"}, 120), ({"Retry-After": "soon"}, None), ({}, None)],
)
async def test_rate_limit(
    responses: aioresponses,
    genie: RitualsGenie,
    headers: dict[str, str],
    retry_after: int | None,
) -> None:
    """Test hitting the rate limit raises a rate limit error."""
    mock_login(responses)
    responses.get(HUBS_URL, status=429, headers=headers)

    with pytest.raises(RitualsGenieRateLimitError) as excinfo:
        await genie.hubs()

    assert excinfo.value.retry_after == retry_after


async def test_server_error(responses: aioresponses, genie: RitualsGenie) -> None:
    """Test a server error raises a response error."""
    mock_login(responses)
    responses.get(HUBS_URL, status=502, body="Bad Gateway")

    with pytest.raises(RitualsGenieResponseError):
        await genie.hubs()


async def test_invalid_json(responses: aioresponses, genie: RitualsGenie) -> None:
    """Test broken JSON raises a response error."""
    mock_login(responses)
    responses.get(HUBS_URL, status=200, body="{", content_type="application/json")

    with pytest.raises(RitualsGenieResponseError):
        await genie.hubs()


@pytest.mark.parametrize(
    ("body", "content_type"),
    [("<html></html>", "text/html"), ('{"not": "a list"}', "application/json")],
)
async def test_unexpected_hubs_response(
    responses: aioresponses, genie: RitualsGenie, body: str, content_type: str
) -> None:
    """Test a hubs response that isn't a list raises a response error."""
    mock_login(responses)
    responses.get(HUBS_URL, status=200, body=body, content_type=content_type)

    with pytest.raises(RitualsGenieResponseError):
        await genie.hubs()


# --- Hubs ---


async def test_hubs(
    responses: aioresponses, genie: RitualsGenie, snapshot: SnapshotAssertion
) -> None:
    """Test parsing the hubs of an account."""
    mock_login(responses)
    mock_hubs(responses)

    hubs = await genie.hubs()

    assert hubs == snapshot

    woonkamer, slaapkamer = hubs
    assert woonkamer.name == "Woonkamer"
    assert not woonkamer.is_online
    assert not woonkamer.is_on
    assert woonkamer.perfume_amount == 3
    assert woonkamer.room_size is RoomSize.EXTRA_LARGE
    assert woonkamer.room_size.square_meters == 100
    assert woonkamer.color == "gold"
    assert not woonkamer.has_battery
    assert woonkamer.firmware is not None
    assert not woonkamer.firmware.update_available
    assert woonkamer.supported_sensors == [
        Sensor.FILL,
        Sensor.PERFUME,
        Sensor.VERSION,
        Sensor.WIFI,
    ]

    assert slaapkamer.name == "Slaapkamer Genie"
    assert slaapkamer.is_online
    assert slaapkamer.is_on
    assert slaapkamer.room_size is RoomSize.MEDIUM
    assert slaapkamer.has_battery
    assert slaapkamer.firmware is not None
    assert slaapkamer.firmware.update_available


async def test_raw_hubs(responses: aioresponses, genie: RitualsGenie) -> None:
    """Test the raw hubs are returned untouched."""
    mock_login(responses)
    mock_hubs(responses)

    raw = await genie.raw_hubs()

    assert raw[0]["hash"] == HUB_ONE
    assert "extraFunctions" in raw[0]


def test_hub_minimal() -> None:
    """Test a hub with nothing but its identifiers still parses."""
    hub = RitualsGenieHub.from_dict({"hash": HUB_ONE, "hublot": "LOT"})

    assert hub.name is None
    assert hub.is_online is None
    assert hub.is_on is None
    assert hub.perfume_amount is None
    assert hub.room_size is None
    assert hub.firmware is None
    assert hub.supported_sensors == []


@pytest.mark.parametrize("room_size", ["0", "5", "large"])
def test_hub_unknown_room_size(room_size: str) -> None:
    """Test room sizes we don't know are reported as None."""
    hub = RitualsGenieHub.from_dict(
        {
            "hash": HUB_ONE,
            "hublot": "LOT",
            "attributeValues": {"roomc": room_size, "speedc": "high"},
        }
    )

    assert hub.room_size is None
    assert hub.perfume_amount is None


# --- Sensors ---


def mock_sensor(
    responses: aioresponses, hub: str, sensor: Sensor, fixture: str
) -> None:
    """Mock a sensor endpoint."""
    responses.get(
        f"{API}hubs/{hub}/sensors/{sensor}",
        status=200,
        body=load_fixture(fixture),
        content_type="application/json",
    )


async def test_sensors(
    responses: aioresponses, genie: RitualsGenie, snapshot: SnapshotAssertion
) -> None:
    """Test only the supported sensors of a diffuser are requested."""
    mock_login(responses)
    mock_hubs(responses)
    mock_sensor(responses, HUB_ONE, Sensor.FILL, "sensor_fill.json")
    mock_sensor(responses, HUB_ONE, Sensor.PERFUME, "sensor_perfume.json")
    mock_sensor(responses, HUB_ONE, Sensor.VERSION, "sensor_version.json")
    mock_sensor(responses, HUB_ONE, Sensor.WIFI, "sensor_wifi.json")

    woonkamer, _ = await genie.hubs()
    sensors = await genie.sensors(woonkamer)

    assert sensors == snapshot
    assert sensors.battery is None
    assert sensors.battery_charging is None
    assert sensors.battery_percentage is None
    assert sensors.has_cartridge
    assert sensors.perfume_name == "Private Collection Sweet Jasmine"
    assert sensors.fill_level == "70-80%"
    assert sensors.perfume is not None
    assert sensors.perfume.discover_url == "1115812"
    assert sensors.wifi_percentage == 75
    assert sensors.wifi_rssi == -80
    assert sensors.fill is not None
    assert sensors.fill.title == "70-80%"
    assert sensors.fill.raw == "5818"
    assert sensors.version is not None
    assert sensors.version.title == "5.4"
    assert not calls(responses, "GET", f"{API}hubs/{HUB_ONE}/sensors/battc")


async def test_sensors_battery(responses: aioresponses, genie: RitualsGenie) -> None:
    """Test the battery sensor of a portable diffuser."""
    mock_login(responses)
    mock_hubs(responses)
    mock_sensor(responses, HUB_TWO, Sensor.BATTERY, "sensor_battery.json")
    mock_sensor(responses, HUB_TWO, Sensor.FILL, "sensor_fill.json")
    mock_sensor(
        responses, HUB_TWO, Sensor.PERFUME, "sensor_perfume_with_description.json"
    )
    mock_sensor(responses, HUB_TWO, Sensor.WIFI, "sensor_wifi.json")

    _, slaapkamer = await genie.hubs()
    sensors = await genie.sensors(slaapkamer)

    assert sensors.battery_charging
    # Charging says what the battery does, not how full it is.
    assert sensors.battery_percentage is None
    assert sensors.version is None
    assert sensors.perfume is not None
    assert sensors.perfume.title == "The Ritual of Hammam"
    assert sensors.perfume.description is not None
    assert sensors.perfume.description.startswith("The Hammam")


def test_sensor_legacy_shape() -> None:
    """Test the undocumented shape older clients relied on still works."""
    legacy = RitualsGenieSensor.from_json(load_fixture("sensor_legacy.json"))

    assert legacy.id == 19
    assert legacy.raw is None
    assert RitualsGenieSensors(perfume=legacy).has_cartridge is False
    assert RitualsGenieSensors(battery=RitualsGenieSensor(id=21)).battery_charging


def test_sensors_no_cartridge() -> None:
    """Test a diffuser without a cartridge, as captured from a real one."""
    sensors = RitualsGenieSensors(
        fill=RitualsGenieSensor.from_json(
            load_fixture("sensor_fill_no_measurement.json")
        ),
        perfume=RitualsGenieSensor.from_json(
            load_fixture("sensor_perfume_no_cartridge.json")
        ),
    )

    assert sensors.has_cartridge is False
    assert sensors.perfume is not None
    assert sensors.perfume.scent_found is False
    assert sensors.perfume.title == "Cartridge is not loaded"
    assert sensors.perfume_name is None
    assert sensors.fill_level is None


def test_sensors_unknown_cartridge() -> None:
    """Test an inserted cartridge that isn't a known perfume still counts."""
    sensors = RitualsGenieSensors(
        perfume=RitualsGenieSensor(title="Unknown", raw="0a1b2c3d", scent_found=False)
    )

    assert sensors.has_cartridge
    assert sensors.perfume_name == "Unknown"


def test_sensors_without_cartridge_info() -> None:
    """Test a perfume reading without any cartridge hints."""
    sensors = RitualsGenieSensors(
        perfume=RitualsGenieSensor(title="Unknown"),
        wifi=RitualsGenieSensor(raw="weak"),
    )

    assert sensors.has_cartridge is None
    assert sensors.wifi_rssi is None
    empty = RitualsGenieSensors()
    assert empty.wifi_rssi is None
    assert empty.perfume_name is None
    assert empty.fill_level is None
    assert RitualsGenieSensors(
        perfume=RitualsGenieSensor(scent_found=True)
    ).has_cartridge


def test_sensors_unknown_icons() -> None:
    """Test icons we don't know about give no percentage instead of an error."""
    sensors = RitualsGenieSensors(
        battery=RitualsGenieSensor(icon="battery-new.png"),
        wifi=RitualsGenieSensor(),
    )

    assert sensors.battery_percentage is None
    assert sensors.wifi_percentage is None
    assert sensors.has_cartridge is None


async def test_sensor_unexpected_response(
    responses: aioresponses, genie: RitualsGenie
) -> None:
    """Test a sensor response that isn't an object raises a response error."""
    mock_login(responses)
    responses.get(
        f"{API}hubs/{HUB_ONE}/sensors/fillc",
        status=200,
        body="[]",
        content_type="application/json",
    )

    with pytest.raises(RitualsGenieResponseError):
        await genie.sensor(HUB_ONE, Sensor.FILL)


# --- Control ---


@pytest.mark.parametrize(
    ("control", "attribute", "value"),
    [
        (lambda genie: genie.turn_on(HUB_ONE), "fanc", "1"),
        (lambda genie: genie.turn_off(HUB_ONE), "fanc", "0"),
        (lambda genie: genie.set_perfume_amount(HUB_ONE, 2), "speedc", "2"),
        (
            lambda genie: genie.set_room_size_category(HUB_ONE, RoomSize.LARGE),
            "roomc",
            "3",
        ),
        (lambda genie: genie.set_standby_led(HUB_ONE, enabled=True), "ledc", "0"),
        (lambda genie: genie.set_standby_led(HUB_ONE, enabled=False), "ledc", "1"),
    ],
    ids=[
        "turn_on",
        "turn_off",
        "set_perfume_amount",
        "set_room_size_category",
        "standby_led_on",
        "standby_led_off",
    ],
)
async def test_control(
    responses: aioresponses,
    genie: RitualsGenie,
    control: Callable[[RitualsGenie], Awaitable[None]],
    attribute: str,
    value: str,
) -> None:
    """Test controlling a diffuser posts the attribute as JSON."""
    url = f"{API}hubs/{HUB_ONE}/attributes/{attribute}"
    mock_login(responses)
    responses.post(url, status=200, body="OK", content_type="text/plain")

    await control(genie)

    (request,) = calls(responses, "POST", url)
    assert request.kwargs["json"] == {attribute: value}
    assert request.kwargs["headers"]["Authorization"] == TOKEN


@pytest.mark.parametrize("amount", [0, 4])
async def test_set_perfume_amount_invalid(genie: RitualsGenie, amount: int) -> None:
    """Test an out of range perfume amount is refused before any request."""
    with pytest.raises(RitualsGenieValueError, match="'amount'"):
        await genie.set_perfume_amount(HUB_ONE, amount)


# --- Other endpoints ---


async def test_hub(
    responses: aioresponses, genie: RitualsGenie, snapshot: SnapshotAssertion
) -> None:
    """Test fetching a single diffuser."""
    mock_login(responses)
    responses.get(
        f"{API}account/hubs/{HUB_ONE}",
        status=200,
        body=load_fixture("hub.json"),
        content_type="application/json",
    )

    assert await genie.hub(HUB_ONE) == snapshot


async def test_hub_schedule_info(responses: aioresponses, genie: RitualsGenie) -> None:
    """Test the planned schedule change is parsed."""
    mock_login(responses)
    mock_hubs(responses)

    _, slaapkamer = await genie.hubs()

    assert slaapkamer.has_schedules
    assert slaapkamer.next_fan_change is not None
    assert slaapkamer.next_fan_change.current_state == "on"
    assert slaapkamer.next_fan_change.next_state_change_time == "17:00"
    assert slaapkamer.next_fan_change.next_state_change_value == 0


async def test_hublot(responses: aioresponses, genie: RitualsGenie) -> None:
    """Test looking up the hardware details of a diffuser."""
    mock_login(responses)
    responses.get(
        f"{API}hublots/LOT000-00-00000-00001",
        status=200,
        body=load_fixture("hublot.json"),
        content_type="application/json",
    )

    hublot = await genie.hublot("LOT000-00-00000-00001")

    assert hublot.device_version == "Genie 2.1"
    assert hublot.hub_ssid == "Perfume Genie"
    assert hublot.hub_hash == HUB_ONE
    assert hublot.add_device_flow_version == 2
    assert hublot.color == "gold"
    assert hublot.firmware_id == 127
    assert hublot.is_secure is False


async def test_attribute(responses: aioresponses, genie: RitualsGenie) -> None:
    """Test fetching a single attribute with its description."""
    mock_login(responses)
    responses.get(
        f"{API}hubs/{HUB_ONE}/attributes/speedc",
        status=200,
        body=load_fixture("attribute.json"),
        content_type="application/json",
    )

    attribute = await genie.attribute(HUB_ONE, Attribute.PERFUME_AMOUNT)

    assert attribute.description == "High fan speed"
    assert attribute.value == "3"


async def test_update_firmware(responses: aioresponses, genie: RitualsGenie) -> None:
    """Test starting a firmware update."""
    url = f"{API}hubs/{HUB_ONE}/update"
    mock_login(responses)
    responses.post(url, status=200, payload="OTA started")

    await genie.update_firmware(HUB_ONE)

    assert len(calls(responses, "POST", url)) == 1


async def test_update_firmware_already_latest(
    responses: aioresponses, genie: RitualsGenie
) -> None:
    """Test the API error message ends up in the exception."""
    mock_login(responses)
    responses.post(
        f"{API}hubs/{HUB_ONE}/update",
        status=409,
        payload="The Hub already has the latest version",
    )

    with pytest.raises(RitualsGenieResponseError, match="already has the latest"):
        await genie.update_firmware(HUB_ONE)


async def test_api_error_message(responses: aioresponses, genie: RitualsGenie) -> None:
    """Test a documented API error message ends up in the exception."""
    url = f"{API}hubs/{HUB_ONE}/attributes/speedc"
    mock_login(responses)
    responses.post(
        url,
        status=400,
        body=load_fixture("error_invalid_value.json"),
        content_type="application/json",
    )

    with pytest.raises(RitualsGenieResponseError, match="not a valid option"):
        await genie.set_perfume_amount(HUB_ONE, 3)


async def test_api_error_alternative_shape(
    responses: aioresponses, genie: RitualsGenie
) -> None:
    """Test the alternative error shape some endpoints use."""
    mock_login(responses)
    responses.get(HUBS_URL, status=403, payload={"error": "This is not your Group"})

    with pytest.raises(RitualsGenieResponseError, match="not your Group"):
        await genie.hubs()


async def test_hub_unexpected_response(
    responses: aioresponses, genie: RitualsGenie
) -> None:
    """Test a hub response that isn't an object raises a response error."""
    mock_login(responses)
    responses.get(f"{API}account/hubs/{HUB_ONE}", status=200, payload=[])

    with pytest.raises(RitualsGenieResponseError):
        await genie.hub(HUB_ONE)


async def test_set_room_square_meters(
    responses: aioresponses, genie: RitualsGenie
) -> None:
    """Test setting the room size in m² sets the category as well."""
    square_meters_url = f"{API}hubs/{HUB_ONE}/attributes/squaremeterc"
    room_size_url = f"{API}hubs/{HUB_ONE}/attributes/roomc"
    mock_login(responses)
    responses.post(square_meters_url, status=201, payload={"value": "40"})
    responses.post(room_size_url, status=201, payload={"value": "3"})

    await genie.set_room_square_meters(HUB_ONE, 40)

    (square_meters,) = calls(responses, "POST", square_meters_url)
    (room_size,) = calls(responses, "POST", room_size_url)
    assert square_meters.kwargs["json"] == {"squaremeterc": "40"}
    assert room_size.kwargs["json"] == {"roomc": "3"}


async def test_set_room_square_meters_invalid(genie: RitualsGenie) -> None:
    """Test an impossible room size is refused before any request."""
    with pytest.raises(RitualsGenieValueError, match="'square_meters'"):
        await genie.set_room_square_meters(HUB_ONE, 0)


@pytest.mark.parametrize(
    ("square_meters", "room_size"),
    [
        (1, RoomSize.SMALL),
        (15, RoomSize.SMALL),
        (16, RoomSize.MEDIUM),
        (30, RoomSize.MEDIUM),
        (60, RoomSize.LARGE),
        (61, RoomSize.EXTRA_LARGE),
        (500, RoomSize.EXTRA_LARGE),
    ],
)
def test_room_size_for_area(square_meters: int, room_size: RoomSize) -> None:
    """Test the room size category a room falls in, like the app does it."""
    assert RoomSize.for_area(square_meters) is room_size


def test_hub_standby_led_and_generation() -> None:
    """Test the standby LED, room m² and hardware generation of a hub."""
    raw = json.loads(load_fixture("hub.json"))
    hub = RitualsGenieHub.from_dict(raw)

    assert hub.generation == 2
    assert hub.room_square_meters == 20
    assert hub.supports_standby_led
    assert hub.standby_led is True

    raw["attributeValues"]["ledc"] = "1"
    raw["attributes"]["ledc"] = False
    raw["deviceFlowVersion"] = 0
    hub = RitualsGenieHub.from_dict(raw)

    assert hub.standby_led is False
    assert not hub.supports_standby_led
    assert hub.generation == 1


def test_hub_without_led_or_generation() -> None:
    """Test a hub that reports neither LED nor generation."""
    hub = RitualsGenieHub.from_dict({"hash": HUB_ONE, "hublot": "LOT"})

    assert hub.standby_led is None
    assert hub.generation is None
    assert not hub.supports_standby_led


async def test_fresh_token_rejected_no_second_login(
    responses: aioresponses, genie: RitualsGenie
) -> None:
    """Test a 401 right after logging in doesn't log in once more."""
    mock_login(responses)
    responses.get(HUBS_URL, status=401)

    with pytest.raises(RitualsGenieResponseError) as excinfo:
        await genie.hubs()

    assert not isinstance(excinfo.value, RitualsGenieAuthenticationError)
    assert len(calls(responses, "POST", LOGIN_URL)) == 1
    assert len(calls(responses, "GET", HUBS_URL)) == 1


async def test_not_linked_skips_login(
    responses: aioresponses, genie: RitualsGenie
) -> None:
    """Test a 401 that isn't about the token doesn't trigger a login."""
    genie.token = "existing"
    responses.get(
        HUBS_URL,
        status=401,
        body=load_fixture("error_not_linked.json"),
        content_type="application/json",
    )

    with pytest.raises(RitualsGenieResponseError, match="not linked"):
        await genie.hubs()

    assert not calls(responses, "POST", LOGIN_URL)


def test_token_with_absurd_expiry(genie: RitualsGenie) -> None:
    """Test a token with an expiry beyond what datetime handles is left alone."""

    def encode(part: dict[str, object]) -> str:
        return base64.urlsafe_b64encode(json.dumps(part).encode()).decode().rstrip("=")

    token = f"{encode({'alg': 'none'})}.{encode({'exp': 10**20})}.signature"

    assert not token_expired(token, now=utcnow())
    assert genie is not None


@pytest.mark.parametrize(
    "hub",
    [
        {"hublot": "LOT"},
        {"hash": HUB_ONE, "hublot": "LOT", "firmwareInfo": "5.4"},
        {"hash": HUB_ONE, "hublot": "LOT", "status": "online"},
    ],
    ids=["missing_hash", "firmware_not_an_object", "status_not_a_number"],
)
async def test_hub_parse_error(
    responses: aioresponses, genie: RitualsGenie, hub: dict[str, object]
) -> None:
    """Test data that doesn't fit the model raises a library error."""
    mock_login(responses)
    responses.get(HUBS_URL, status=200, payload=[hub])

    with pytest.raises(RitualsGenieResponseError, match="Unexpected data"):
        await genie.hubs()


def test_hub_tolerates_null_and_partial_data() -> None:
    """Test null mappings and partial firmware info still parse."""
    hub = RitualsGenieHub.from_dict(
        {
            "hash": HUB_ONE,
            "hublot": "LOT",
            "attributeValues": None,
            "attributes": None,
            "sensors": None,
            "firmwareInfo": {"currentFirmware": "5.4"},
        }
    )

    assert hub.attribute_values == {}
    assert hub.supported_sensors == []
    assert hub.firmware is not None
    assert hub.firmware.current == "5.4"
    assert hub.firmware.newest is None
    assert not hub.firmware.update_available
    assert not hub.supports_standby_led


def test_sensors_unknown_states() -> None:
    """Test readings that don't tell are reported as unknown, not as False."""
    sensors = RitualsGenieSensors(
        battery=RitualsGenieSensor(title="Battery"),
        perfume=RitualsGenieSensor(title="Something", scent_found=False),
    )

    assert sensors.battery_charging is None
    assert sensors.has_cartridge is None


async def test_login_after_failed_login_is_retried(
    responses: aioresponses, genie: RitualsGenie, clock: Clock
) -> None:
    """Test a failed login is remembered for a while, then tried again."""
    responses.post(LOGIN_URL, exception=TimeoutError())
    mock_login(responses)
    mock_hubs(responses)

    with pytest.raises(RitualsGenieConnectionTimeoutError):
        await genie.hubs()

    # Right after, the same failure comes back without another attempt.
    clock.advance(seconds=29)
    with pytest.raises(RitualsGenieConnectionTimeoutError):
        await genie.hubs()
    assert len(calls(responses, "POST", LOGIN_URL)) == 1

    clock.advance(seconds=2)
    assert len(await genie.hubs()) == 2
    assert len(calls(responses, "POST", LOGIN_URL)) == 2


@pytest.mark.parametrize(
    "call",
    [
        lambda genie: genie.turn_on("../account/hubs"),
        lambda genie: genie.hub("abc?x=1"),
        lambda genie: genie.hublot(""),
        lambda genie: genie.sensor(HUB_ONE, "nope"),
        lambda genie: genie.set_room_size_category(HUB_ONE, 7),
        lambda genie: genie.set_standby_led(HUB_ONE, enabled="yes"),
    ],
    ids=["path_traversal", "query", "empty", "sensor", "room_size", "not_a_bool"],
)
async def test_invalid_arguments(
    responses: aioresponses,
    genie: RitualsGenie,
    call: Callable[[RitualsGenie], Awaitable[object]],
) -> None:
    """Test invalid arguments are refused before anything is sent."""
    with pytest.raises(RitualsGenieValueError) as excinfo:
        await call(genie)

    assert isinstance(excinfo.value, ValueError)
    assert not responses.requests


async def test_arguments_are_coerced(
    responses: aioresponses, genie: RitualsGenie
) -> None:
    """Test plain values are accepted where an enum is expected."""
    url = f"{API}hubs/{HUB_ONE}/attributes/roomc"
    mock_login(responses)
    responses.post(url, status=201, payload={"value": "3"})

    # Deliberately not a RoomSize: the validation coerces it into one.
    await genie.set_room_size_category(HUB_ONE, 3)  # ty: ignore[invalid-argument-type]

    (request,) = calls(responses, "POST", url)
    assert request.kwargs["json"] == {"roomc": "3"}


async def test_set_room_square_meters_stops_after_failed_first_write(
    responses: aioresponses, genie: RitualsGenie
) -> None:
    """Test the category isn't set when setting the square meters failed."""
    mock_login(responses)
    responses.post(f"{API}hubs/{HUB_ONE}/attributes/squaremeterc", status=500)

    with pytest.raises(RitualsGenieResponseError):
        await genie.set_room_square_meters(HUB_ONE, 40)

    assert not calls(responses, "POST", f"{API}hubs/{HUB_ONE}/attributes/roomc")


async def test_set_room_square_meters_reports_failed_second_write(
    responses: aioresponses, genie: RitualsGenie
) -> None:
    """Test a failure of the second write isn't swallowed."""
    mock_login(responses)
    responses.post(f"{API}hubs/{HUB_ONE}/attributes/squaremeterc", status=201)
    responses.post(f"{API}hubs/{HUB_ONE}/attributes/roomc", status=500)

    with pytest.raises(RitualsGenieResponseError) as excinfo:
        await genie.set_room_square_meters(HUB_ONE, 40)

    assert excinfo.value.status == 500


async def test_room_size_changes_do_not_interleave(
    responses: aioresponses, genie: RitualsGenie
) -> None:
    """Test overlapping room size changes of a diffuser are done one by one."""
    written: list[tuple[str, str]] = []
    first_write = asyncio.Event()

    async def record(_url: URL, **kwargs: Any) -> CallbackResult:
        ((attribute, value),) = kwargs["json"].items()
        written.append((attribute, value))
        if not first_write.is_set():
            first_write.set()
            await asyncio.sleep(0.01)
        return CallbackResult(status=201, payload={"value": value})

    mock_login(responses)
    for attribute in ("squaremeterc", "roomc"):
        responses.post(
            f"{API}hubs/{HUB_ONE}/attributes/{attribute}", callback=record, repeat=True
        )

    await asyncio.gather(
        genie.set_room_square_meters(HUB_ONE, 10),
        genie.set_room_square_meters(HUB_ONE, 100),
    )

    assert written == [
        ("squaremeterc", "10"),
        ("roomc", "1"),
        ("squaremeterc", "100"),
        ("roomc", "4"),
    ]


@pytest.mark.parametrize("identifier", [".", "..", "%2e%2e", "a\\b", "a%2Fb"])
async def test_identifiers_that_change_the_path(
    responses: aioresponses, genie: RitualsGenie, identifier: str
) -> None:
    """Test dot segments and escapes can't steer a request elsewhere."""
    with pytest.raises(RitualsGenieValueError):
        await genie.hub(identifier)

    assert not responses.requests


async def test_sensors_only(responses: aioresponses, genie: RitualsGenie) -> None:
    """Test only the requested sensors are fetched."""
    mock_login(responses)
    mock_hubs(responses)
    responses.get(
        f"{API}hubs/{HUB_ONE}/sensors/fillc",
        status=200,
        body=load_fixture("sensor_fill.json"),
        content_type="application/json",
    )

    woonkamer, _ = await genie.hubs()
    sensors = await genie.sensors(woonkamer, only=["fillc", Sensor.BATTERY])

    assert sensors.fill_level == "70-80%"
    assert sensors.generation == 2
    assert sensors.perfume is None
    # The battery was asked for, but this diffuser doesn't have one.
    assert sensors.battery is None
    assert len([key for key in responses.requests if "sensors" in str(key[1])]) == 1


async def test_sensors_only_unknown(genie: RitualsGenie) -> None:
    """Test asking for a sensor that doesn't exist is refused."""
    woonkamer = RitualsGenieHub.from_json(load_fixture("hub.json"))

    with pytest.raises(RitualsGenieValueError, match="Unknown sensor"):
        await genie.sensors(woonkamer, only=["smellc"])


async def test_response_error_status(
    responses: aioresponses, genie: RitualsGenie
) -> None:
    """Test the HTTP status is available, so callers don't match on text."""
    mock_login(responses)
    responses.post(
        f"{API}hubs/{HUB_ONE}/update",
        status=409,
        payload="The Hub already has the latest version",
    )

    with pytest.raises(RitualsGenieResponseError) as excinfo:
        await genie.update_firmware(HUB_ONE)

    assert excinfo.value.status == 409


async def test_parse_error_does_not_leak_values(
    responses: aioresponses, genie: RitualsGenie
) -> None:
    """Test a parse error names the field, not what someone named their room."""
    mock_login(responses)
    responses.get(
        HUBS_URL,
        status=200,
        payload=[{"hash": HUB_ONE, "hublot": "LOT", "status": "Bedroom of Frenck"}],
    )

    with pytest.raises(RitualsGenieResponseError) as excinfo:
        await genie.hubs()

    assert "'status'" in str(excinfo.value)
    assert "Frenck" not in str(excinfo.value)
    assert excinfo.value.args == (str(excinfo.value),)


async def test_empty_success_body(responses: aioresponses, genie: RitualsGenie) -> None:
    """Test a write answered with an empty body is a success, not bad JSON."""
    url = f"{API}hubs/{HUB_ONE}/attributes/fanc"
    mock_login(responses)
    responses.post(url, status=201, body="", content_type="application/json")

    await genie.turn_on(HUB_ONE)

    assert len(calls(responses, "POST", url)) == 1
