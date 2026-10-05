"""Helpers for the Rituals Perfume Genie API client."""

from __future__ import annotations

import base64
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import orjson
from mashumaro.exceptions import InvalidFieldValue, MissingField

from .const import (
    NOT_TOKEN_RELATED_MESSAGES,
    PERCENTAGE_RANGE_PATTERN,
    TOKEN_EXPIRY_MARGIN,
    Sensor,
)
from .exceptions import RitualsGenieResponseError, RitualsGenieValueError

if TYPE_CHECKING:
    from .models import BaseModel


def utcnow() -> datetime:
    """Return the current time; a function, so tests can move the clock."""
    return datetime.now(tz=UTC)


def as_int(value: str | None) -> int | None:
    """Convert an API value to an integer, None if it isn't one."""
    if value is None:
        return None

    try:
        return int(value)
    except ValueError:
        return None


def as_bool(value: str | None, *, on: str, off: str) -> bool | None:
    """Convert an API value to a boolean, None if it is neither on nor off."""
    if value is None:
        return None

    return {on: True, off: False}.get(value)


def as_sensor(value: Sensor | str) -> Sensor:
    """Convert a sensor name into a sensor, raising a library error if unknown."""
    try:
        return Sensor(value)
    except ValueError as exception:
        msg = f"Unknown sensor: {value!r}"
        raise RitualsGenieValueError(msg) from exception


def percentage_range(title: str | None) -> tuple[int, int] | None:
    """Return the range of a title like "70-80%", if it is one."""
    if title is None:
        return None

    if not (match := PERCENTAGE_RANGE_PATTERN.fullmatch(title)):
        return None

    return int(match.group("low")), int(match.group("high"))


def error_message(body: bytes) -> str | None:
    """Extract the message of an error response: a "message", "error" or string."""
    try:
        data = orjson.loads(body)  # pylint: disable=no-member
    except ValueError:
        return None

    if isinstance(data, str):
        return data

    if not isinstance(data, dict):
        return None

    message = data.get("message") or data.get("error")
    return message if isinstance(message, str) else None


def not_about_the_token(message: str) -> bool:
    """Return if a 401 is about something else than the token."""
    message = message.casefold()
    return any(known in message for known in NOT_TOKEN_RELATED_MESSAGES)


def parse_model[ModelT: BaseModel](model: type[ModelT], data: Any) -> ModelT:
    """Parse API data into a model, raising a library error if it doesn't fit.

    Names the field, never the value: that may be someone's room name.
    """
    if not isinstance(data, dict):
        msg = "Unexpected response from the Rituals Perfume Genie API"
        raise RitualsGenieResponseError(msg)

    try:
        return model.from_dict(data)
    except (InvalidFieldValue, MissingField) as exception:
        msg = (
            "Unexpected data from the Rituals Perfume Genie API"
            f" in field {exception.field_name!r}"
        )
        raise RitualsGenieResponseError(msg) from exception


def token_expired(token: str, *, now: datetime) -> bool:
    """Return if a token (a JWT, valid for an hour) is about to expire.

    Only peeks at the expiry, no signature check. An unreadable token counts
    as valid; the server will reject it if it isn't.
    """
    try:
        payload = token.split(".")[1]
        claims = orjson.loads(  # pylint: disable=no-member
            base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4))
        )
        expires_at = datetime.fromtimestamp(int(claims["exp"]), tz=UTC)
    except (IndexError, KeyError, OSError, OverflowError, TypeError, ValueError):
        return False

    return now >= expires_at - TOKEN_EXPIRY_MARGIN
