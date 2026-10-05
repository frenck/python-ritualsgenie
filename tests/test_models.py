"""Tests for how the models read what the API sends."""

from __future__ import annotations

import pytest
from mashumaro.exceptions import MissingField

from ritualsgenie import (
    RitualsGenieFirmware,
    RitualsGenieHub,
    RitualsGenieSensor,
    RitualsGenieSensors,
)

from .conftest import HUB_ONE


def hub(**fields: object) -> RitualsGenieHub:
    """Build a hub with identifiers and the given fields."""
    return RitualsGenieHub.from_dict({"hash": HUB_ONE, "hublot": "LOT", **fields})


def test_null_attribute_values_are_unknown() -> None:
    """Test a single null value doesn't turn into the string "None"."""
    parsed = hub(attributeValues={"fanc": None, "roomnamec": None, "speedc": "2"})

    assert parsed.attribute_values == {"speedc": "2"}
    assert parsed.is_on is None
    assert parsed.name is None


@pytest.mark.parametrize(
    "identifiers",
    [
        {"hash": None, "hublot": "LOT"},
        {"hash": HUB_ONE, "hublot": None},
        {"hash": "", "hublot": "LOT"},
        {"hash": 123, "hublot": "LOT"},
    ],
)
def test_hub_without_usable_identifiers(identifiers: dict[str, object]) -> None:
    """Test a hub without real identifiers fails to parse."""
    with pytest.raises(MissingField):
        RitualsGenieHub.from_dict(identifiers)


@pytest.mark.parametrize(
    ("fields", "property_name"),
    [
        ({"status": 2}, "is_online"),
        ({"attributeValues": {"fanc": "2"}}, "is_on"),
        ({"attributeValues": {"ledc": "2"}}, "standby_led"),
    ],
)
def test_unknown_states_are_none(fields: dict[str, object], property_name: str) -> None:
    """Test a value we don't know is unknown, not a definite no."""
    assert getattr(hub(**fields), property_name) is None


@pytest.mark.parametrize(
    ("fields", "property_name", "expected"),
    [
        ({"status": 1}, "is_online", True),
        ({"status": 0}, "is_online", False),
        ({"attributeValues": {"fanc": "1"}}, "is_on", True),
        ({"attributeValues": {"fanc": "0"}}, "is_on", False),
        ({"attributeValues": {"ledc": "0"}}, "standby_led", True),
        ({"attributeValues": {"ledc": "1"}}, "standby_led", False),
    ],
)
def test_known_states(
    fields: dict[str, object], property_name: str, *, expected: bool
) -> None:
    """Test the values we do know, including the inverted LED."""
    assert getattr(hub(**fields), property_name) is expected


@pytest.mark.parametrize(
    ("battery", "charging", "percentage"),
    [
        (RitualsGenieSensor(icon="battery-charge.png"), True, None),
        (RitualsGenieSensor(id=21), True, None),
        (RitualsGenieSensor(icon="battery-full.png"), False, 100),
        (RitualsGenieSensor(icon="Battery-75.png"), False, 50),
        (RitualsGenieSensor(icon="battery-50.png"), False, 25),
        (RitualsGenieSensor(icon="battery-low.png"), False, 10),
        (RitualsGenieSensor(id=5), False, None),
        (RitualsGenieSensor(icon="battery-new.png"), None, None),
        (RitualsGenieSensor(), None, None),
    ],
)
def test_battery(
    battery: RitualsGenieSensor, *, charging: bool | None, percentage: int | None
) -> None:
    """Test charging and level, from the icons inherited from pyrituals.

    None of these icons have been seen on a current device yet; this pins
    down the interpretation, not the API.
    """
    sensors = RitualsGenieSensors(battery=battery)

    assert sensors.battery_charging is charging
    assert sensors.battery_percentage == percentage


@pytest.mark.parametrize(
    ("firmware", "expected"),
    [
        ({"currentFirmwareId": 101, "newestFirmwareId": 127}, True),
        ({"currentFirmwareId": 127, "newestFirmwareId": 127}, False),
        ({"currentFirmwareId": 127}, None),
        ({}, None),
    ],
)
def test_firmware_update_available(
    firmware: dict[str, int], *, expected: bool | None
) -> None:
    """Test an update is only claimed, or denied, when the IDs tell."""
    assert RitualsGenieFirmware.from_dict(firmware).update_available is expected


def test_round_trip() -> None:
    """Test a parsed hub serializes back into something that parses the same."""
    parsed = hub(
        status=1,
        attributeValues={"fanc": "1", "speedc": "2"},
        firmwareInfo={"currentFirmware": "5.4", "currentFirmwareId": 127},
        extraFunctions={"color": "gold"},
    )

    assert RitualsGenieHub.from_dict(parsed.to_dict()) == parsed


@pytest.mark.parametrize(
    ("raw", "title", "expected"),
    [
        # Real readings, all within the range the app showed for them.
        ("5818", "70-80%", 70),
        ("6437", "60-70%", 67),
        ("9458", "50-60%", 52),
        ("13760", "30-40%", 31),
        # The edges of the ranges.
        ("0", "90-100%", 100),
        ("2000", "90-100%", 90),
        ("2001", "80-90%", 89),
        ("20000", "0-10%", 0),
        ("25000", "Empty", 0),
        # Kept within the range of the title, whatever the raw value says.
        ("1000", "30-40%", 40),
        ("19000", "30-40%", 30),
        # No measurement, or nothing to go on.
        ("-1", "No measurement available", None),
        (None, "70-80%", None),
        ("lots", "70-80%", None),
    ],
)
def test_fill_percentage(raw: str | None, title: str, expected: int | None) -> None:
    """Test the estimate of the perfume left, from the use counter."""
    sensors = RitualsGenieSensors(fill=RitualsGenieSensor(raw=raw, title=title))

    assert sensors.fill_percentage == expected


def test_fill_percentage_without_sensor() -> None:
    """Test there is no estimate without a fill sensor."""
    assert RitualsGenieSensors().fill_percentage is None


@pytest.mark.parametrize(
    ("raw", "title", "expected"),
    [
        ("85", "80-90%", 85),
        ("100", "90-100%", 100),
        ("150", "90-100%", 100),
        ("85", "50-60%", 60),
    ],
)
def test_fill_percentage_level_sensor(raw: str, title: str, expected: int) -> None:
    """Test the 3rd generation reports a percentage, not a use counter."""
    sensors = RitualsGenieSensors(
        fill=RitualsGenieSensor(raw=raw, title=title), generation=3
    )

    assert sensors.fill_percentage == expected
