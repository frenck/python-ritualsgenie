"""Models for the Rituals Perfume Genie API."""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from awesomeversion import AwesomeVersion
from mashumaro import field_options
from mashumaro.config import BaseConfig
from mashumaro.mixins.orjson import DataClassORJSONMixin
from mashumaro.types import SerializationStrategy

from .const import (
    BATTERY_CHARGING_ICON,
    BATTERY_CHARGING_ID,
    BATTERY_ICON_PERCENTAGE,
    FILL_USE_PER_PERCENT,
    NO_CARTRIDGE_ID,
    NO_CARTRIDGE_RAW,
    NO_FILL_MEASUREMENT_RAW,
    WIFI_ICON_PERCENTAGE,
    Attribute,
    RoomSize,
    Sensor,
)
from .util import as_bool, as_int, percentage_range


class _AwesomeVersionStrategy(SerializationStrategy):
    """Serialize AwesomeVersion to/from string for mashumaro."""

    def serialize(self, value: AwesomeVersion | None) -> str | None:
        """Serialize to string."""
        return None if value is None else str(value)

    def deserialize(self, value: str | float | None) -> AwesomeVersion | None:
        """Deserialize from string or number."""
        return None if value is None else AwesomeVersion(str(value))


class _OptionalStringStrategy(SerializationStrategy):
    """Deserialize a string-or-number into a string, keeping None."""

    def serialize(self, value: str | None) -> str | None:
        """Serialize as is."""
        return value

    def deserialize(self, value: str | float | None) -> str | None:
        """Deserialize from string or number."""
        return None if value is None else str(value)


class BaseModel(DataClassORJSONMixin):
    """Base model for all Rituals Perfume Genie models."""

    # pylint: disable-next=too-few-public-methods
    class Config(BaseConfig):
        """Mashumaro configuration."""

        omit_none = True
        serialize_by_alias = True


@dataclass(kw_only=True)
class RitualsGenieFirmware(BaseModel):
    """Firmware information of a Perfume Genie diffuser."""

    current: AwesomeVersion | None = field(
        default=None,
        metadata=field_options(
            alias="currentFirmware",
            serialization_strategy=_AwesomeVersionStrategy(),
        ),
    )
    current_id: int | None = field(
        default=None, metadata=field_options(alias="currentFirmwareId")
    )

    newest: AwesomeVersion | None = field(
        default=None,
        metadata=field_options(
            alias="newestFirmware",
            serialization_strategy=_AwesomeVersionStrategy(),
        ),
    )
    newest_id: int | None = field(
        default=None, metadata=field_options(alias="newestFirmwareId")
    )

    @property
    def update_available(self) -> bool | None:
        """Return if Rituals has newer firmware available for this diffuser."""
        if self.current_id is None or self.newest_id is None:
            return None

        return self.newest_id > self.current_id


@dataclass(kw_only=True)
class RitualsGenieNextFanChange(BaseModel):
    """The next change in diffusing, as planned by the diffuser schedules."""

    current_state: str | None = field(
        default=None, metadata=field_options(alias="currentState")
    )
    next_state_change_time: str | None = field(
        default=None, metadata=field_options(alias="nextStateChangeTime")
    )
    next_state_change_value: int | None = field(
        default=None, metadata=field_options(alias="nextStateChangeValue")
    )


