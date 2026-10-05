"""Common fixtures and helpers for Rituals Perfume Genie tests."""

from __future__ import annotations

import base64
import json
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

import aiohttp
import pytest
from aioresponses import aioresponses
from aioresponses import core as aioresponses_core
from yarl import URL

from ritualsgenie import RitualsGenie

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, Generator

AIOHTTP_REQUIRES_STREAM_WRITER = (
    "stream_writer" in aiohttp.ClientResponse.__init__.__code__.co_varnames
)


AIOHTTP_STREAM_WRITER = SimpleNamespace(output_size=0)


class AioresponsesClientResponse(aioresponses_core.ClientResponse):
    """Backwards-compatible ClientResponse for aioresponses."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        """Initialize and provide a stream_writer for aiohttp 3.14+."""
        kwargs.setdefault("stream_writer", AIOHTTP_STREAM_WRITER)
        super().__init__(*args, **kwargs)


@pytest.fixture(scope="session", autouse=True)
def setup_aioresponses_aiohttp_compat() -> Generator[None, None, None]:
    """Patch aioresponses ClientResponse for aiohttp compatibility in tests."""
    if not AIOHTTP_REQUIRES_STREAM_WRITER:
        yield
        return

    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(aioresponses_core, "ClientResponse", AioresponsesClientResponse)
    yield
    monkeypatch.undo()


FIXTURES_DIR = Path(__file__).parent / "fixtures"

API = "https://rituals.apiv2.sense-company.com/apiv2/"
LOGIN_URL = f"{API}account/token"
HUBS_URL = f"{API}account/hubs"
HUB_ONE = "1" * 64
HUB_TWO = "2" * 64
TOKEN = "a-token"


def mock_login(responses: aioresponses, token: str = TOKEN) -> None:
    """Mock a successful login."""
    responses.post(
        LOGIN_URL,
        status=200,
        payload={"success": token},
    )


def mock_hubs(responses: aioresponses, *, status: int = 200) -> None:
    """Mock the account hubs endpoint."""
    responses.get(
        HUBS_URL,
        status=status,
        body=load_fixture("hubs.json"),
        content_type="application/json",
    )


def calls(responses: aioresponses, method: str, url: str) -> list:
    """Return the requests made to an URL."""
    return responses.requests.get((method, URL(url)), [])


def load_fixture(name: str) -> str:
    """Load a fixture file by name."""
    return (FIXTURES_DIR / name).read_text(encoding="utf-8")


def make_token(*, expires_in: timedelta, now: datetime | None = None) -> str:
    """Build an unsigned JWT that expires after the given time."""

    def encode(part: dict[str, Any]) -> str:
        return base64.urlsafe_b64encode(json.dumps(part).encode()).decode().rstrip("=")

    expires_at = int(((now or datetime.now(tz=UTC)) + expires_in).timestamp())
    return f"{encode({'alg': 'none'})}.{encode({'exp': expires_at})}.signature"


@dataclass
class Clock:
    """A clock the tests move by hand."""

    now: datetime = field(default_factory=lambda: datetime(2026, 10, 5, tzinfo=UTC))

    def advance(self, **delta: float) -> None:
        """Move the clock forward."""
        self.now += timedelta(**delta)


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> Clock:
    """Replace the clock of the client with one the test controls."""
    controlled = Clock()
    monkeypatch.setattr("ritualsgenie.ritualsgenie.utcnow", lambda: controlled.now)
    return controlled


@pytest.fixture
def responses() -> Generator[aioresponses, None, None]:
    """Yield an aioresponses instance that patches aiohttp client sessions."""
    with aioresponses() as mocker:
        yield mocker


@pytest.fixture
async def genie() -> AsyncGenerator[RitualsGenie, None]:
    """Yield a Rituals Perfume Genie client wired with default settings."""
    async with aiohttp.ClientSession() as session:
        yield RitualsGenie(email="user@example.com", password="secret", session=session)
