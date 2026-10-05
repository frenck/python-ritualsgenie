"""Tests for logging in, sharing logins, and backing off from the API."""

# pylint: disable=protected-access
from __future__ import annotations

import asyncio
from datetime import timedelta
from typing import TYPE_CHECKING, Any

import pytest
from aioresponses import CallbackResult, aioresponses

from ritualsgenie import (
    RitualsGenie,
    RitualsGenieAuthenticationError,
    RitualsGenieRateLimitError,
    RitualsGenieResponseError,
)

from .conftest import (
    API,
    HUB_ONE,
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

if TYPE_CHECKING:
    from yarl import URL


class HeldLogin:
    """A login response the test releases by hand.

    Against the real API a login takes a moment, and every request that
    needs a token meanwhile waits on it. Holding the response makes that
    overlap happen for sure, instead of leaving it to luck.
    """

    def __init__(self, payload: dict[str, Any]) -> None:
        """Initialize with the payload to answer once released."""
        self.payload = payload
        self.released = asyncio.Event()
        self.waiting = asyncio.Event()

    async def respond(self, _url: URL, **_kwargs: Any) -> CallbackResult:
        """Answer the login, once released.

        A plain method, not __call__: aioresponses only awaits callbacks it
        recognizes as coroutine functions.
        """
        self.waiting.set()
        await self.released.wait()
        return CallbackResult(status=200, payload=self.payload)


async def settle() -> None:
    """Let every task that can run, run until it waits."""
    for _ in range(10):
        await asyncio.sleep(0)


async def test_overlapping_requests_share_one_login(
    responses: aioresponses, genie: RitualsGenie
) -> None:
    """Test requests that need a token while logging in all wait on it."""
    login = HeldLogin({"success": TOKEN})
    responses.post(LOGIN_URL, callback=login.respond, repeat=True)
    mock_hubs(responses)
    mock_hubs(responses)
    mock_hubs(responses)

    tasks = [asyncio.create_task(genie.hubs()) for _ in range(3)]
    await login.waiting.wait()
    await settle()
    login.released.set()

    results = await asyncio.gather(*tasks)

    assert all(len(hubs) == 2 for hubs in results)
    assert len(calls(responses, "POST", LOGIN_URL)) == 1
    assert all(
        request.kwargs["headers"]["Authorization"] == TOKEN
        for request in calls(responses, "GET", HUBS_URL)
    )


async def test_overlapping_logins_are_shared(
    responses: aioresponses, genie: RitualsGenie
) -> None:
    """Test explicit logins that overlap share a single attempt."""
    login = HeldLogin({"success": TOKEN})
    responses.post(LOGIN_URL, callback=login.respond, repeat=True)

    tasks = [asyncio.create_task(genie.login()) for _ in range(3)]
    await login.waiting.wait()
    await settle()
    login.released.set()
    await asyncio.gather(*tasks)

    assert len(calls(responses, "POST", LOGIN_URL)) == 1


async def test_fresh_token_rejected_by_all_waiters(
    responses: aioresponses, genie: RitualsGenie
) -> None:
    """Test requests that waited on a login don't log in again on a 401.

    They all got a token that was just issued, so a 401 can't be about it.
    """
    login = HeldLogin({"success": TOKEN})
    responses.post(LOGIN_URL, callback=login.respond, repeat=True)
    responses.get(
        HUBS_URL, status=401, payload={"message": "Access denied"}, repeat=True
    )

    tasks = [asyncio.create_task(genie.hubs()) for _ in range(3)]
    await login.waiting.wait()
    await settle()
    login.released.set()

    results = await asyncio.gather(*tasks, return_exceptions=True)

    errors = [
        result for result in results if isinstance(result, RitualsGenieResponseError)
    ]
    assert len(errors) == 3
    assert all(error.status == 401 for error in errors)
    assert len(calls(responses, "POST", LOGIN_URL)) == 1


async def test_failed_login_is_shared_with_late_requests(
    responses: aioresponses, genie: RitualsGenie
) -> None:
    """Test a request that hits a 401 after a failed login doesn't log in again."""
    genie.token = "stale"
    responses.get(HUBS_URL, status=401, repeat=True)
    responses.post(LOGIN_URL, status=200, payload={"success": False}, repeat=True)

    with pytest.raises(RitualsGenieAuthenticationError):
        await genie.hubs()

    # Its 401 arrives after the first login already failed.
    with pytest.raises(RitualsGenieAuthenticationError):
        await genie.hubs()

    assert len(calls(responses, "POST", LOGIN_URL)) == 1


async def test_explicit_login_ignores_recent_failure(
    responses: aioresponses, genie: RitualsGenie
) -> None:
    """Test login() tries again, like a config flow with a new password does."""
    responses.post(LOGIN_URL, status=200, payload={"success": False})
    mock_login(responses)

    with pytest.raises(RitualsGenieAuthenticationError):
        await genie.login()

    await genie.login()

    assert genie.token == TOKEN
    assert len(calls(responses, "POST", LOGIN_URL)) == 2


async def test_cancelled_waiter_does_not_cancel_the_login(
    responses: aioresponses, genie: RitualsGenie
) -> None:
    """Test one cancelled request doesn't take the login from the others."""
    login = HeldLogin({"success": TOKEN})
    responses.post(LOGIN_URL, callback=login.respond, repeat=True)
    mock_hubs(responses)

    cancelled = asyncio.create_task(genie.hubs())
    waiting = asyncio.create_task(genie.hubs())
    await login.waiting.wait()
    await settle()

    cancelled.cancel()
    await settle()
    login.released.set()

    with pytest.raises(asyncio.CancelledError):
        await cancelled

    assert len(await waiting) == 2
    assert genie.token == TOKEN
    assert len(calls(responses, "POST", LOGIN_URL)) == 1


async def test_cancelled_login_is_not_remembered_as_failure(
    responses: aioresponses, genie: RitualsGenie
) -> None:
    """Test a login stopped by close() doesn't block the next login."""
    login = HeldLogin({"success": TOKEN})
    responses.post(LOGIN_URL, callback=login.respond)
    mock_login(responses)

    pending = asyncio.create_task(genie.login())
    await login.waiting.wait()

    await genie.close()

    with pytest.raises(asyncio.CancelledError):
        await pending

    assert genie._login_failure is None

    # The mock of the cancelled login may be reused; let it answer this time.
    login.released.set()
    await genie.login()
    assert genie.token == TOKEN


async def test_token_expiry_margin(
    responses: aioresponses, genie: RitualsGenie, clock: Clock
) -> None:
    """Test a token is replaced a minute before it expires, not later."""
    issued = make_token(expires_in=timedelta(hours=1), now=clock.now)
    mock_login(responses, token=issued)
    mock_login(responses)
    for _ in range(3):
        mock_hubs(responses)

    await genie.hubs()

    clock.advance(minutes=58, seconds=59)
    await genie.hubs()
    assert len(calls(responses, "POST", LOGIN_URL)) == 1

    clock.advance(seconds=1)
    await genie.hubs()
    assert len(calls(responses, "POST", LOGIN_URL)) == 2
    assert genie.token == TOKEN


async def test_lockout_lifecycle(
    responses: aioresponses, genie: RitualsGenie, clock: Clock
) -> None:
    """Test no login is tried during a lockout, and one is right after."""
    responses.post(
        LOGIN_URL,
        status=200,
        body=load_fixture("error_login_locked_out.json"),
        content_type="application/json",
    )
    mock_login(responses)

    with pytest.raises(RitualsGenieRateLimitError):
        await genie.login()

    clock.advance(seconds=1295)
    with pytest.raises(RitualsGenieRateLimitError) as excinfo:
        await genie.login()
    assert excinfo.value.retry_after == 1
    assert len(calls(responses, "POST", LOGIN_URL)) == 1

    clock.advance(seconds=1)
    await genie.login()
    assert genie.token == TOKEN


async def test_rate_limit_is_remembered(
    responses: aioresponses, genie: RitualsGenie, clock: Clock
) -> None:
    """Test nothing is sent while the API told us to back off."""
    mock_login(responses)
    responses.get(HUBS_URL, status=429, headers={"Retry-After": "120"})
    mock_hubs(responses)

    with pytest.raises(RitualsGenieRateLimitError) as excinfo:
        await genie.hubs()
    assert excinfo.value.status == 429

    clock.advance(seconds=60)
    with pytest.raises(RitualsGenieRateLimitError) as excinfo:
        await genie.turn_on(HUB_ONE)
    assert excinfo.value.retry_after == 60
    assert len(calls(responses, "GET", HUBS_URL)) == 1

    clock.advance(seconds=60)
    assert len(await genie.hubs()) == 2


async def test_rate_limit_without_retry_after_is_not_remembered(
    responses: aioresponses, genie: RitualsGenie
) -> None:
    """Test a 429 without a known wait doesn't block anything afterwards."""
    mock_login(responses)
    responses.get(HUBS_URL, status=429)
    mock_hubs(responses)

    with pytest.raises(RitualsGenieRateLimitError):
        await genie.hubs()

    assert len(await genie.hubs()) == 2


async def test_redirects_are_not_followed(
    responses: aioresponses, genie: RitualsGenie
) -> None:
    """Test a redirect isn't followed, so the password can't go along with it."""
    responses.post(
        LOGIN_URL, status=307, headers={"Location": "https://example.com/steal"}
    )

    with pytest.raises(RitualsGenieResponseError) as excinfo:
        await genie.login()

    assert excinfo.value.status == 307
    (request,) = calls(responses, "POST", LOGIN_URL)
    assert request.kwargs["allow_redirects"] is False


async def test_request_timeout_is_used(responses: aioresponses) -> None:
    """Test the configured timeout reaches the request."""
    mock_login(responses)

    async with RitualsGenie(
        email="user@example.com", password="secret", request_timeout=3
    ) as genie:
        await genie.login()

    (request,) = calls(responses, "POST", LOGIN_URL)
    assert request.kwargs["timeout"].total == 3


async def test_control_retry_keeps_the_request(
    responses: aioresponses, genie: RitualsGenie
) -> None:
    """Test a retried command sends the same body, with only a new token."""
    url = f"{API}hubs/{HUB_ONE}/attributes/speedc"
    genie.token = "stale"
    responses.post(url, status=401)
    mock_login(responses)
    responses.post(url, status=201, payload={"value": "2"})

    await genie.set_perfume_amount(HUB_ONE, 2)

    first, retry = calls(responses, "POST", url)
    assert first.kwargs["json"] == retry.kwargs["json"] == {"speedc": "2"}
    assert first.kwargs["headers"]["Authorization"] == "stale"
    assert retry.kwargs["headers"]["Authorization"] == TOKEN