@dataclass(kw_only=True)
# pylint: disable-next=too-many-instance-attributes
class RitualsGenieHub(BaseModel):
    """A Perfume Genie diffuser; the API calls it a "hub".

    `attributes` and `sensors` tell what this diffuser supports,
    `attribute_values` holds the state. Plain mappings on purpose: the API
    adds keys over time, and parsing shouldn't break on that.
    """

    hash: str
    hublot: str

    attribute_values: dict[str, str] = field(
        default_factory=dict, metadata=field_options(alias="attributeValues")
    )
    attributes: dict[str, bool] = field(default_factory=dict)
    sensors: dict[str, bool] = field(default_factory=dict)

    color: str | None = None
    device_flow_version: int | None = field(
        default=None, metadata=field_options(alias="deviceFlowVersion")
    )
    firmware: RitualsGenieFirmware | None = field(
        default=None, metadata=field_options(alias="firmwareInfo")
    )
    status: int | None = None
    timezone: str | None = None

    has_schedules: bool | None = field(
        default=None, metadata=field_options(alias="hasSchedules")
    )
    next_fan_change: RitualsGenieNextFanChange | None = field(
        default=None, metadata=field_options(alias="nextFanChange")
    )

    @classmethod
    def __pre_deserialize__(cls, d: dict[str, object]) -> dict[str, object]:
        """Clean up the raw hub before parsing it."""
        # Null instead of empty mappings: drop them, so the defaults kick in.
        d = {
            key: value
            for key, value in d.items()
            if not (
                key in {"attributeValues", "attributes", "sensors"} and value is None
            )
        }

        # A null value would otherwise become the string "None".
        if isinstance(attribute_values := d.get("attributeValues"), dict):
            d["attributeValues"] = {
                key: value
                for key, value in attribute_values.items()
                if value is not None
            }

        # A hub without real identifiers is unusable; this makes parsing fail.
        for key in ("hash", "hublot"):
            if not isinstance(d.get(key), str) or not d[key]:
                d.pop(key, None)

        # The color is the only useful bit in extraFunctions.
        extra_functions = d.get("extraFunctions")
        if isinstance(extra_functions, dict) and "color" not in d:
            d = {**d, "color": extra_functions.get("color")}

        return d

    @property
    def name(self) -> str | None:
        """Return the user given name of the diffuser."""
        return self.attribute_values.get(Attribute.ROOM_NAME)

    @property
    def is_online(self) -> bool | None:
        """Return if the diffuser is connected to the Rituals cloud."""
        if self.status is None:
            return None

        return {1: True, 0: False}.get(self.status)

    @property
    def is_on(self) -> bool | None:
        """Return if the diffuser is diffusing."""
        return as_bool(self.attribute_values.get(Attribute.FAN), on="1", off="0")

    @property
    def perfume_amount(self) -> int | None:
        """Return the perfume intensity, from 1 (low) to 3 (high)."""
        return as_int(self.attribute_values.get(Attribute.PERFUME_AMOUNT))

    @property
    def room_size(self) -> RoomSize | None:
        """Return the room size category."""
        value = as_int(self.attribute_values.get(Attribute.ROOM_SIZE))
        if value is None:
            return None

        try:
            return RoomSize(value)
        except ValueError:
            return None

    @property
    def room_square_meters(self) -> int | None:
        """Return the room size in square meters, as shown in the app."""
        return as_int(self.attribute_values.get(Attribute.ROOM_SQUARE_METERS))

    @property
    def generation(self) -> int | None:
        """Return the hardware generation, like 2."""
        if self.device_flow_version is None:
            return None

        # Anything below 2 is the first generation.
        return max(self.device_flow_version, 1)

    @property
    def supports_standby_led(self) -> bool:
        """Return if the standby LED can be switched on and off."""
        return self.attributes.get(Attribute.STANDBY_LED, False)

    @property
    def standby_led(self) -> bool | None:
        """Return if the LED shows while the diffuser is on standby."""
        # Inverted on purpose: the attribute means "LED disabled".
        return as_bool(
            self.attribute_values.get(Attribute.STANDBY_LED), on="0", off="1"
        )

    @property
    def has_battery(self) -> bool:
        """Return if the diffuser has a battery (the portable Genie)."""
        return self.sensors.get(Sensor.BATTERY, False)

    @property
    def supported_sensors(self) -> list[Sensor]:
        """Return the sensors this diffuser reports to be capable of."""
        return [sensor for sensor in Sensor if self.sensors.get(sensor, False)]


@dataclass(kw_only=True)
class RitualsGenieHublot(BaseModel):
    """Hardware details of a diffuser, looked up by its hublot."""

    device_version: str | None = field(
        default=None, metadata=field_options(alias="deviceVersion")
    )
    """The model, like "Genie 2.1"."""

    color: str | None = None
    firmware_id: int | None = field(
        default=None, metadata=field_options(alias="firmwareId")
    )

    is_secure: bool | None = field(
        default=None, metadata=field_options(alias="isSecure")
    )
    """Not documented. Probably whether the firmware is encrypted."""

    add_device_flow_version: int | None = field(
        default=None, metadata=field_options(alias="addDeviceFlowVersion")
    )
    hub_hash: str | None = field(default=None, metadata=field_options(alias="hubHash"))
    hub_ssid: str | None = field(default=None, metadata=field_options(alias="hubSSID"))


@dataclass(kw_only=True)
class RitualsGenieAttribute(BaseModel):
    """A single attribute value, with a human readable description."""

    description: str | None = None
    """Like "Medium fan speed"."""

    value: str | None = field(
        default=None,
        metadata=field_options(serialization_strategy=_OptionalStringStrategy()),
    )


