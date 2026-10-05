# pylint: disable=W0621
"""Asynchronous Python client for the Rituals Perfume Genie API."""

import asyncio

from ritualsgenie import RitualsGenie


async def main() -> None:
    """Show example on controlling your Rituals Perfume Genie."""
    async with RitualsGenie(email="you@example.com", password="secret") as genie:
        for hub in await genie.hubs():
            state = {True: "on", False: "off", None: "unknown"}[hub.is_on]
            print(f"{hub.name}: {state}")

            # Only when it is known to be off; unknown is not off.
            if hub.is_on is False:
                await genie.turn_on(hub.hash)

            await genie.set_perfume_amount(hub.hash, 2)
            await genie.set_room_square_meters(hub.hash, 20)


if __name__ == "__main__":
    asyncio.run(main())
