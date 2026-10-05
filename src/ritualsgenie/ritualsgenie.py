"""Asynchronous Python client for the Rituals Perfume Genie API."""

from __future__ import annotations

import asyncio
import logging
import math
import socket
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Annotated, Any, Self

import aiohttp
import orjson
from probatio import Match, Range, probatio
from yarl import URL

from .const import (
    API_BASE_URL,
    IDENTIFIER_PATTERN,
    LOGIN_FAILURE_MEMORY,
    LOGIN_FAILURE_STATUSES,
    LOGIN_LOCKOUT_PATTERN,
    SENSOR_FIELDS,
    USER_AGENT,
    Attribute,
    RoomSize,
    Sensor,
)
from .exceptions import (
    RitualsGenieAuthenticationError,
    RitualsGenieConnectionError,
    RitualsGenieConnectionTimeoutError,
    RitualsGenieError,
    RitualsGenieRateLimitError,
    RitualsGenieResponseError,
    RitualsGenieValueError,
)
from .models import (
    RitualsGenieAttribute,
    RitualsGenieHub,
    RitualsGenieHublot,
    RitualsGenieSensor,
    RitualsGenieSensors,
)
from .util import (
    as_int,
    as_sensor,
    error_message,
    not_about_the_token,
    parse_model,
    token_expired,
    utcnow,
)

if TYPE_CHECKING:
    from collections.abc import Iterable

_LOGGER = logging.getLogger(__name__)