@dataclass(kw_only=True)
# pylint: disable-next=too-many-instance-attributes
class RitualsGenieSensor(BaseModel):
    """A single sensor reading of a Perfume Genie diffuser.

    `title` is for humans ("70-80%", the perfume name), `raw` is what the
    diffuser reported (RSSI in dBm, the perfume code, the fill use counter).
    `id` and `value` are from older API versions. All optional on purpose.
    """

    title: str | None = None
    raw: str | None = field(
        default=None,
        metadata=field_options(serialization_strategy=_OptionalStringStrategy()),
    )

    description: str | None = None
    icon: str | None = None
    image: str | None = None

    discover_url: str | None = None
    """For a cartridge, the ID of the perfume product in the Rituals shop."""

    scent_found: bool | None = field(
        default=None, metadata=field_options(alias="scentFound")
    )
    """For a cartridge, if the RFID tag was recognized as a known perfume."""

    id: int | None = None
    value: str | None = field(
        default=None,
        metadata=field_options(serialization_strategy=_OptionalStringStrategy()),
    )


@dataclass(kw_only=True)
class RitualsGenieSensors:
    """All sensor readings of a single Perfume Genie diffuser.

    A sensor is None when unsupported or not requested. The generation is
    kept along, as it changes what some readings mean.
    """

    battery: RitualsGenieSensor | None = None
    fill: RitualsGenieSensor | None = None
    perfume: RitualsGenieSensor | None = None
    version: RitualsGenieSensor | None = None
    wifi: RitualsGenieSensor | None = None

    generation: int | None = None

    @property
    def battery_charging(self) -> bool | None:
        """Return if the battery is charging."""
        if self.battery is None:
            return None

        if (
            self.battery.icon == BATTERY_CHARGING_ICON
            or self.battery.id == BATTERY_CHARGING_ID
        ):
            return True

        # Only a known level icon, or another ID, says it's not charging.
        if self.battery.icon in BATTERY_ICON_PERCENTAGE or self.battery.id is not None:
            return False

        return None

    @property
    def battery_percentage(self) -> int | None:
        """Return the (rough) battery level, derived from the app icon."""
        if self.battery is None or self.battery.icon is None:
            return None

        return BATTERY_ICON_PERCENTAGE.get(self.battery.icon)

    @property
    def has_cartridge(self) -> bool | None:
        """Return if a cartridge is inserted (not necessarily a known perfume)."""
        if self.perfume is None:
            return None

        if self.perfume.raw is not None:
            return self.perfume.raw != NO_CARTRIDGE_RAW

        if self.perfume.id is not None:
            return self.perfume.id != NO_CARTRIDGE_ID

        # A known perfume is surely inserted; otherwise we can't tell.
        return True if self.perfume.scent_found else None

    @property
    def perfume_name(self) -> str | None:
        """Return the perfume name; without a cartridge the title is a message."""
        if self.perfume is None or self.has_cartridge is False:
            return None

        return self.perfume.title

    @property
    def fill_level(self) -> str | None:
        """Return the fill level range, like "70-80%", if there is a measurement."""
        if self.fill is None or self.fill.raw == NO_FILL_MEASUREMENT_RAW:
            return None

        return self.fill.title

    @property
    def fill_percentage(self) -> int | None:
        """Return an estimate of the perfume left, in percent.

        Up to the 2nd generation it is derived from the use counter on the
        cartridge, so it's no measurement and a refilled cartridge stays
        empty. The 3rd generation reports 0 to 100, per Rituals (not seen
        yet). Kept within the range of the title, so it matches the app.
        """
        if self.fill is None:
            return None

        value = as_int(self.fill.raw)
        if value is None or value < 0:
            return None

        if self.generation is not None and self.generation >= 3:
            estimate = min(100, value)
        else:
            estimate = math.floor(100 - value / FILL_USE_PER_PERCENT)
            estimate = max(0, min(100, estimate))

        if (title_range := percentage_range(self.fill.title)) is not None:
            low, high = title_range
            estimate = max(low, min(high, estimate))

        return estimate

    @property
    def wifi_rssi(self) -> int | None:
        """Return the WiFi signal strength in dBm."""
        if self.wifi is None:
            return None

        return as_int(self.wifi.raw)

    @property
    def wifi_percentage(self) -> int | None:
        """Return the (rough) WiFi signal strength, derived from the app icon."""
        if self.wifi is None or self.wifi.icon is None:
            return None

        return WIFI_ICON_PERCENTAGE.get(self.wifi.icon)
