# pylint: disable=W0621
"""Asynchronous Python client for the Rituals Perfume Genie API."""

import asyncio

from ritualsgenie import RitualsGenie


async def main() -> None:
    """Show example on reading the sensors of your Rituals Perfume Genie."""
    async with RitualsGenie(email="you@example.com", password="secret") as genie:
        for hub in await genie.hubs():
            # Every sensor is a separate request; mind the rate limit.
            sensors = await genie.sensors(hub)

            print(f"{hub.name}")
            if sensors.perfume_name:
                print(f"  Perfume: {sensors.perfume_name}")
            if sensors.fill_level:
                # An estimate, from how long the cartridge has been used.
                estimate = sensors.fill_percentage
                print(f"  Fill level: {sensors.fill_level} (about {estimate}%)")
            if sensors.wifi_rssi is not None:
                print(f"  WiFi: {sensors.wifi_rssi} dBm")
            if sensors.battery_percentage is not None:
                print(f"  Battery: {sensors.battery_percentage}%")


if __name__ == "__main__":
    asyncio.run(main())