@dataclass
# pylint: disable-next=too-many-instance-attributes
class RitualsGenie:
    """Main class for handling connections with the Rituals Perfume Genie API.

    The API is rate limited, logins even more so. So the client logs in
    lazily, reuses its token, shares one login between all requests that need
    one, and remembers rate limits and lockouts. Share one client per account.

    A session passed in is never closed by the client.
    """

    email: str
    password: str = field(repr=False)
    request_timeout: int = 10
    session: aiohttp.ClientSession | None = None
    token: str | None = field(default=None, repr=False)

    _close_session: bool = field(default=False, init=False)

    # The login in progress, and what we remember of earlier ones.
    _login_task: asyncio.Task[str] | None = field(default=None, init=False, repr=False)
    _login_failure: tuple[RitualsGenieError, datetime] | None = field(
        default=None, init=False, repr=False
    )
    _locked_out_until: datetime | None = field(default=None, init=False, repr=False)

    # Until when the API told us to back off.
    _rate_limited_until: datetime | None = field(default=None, init=False, repr=False)

    # Keeps the two writes of a room size change together, per diffuser.
    _room_locks: dict[str, asyncio.Lock] = field(
        default_factory=dict, init=False, repr=False
    )

    async def _request(
        self,
        uri: str,
        *,
        method: str = "GET",
        json: dict[str, Any] | None = None,
        token: str | None = None,
        auth_failure_statuses: frozenset[int] = frozenset({401}),
    ) -> Any:
        """Handle a request to the Rituals Perfume Genie API.

        Returns the decoded JSON body, or None when there is no JSON body.
        """
        self._raise_if_rate_limited()

        url = API_BASE_URL.join(URL(uri))

        if self.session is None:
            self.session = aiohttp.ClientSession()
            self._close_session = True

        headers = {
            "Accept": "application/json",
            "User-Agent": USER_AGENT,
        }

        # Without a "Bearer" prefix: that's what works, and what everyone sends.
        if token is not None:
            headers["Authorization"] = token

        _LOGGER.debug("%s %s", method, url)

        try:
            async with self.session.request(
                method,
                url,
                json=json,
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=self.request_timeout),
                # A redirect would take the password along. The API doesn't.
                allow_redirects=False,
            ) as response:
                _LOGGER.debug("%s %s returned %s", method, url, response.status)

                body = await response.read()
                return self._handle_response(response, body, auth_failure_statuses)

        except RitualsGenieError:  # pylint: disable=try-except-raise
            raise

        except TimeoutError as exception:
            _LOGGER.debug("Timeout connecting to %s", url)
            msg = "Timeout occurred while connecting to the Rituals Perfume Genie API"
            raise RitualsGenieConnectionTimeoutError(msg) from exception

        except (aiohttp.ClientError, socket.gaierror) as exception:
            _LOGGER.debug("Connection error for %s: %s", url, exception)
            msg = (
                "Error occurred while communicating with the Rituals Perfume Genie API"
            )
            raise RitualsGenieConnectionError(msg) from exception

    def _handle_response(
        self,
        response: aiohttp.ClientResponse,
        body: bytes,
        auth_failure_statuses: frozenset[int],
    ) -> Any:
        """Turn a response into its JSON body, or the matching error."""
        status = response.status

        if status in auth_failure_statuses:
            msg = error_message(body) or (
                "Authentication with the Rituals Perfume Genie API failed"
            )
            raise RitualsGenieAuthenticationError(msg)

        if status == 429:
            retry_after = as_int(response.headers.get("Retry-After"))
            if retry_after is not None:
                self._rate_limited_until = utcnow() + timedelta(seconds=retry_after)

            msg = error_message(body) or (
                "Rate limit of the Rituals Perfume Genie API exceeded"
            )
            raise RitualsGenieRateLimitError(
                msg, retry_after=retry_after, status=status
            )

        if status >= 300:
            msg = error_message(body) or (
                f"Error response ({status}) from the Rituals Perfume Genie API"
            )
            raise RitualsGenieResponseError(msg, status=status)

        # No body is no answer, whatever the content type claims.
        content_type = response.headers.get("Content-Type", "")
        if not body.strip() or "application/json" not in content_type:
            return None

        try:
            return orjson.loads(body)  # pylint: disable=no-member
        except orjson.JSONDecodeError as exception:  # pylint: disable=no-member
            msg = "Invalid JSON received from the Rituals Perfume Genie API"
            raise RitualsGenieResponseError(msg, status=status) from exception

    def _raise_if_rate_limited(self) -> None:
        """Refuse to send anything while the API told us to back off."""
        if self._rate_limited_until is None:
            return

        remaining = (self._rate_limited_until - utcnow()).total_seconds()
        if remaining <= 0:
            self._rate_limited_until = None
            return

        msg = "Rate limit of the Rituals Perfume Genie API exceeded, still waiting"
        raise RitualsGenieRateLimitError(msg, retry_after=math.ceil(remaining))

    async def _authenticated_request(
        self,
        uri: str,
        *,
        method: str = "GET",
        json: dict[str, Any] | None = None,
    ) -> Any:
        """Handle a request that needs a token, logging in when needed.

        A 401 gets one retry with a new token. A 401 on a token that was just
        obtained isn't about the token (the API also uses it for a diffuser
        that isn't linked), so that becomes a response error.
        """
        token, fresh = await self._valid_token()

        try:
            return await self._request(uri, method=method, json=json, token=token)
        except RitualsGenieAuthenticationError as exception:
            if fresh or not_about_the_token(str(exception)):
                raise RitualsGenieResponseError(
                    str(exception), status=401
                ) from exception

            _LOGGER.debug("Token rejected, logging in again")

        token, _ = await self._valid_token(rejected_token=token)

        try:
            return await self._request(uri, method=method, json=json, token=token)
        except RitualsGenieAuthenticationError as exception:
            raise RitualsGenieResponseError(str(exception), status=401) from exception

    async def _valid_token(
        self, *, rejected_token: str | None = None
    ) -> tuple[str, bool]:
        """Return a usable token, and whether a login was needed to get it."""
        if (
            self.token is not None
            and self.token != rejected_token
            and not token_expired(self.token, now=utcnow())
        ):
            return self.token, False

        return await self._shared_login(), True

    async def _shared_login(self, *, force: bool = False) -> str:
        """Log in, or join the login already in progress.

        Shielded, so one cancelled request doesn't cancel it for the others.
        """
        task = self._login_task

        if task is None:
            if not force:
                self._raise_recent_login_failure()

            task = self._login_task = asyncio.create_task(self._login())
            task.add_done_callback(self._login_finished)

        return await asyncio.shield(task)

    def _login_finished(self, task: asyncio.Task[str]) -> None:
        """Forget the login task once it is done."""
        self._login_task = None

        # Avoid "exception never retrieved" when every waiter was cancelled.
        if not task.cancelled():
            task.exception()

    def _raise_recent_login_failure(self) -> None:
        """Raise the failure of the last login, if it failed only just now."""
        if self._login_failure is None:
            return

        exception, failed_at = self._login_failure
        if utcnow() - failed_at < LOGIN_FAILURE_MEMORY:
            raise exception

        self._login_failure = None

    async def _login(self) -> str:
        """Log in and store the token."""
        try:
            token = await self._fetch_token()
        except RitualsGenieError as exception:
            self._login_failure = (exception, utcnow())
            raise

        self._login_failure = None
        self.token = token

        return token

    async def _fetch_token(self) -> str:
        """Exchange the email address and password for a token."""
        self._raise_if_locked_out()

        try:
            data = await self._request(
                "account/token",
                method="POST",
                json={"email": self.email, "password": self.password},
                auth_failure_statuses=LOGIN_FAILURE_STATUSES,
            )
        except RitualsGenieAuthenticationError as exception:
            self._raise_on_lockout(str(exception))
            raise

        if isinstance(data, dict) and isinstance(data.get("success"), str):
            return data["success"]

        # Only an explicit "success": false means a refused login; anything
        # else is no reason to ask the user for their password again.
        if not isinstance(data, dict) or data.get("success") is not False:
            msg = "Unexpected login response from the Rituals Perfume Genie API"
            raise RitualsGenieResponseError(msg)

        message = data.get("message")
        if not isinstance(message, str):
            message = "Invalid email address or password"

        self._raise_on_lockout(message)
        raise RitualsGenieAuthenticationError(message)

    def _raise_if_locked_out(self) -> None:
        """Refuse to log in while we are still locked out."""
        if self._locked_out_until is None:
            return

        remaining = (self._locked_out_until - utcnow()).total_seconds()
        if remaining <= 0:
            self._locked_out_until = None
            return

        msg = "Too many login attempts, still locked out"
        raise RitualsGenieRateLimitError(msg, retry_after=math.ceil(remaining))

    def _raise_on_lockout(self, message: str) -> None:
        """Raise a rate limit error if the message says we're locked out.

        A lockout looks exactly like a wrong password, except for the message.
        """
        if not (locked_out := LOGIN_LOCKOUT_PATTERN.search(message)):
            return

        retry_after = int(locked_out.group("seconds"))
        self._locked_out_until = utcnow() + timedelta(seconds=retry_after)

        raise RitualsGenieRateLimitError(message, retry_after=retry_after)

    async def login(self) -> None:
        """Log in to the Rituals Perfume Genie API.

        Not needed for requests, those log in by themselves. Useful to check
        credentials, like in a config flow. Always logs in, so every call
        counts towards the login lockout.
        """
        await self._shared_login(force=True)

    async def raw_hubs(self) -> list[dict[str, Any]]:
        """Return the raw API response of the account hubs endpoint.

        Useful for diagnostics, debugging, and fixture capture.
        """
        data = await self._authenticated_request("account/hubs")

        if not isinstance(data, list):
            msg = "Unexpected response from the Rituals Perfume Genie API"
            raise RitualsGenieResponseError(msg)

        return data

    async def hubs(self) -> list[RitualsGenieHub]:
        """Return all diffusers on the account, with their state, in one request."""
        return [parse_model(RitualsGenieHub, hub) for hub in await self.raw_hubs()]

    @probatio(error=RitualsGenieValueError)
    async def hub(
        self, hub_hash: Annotated[str, Match(IDENTIFIER_PATTERN)]
    ) -> RitualsGenieHub:
        """Return a single diffuser, including its current state."""
        data = await self._authenticated_request(f"account/hubs/{hub_hash}")
        return parse_model(RitualsGenieHub, data)

    @probatio(error=RitualsGenieValueError)
    async def hublot(
        self, hublot: Annotated[str, Match(IDENTIFIER_PATTERN)]
    ) -> RitualsGenieHublot:
        """Return the hardware details of a diffuser by its hublot.

        This is the only place the API tells which Genie model it is.
        """
        data = await self._authenticated_request(f"hublots/{hublot}")
        return parse_model(RitualsGenieHublot, data)

    @probatio(error=RitualsGenieValueError)
    async def raw_sensor(
        self,
        hub_hash: Annotated[str, Match(IDENTIFIER_PATTERN)],
        sensor: Sensor,
    ) -> Any:
        """Return the raw API response of a sensor endpoint."""
        return await self._authenticated_request(f"hubs/{hub_hash}/sensors/{sensor}")

    @probatio(error=RitualsGenieValueError)
    async def sensor(
        self,
        hub_hash: Annotated[str, Match(IDENTIFIER_PATTERN)],
        sensor: Sensor,
    ) -> RitualsGenieSensor:
        """Return a single sensor reading of a diffuser."""
        data = await self.raw_sensor(hub_hash, sensor)
        return parse_model(RitualsGenieSensor, data)

    async def sensors(
        self, hub: RitualsGenieHub, *, only: Iterable[Sensor | str] | None = None
    ) -> RitualsGenieSensors:
        """Return the sensor readings of a diffuser.

        Only supported sensors are requested, optionally narrowed down with
        `only`. That is a request per sensor, so poll these sparingly: Rituals
        has blocked Home Assistant over it. The firmware is in `hubs()`
        already, so the version sensor is never needed.
        """
        wanted = set(Sensor) if only is None else {as_sensor(item) for item in only}

        readings = {
            SENSOR_FIELDS[sensor]: await self.sensor(hub.hash, sensor)
            for sensor in hub.supported_sensors
            if sensor in wanted
        }

        return RitualsGenieSensors(generation=hub.generation, **readings)

    @probatio(error=RitualsGenieValueError)
    async def attribute(
        self,
        hub_hash: Annotated[str, Match(IDENTIFIER_PATTERN)],
        attribute: Attribute,
    ) -> RitualsGenieAttribute:
        """Return the value of a single attribute, with a description."""
        data = await self._authenticated_request(
            f"hubs/{hub_hash}/attributes/{attribute}"
        )
        return parse_model(RitualsGenieAttribute, data)

    async def _set_attribute(
        self, hub_hash: str, attribute: Attribute, value: str
    ) -> None:
        """Change an attribute of a diffuser.

        The answer echoes the value, but unreliably ("1023" has been seen), so
        it is ignored.
        """
        await self._authenticated_request(
            f"hubs/{hub_hash}/attributes/{attribute}",
            method="POST",
            json={attribute: value},
        )

    def _room_lock(self, hub_hash: str) -> asyncio.Lock:
        """Return the lock that keeps room size changes of a diffuser apart."""
        return self._room_locks.setdefault(hub_hash, asyncio.Lock())

    @probatio(error=RitualsGenieValueError)
    async def turn_on(
        self, hub_hash: Annotated[str, Match(IDENTIFIER_PATTERN)]
    ) -> None:
        """Turn on a diffuser."""
        await self._set_attribute(hub_hash, Attribute.FAN, "1")

    @probatio(error=RitualsGenieValueError)
    async def turn_off(
        self, hub_hash: Annotated[str, Match(IDENTIFIER_PATTERN)]
    ) -> None:
        """Turn off a diffuser."""
        await self._set_attribute(hub_hash, Attribute.FAN, "0")

    @probatio(error=RitualsGenieValueError)
    async def set_perfume_amount(
        self,
        hub_hash: Annotated[str, Match(IDENTIFIER_PATTERN)],
        amount: Annotated[int, Range(min=1, max=3)],
    ) -> None:
        """Set the perfume intensity of a diffuser, from 1 (low) to 3 (high)."""
        await self._set_attribute(hub_hash, Attribute.PERFUME_AMOUNT, str(amount))

    @probatio(error=RitualsGenieValueError)
    async def set_room_size_category(
        self,
        hub_hash: Annotated[str, Match(IDENTIFIER_PATTERN)],
        room_size: RoomSize,
    ) -> None:
        """Set the room size category, leaving the square meters untouched."""
        async with self._room_lock(hub_hash):
            await self._set_attribute(
                hub_hash, Attribute.ROOM_SIZE, str(int(room_size))
            )

    @probatio(error=RitualsGenieValueError)
    async def set_room_square_meters(
        self,
        hub_hash: Annotated[str, Match(IDENTIFIER_PATTERN)],
        square_meters: Annotated[int, Range(min=1)],
    ) -> None:
        """Set the room size in square meters, and the category that goes with it.

        The API doesn't derive one from the other. That's two requests, not
        atomic: if the second fails, call this again.
        """
        room_size = RoomSize.for_area(square_meters)

        async with self._room_lock(hub_hash):
            await self._set_attribute(
                hub_hash, Attribute.ROOM_SQUARE_METERS, str(square_meters)
            )
            await self._set_attribute(
                hub_hash, Attribute.ROOM_SIZE, str(int(room_size))
            )

    @probatio(error=RitualsGenieValueError)
    async def set_standby_led(
        self,
        hub_hash: Annotated[str, Match(IDENTIFIER_PATTERN)],
        *,
        enabled: bool,
    ) -> None:
        """Turn the LED that shows while the diffuser is on standby on or off.

        Check `RitualsGenieHub.supports_standby_led` first.
        """
        # Inverted on purpose: the attribute means "LED disabled".
        value = "0" if enabled else "1"
        await self._set_attribute(hub_hash, Attribute.STANDBY_LED, value)

    @probatio(error=RitualsGenieValueError)
    async def update_firmware(
        self, hub_hash: Annotated[str, Match(IDENTIFIER_PATTERN)]
    ) -> None:
        """Start a firmware update; a 409 means it already is the latest."""
        await self._authenticated_request(f"hubs/{hub_hash}/update", method="POST")

    async def close(self) -> None:
        """Stop a login in progress, and close the session if we created it."""
        if self._login_task is not None:
            self._login_task.cancel()

        if self.session and self._close_session:
            await self.session.close()

    async def __aenter__(self) -> Self:
        """Async enter."""
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        """Async exit."""
        await self.close()
