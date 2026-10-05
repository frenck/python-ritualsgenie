"""Constants for the Rituals Perfume Genie API."""

from __future__ import annotations

import re
from datetime import timedelta
from enum import IntEnum, StrEnum

from yarl import URL

API_BASE_URL = URL("https://rituals.apiv2.sense-company.com/apiv2/")

# Recognizable on purpose, so Rituals can throttle us instead of everyone.
USER_AGENT = "PythonRitualsGenie"

# Identifiers end up in the URL path; refuse anything that could change it.
IDENTIFIER_PATTERN = r"^(?!\.{1,2}$)[^/\\?#%\s]+$"


class Attribute(StrEnum):
    """Writable attributes of a Perfume Genie diffuser."""

    FAN = "fanc"
    """Diffuser on (1) or off (0)."""

    PERFUME_AMOUNT = "speedc"
    """Perfume intensity, from 1 (low) to 3 (high)."""

    ROOM_SIZE = "roomc"
    """Room size category, see RoomSize."""

    ROOM_NAME = "roomnamec"
    """User given name of the diffuser."""

    ROOM_SQUARE_METERS = "squaremeterc"
    """Room size in square meters, as shown in the app."""

    STANDBY_LED = "ledc"
    """Standby LED disabled (1) or enabled (0). Yes, inverted."""


class Sensor(StrEnum):
    """Sensors a Perfume Genie diffuser can report."""

    BATTERY = "battc"
    FILL = "fillc"
    PERFUME = "rfidc"
    VERSION = "versionc"
    WIFI = "wific"


# The field each sensor reading ends up in, in RitualsGenieSensors.
SENSOR_FIELDS: dict[Sensor, str] = {
    Sensor.BATTERY: "battery",
    Sensor.FILL: "fill",
    Sensor.PERFUME: "perfume",
    Sensor.VERSION: "version",
    Sensor.WIFI: "wifi",
}


class RoomSize(IntEnum):
    """Room size categories, as used by the Rituals app."""

    SMALL = 1
    MEDIUM = 2
    LARGE = 3
    EXTRA_LARGE = 4

    @property
    def square_meters(self) -> int:
        """Return the largest room, in square meters, this category covers."""
        return ROOM_SIZE_SQUARE_METERS[self]

    @classmethod
    def for_area(cls, square_meters: int) -> RoomSize:
        """Return the room size category a room of this size falls in."""
        for room_size, size in ROOM_SIZE_SQUARE_METERS.items():
            if square_meters <= size:
                return room_size

        return cls.EXTRA_LARGE


ROOM_SIZE_SQUARE_METERS: dict[RoomSize, int] = {
    RoomSize.SMALL: 15,
    RoomSize.MEDIUM: 30,
    RoomSize.LARGE: 60,
    RoomSize.EXTRA_LARGE: 100,
}


# --- Logging in ---

# Log in again before the token expires, not after a wasted 401.
TOKEN_EXPIRY_MARGIN = timedelta(minutes=1)

# A login that just failed won't succeed seconds later, and every attempt
# counts towards the lockout.
LOGIN_FAILURE_MEMORY = timedelta(seconds=30)

# Seen as: "Too many login attempts. Try again in 1296 seconds."
LOGIN_LOCKOUT_PATTERN = re.compile(
    r"too many login attempts.*?(?P<seconds>\d+) seconds", re.IGNORECASE
)

# Just in case: wrong credentials are a 200 with success false. Not 403 or
# 404 on purpose, those are a block or a moved endpoint, not a bad password.
LOGIN_FAILURE_STATUSES = frozenset({400, 401, 422})

# 401s that aren't about the token, like "This hub is not linked to the
# user's account". Logging in again won't help those.
NOT_TOKEN_RELATED_MESSAGES = ("not linked", "not your")


# --- Sensor readings ---

# Rough percentages per app icon, from pyrituals. Prefer the RSSI for WiFi;
# the battery ones are unverified. Charging is no level, so it's not in here.
BATTERY_ICON_PERCENTAGE: dict[str, int] = {
    "battery-full.png": 100,
    "Battery-75.png": 50,
    "battery-50.png": 25,
    "battery-low.png": 10,
}

WIFI_ICON_PERCENTAGE: dict[str, int] = {
    "icon-signal.png": 100,
    "icon-signal-75.png": 75,
    "icon-signal-low.png": 25,
    "icon-signal-0.png": 0,
}

BATTERY_CHARGING_ICON = "battery-charge.png"

# Raw values meaning "nothing there".
NO_CARTRIDGE_RAW = "0"
NO_FILL_MEASUREMENT_RAW = "-1"

# The fill level is a use counter on the cartridge, 10% per 2000 and empty
# beyond 20000 (per Rituals, in Echnics/Perfume-Genie-ESPhome#1).
FILL_USE_PER_PERCENT = 200

# A fill level title, like "70-80%".
PERCENTAGE_RANGE_PATTERN = re.compile(r"(?P<low>\d{1,3})\s*-\s*(?P<high>\d{1,3})\s*%")

# Magic IDs of the old API, from pyrituals; current responses have none.
BATTERY_CHARGING_ID = 21
NO_CARTRIDGE_ID = 19
