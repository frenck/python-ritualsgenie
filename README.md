# Python: Asynchronous client for the Rituals Perfume Genie API

[![GitHub Release][releases-shield]][releases]
[![Python Versions][python-versions-shield]][pypi]
![Project Stage][project-stage-shield]
![Project Maintenance][maintenance-shield]
[![License][license-shield]](LICENSE.md)

[![Build Status][build-shield]][build]
[![Code Coverage][codecov-shield]][codecov]
[![OpenSSF Scorecard][scorecard-shield]][scorecard]
[![Open in Dev Containers][devcontainer-shield]][devcontainer]

[![Sponsor Frenck via GitHub Sponsors][github-sponsors-shield]][github-sponsors]

[![Support Frenck on Patreon][patreon-shield]][patreon]

Asynchronous Python client for the Rituals Perfume Genie API.

## About

This package allows you to control and monitor Rituals Perfume Genie
diffusers programmatically. It is mainly created to allow third-party
programs to automate the behavior of a Perfume Genie.

An excellent example of this might be Home Assistant, which allows you to write
automations, to turn on your diffuser when you come home.

## A word on the rate limit

The Rituals API is rate limited, at roughly 30 requests per hour per IP
address at the time of writing. Logins have their own, stricter limit: a
dozen or so within an hour locks the account out for a while. This library
is built around that:

- It logs in lazily and reuses its token until it expires (an hour),
  instead of logging in on every poll.
- All requests that need a login at the same time share a single login,
  and a login that just failed isn't retried right away.
- A rate limit or lockout is remembered: until it is over, requests fail
  right away with `RitualsGenieRateLimitError`, without touching the API.
- A single `hubs()` call returns the state (on/off, perfume amount, room
  size, firmware) of _all_ diffusers on the account.
- Sensors (fill level, perfume, WiFi, battery) are a request _per sensor_,
  and are only requested for sensors the diffuser actually has.

What it doesn't do is pace your requests: how often you poll is up to you.
Do the math, and remember other clients on the same IP address share the
budget. For example, with two diffusers:

| What                                 | Requests per hour |
| ------------------------------------ | ----------------: |
| `hubs()` every 5 minutes             |                12 |
| `sensors(hub)` hourly, 4 sensors × 2 |                 8 |
| Login, once per hour                 |                 1 |
| **Total**                            |            **21** |

That leaves a handful of requests for actually controlling the diffusers.
Use `sensors(hub, only=[...])` to request just the sensors you need, and
never the version sensor: the firmware is part of `hubs()` already.

Be nice to the API. Rituals has blocked Home Assistant before for polling too
much, the sensors in particular, and with that every user of it.

Token reuse works within a client, so create one client and share it.

## Installation

```bash
pip install ritualsgenie
```

To use the command-line interface as well:

```bash
pip install "ritualsgenie[cli]"
```

## CLI

```bash
# Set credentials once via environment variables
export RITUALS_EMAIL="you@example.com"
export RITUALS_PASSWORD="secret"

# List all diffusers and their state
ritualsgenie hubs

# Show the details of a diffuser (by name, hublot or hash), like its model
ritualsgenie info "Living room"

# Show the sensors of a diffuser
ritualsgenie sensors "Living room"

# Turn a diffuser on/off
ritualsgenie on "Living room"
ritualsgenie off "Living room"

# Set the perfume intensity (1-3) and the room size in square meters
ritualsgenie perfume-amount "Living room" 2
ritualsgenie room-size "Living room" 20

# Turn the standby LED off, start a firmware update
ritualsgenie standby-led "Living room" off
ritualsgenie update-firmware "Living room"

# Emit machine-readable JSON
ritualsgenie hubs --json

# Dump raw API responses as JSON, for sharing in an issue
ritualsgenie dump --sensors
```

Every command looks up the diffusers first, so most commands cost two or
three requests. To not log in on every command, the CLI caches the token in
`~/.cache/ritualsgenie/` (or `$XDG_CACHE_HOME/ritualsgenie/`), readable by
you only. Set `RITUALS_NO_TOKEN_CACHE=1` to disable that.

The `dump` command redacts the hash, hublot, PIN and the room and space
names. Sensor readings are kept as is, including the RFID tag of the
cartridge. Have a look before you share it.

Output goes to stdout, errors to stderr, so `--json` output can be piped
safely. Pass the credentials as options or environment variables in that
case, so there is no prompt in between.

## Usage

```python
import asyncio

from ritualsgenie import RitualsGenie, Sensor


async def main() -> None:
    """Show example on controlling your Rituals Perfume Genie."""
    async with RitualsGenie(email="you@example.com", password="secret") as genie:
        for hub in await genie.hubs():
            state = {True: "on", False: "off", None: "unknown"}[hub.is_on]
            print(f"{hub.name}: {state}")

            sensors = await genie.sensors(hub, only=[Sensor.PERFUME])
            if sensors.perfume_name:
                print(f"Perfume: {sensors.perfume_name}")

            await genie.turn_on(hub.hash)
            await genie.set_perfume_amount(hub.hash, 2)


if __name__ == "__main__":
    asyncio.run(main())
```

Many properties return `None` when the API doesn't tell, so "unknown" is
never mistaken for "off". Check with `is False` when that matters.

### Sharing a session

Pass your own `aiohttp` session to share it, like Home Assistant does. The
client never closes a session it didn't create:

```python
async with aiohttp.ClientSession() as session:
    genie = RitualsGenie(email="you@example.com", password="secret", session=session)
    hubs = await genie.hubs()
```

### More endpoints

```python
# A single diffuser, by hash
hub = await genie.hub(hub_hash)

# The model, only available through the hublot lookup
details = await genie.hublot(hub.hublot)
print(details.device_version)  # "Genie 2.1"

# Room size in square meters, which sets the category the diffuser works
# with as well. Two requests, see the docstring.
await genie.set_room_square_meters(hub.hash, 20)

# The standby LED
if hub.supports_standby_led:
    await genie.set_standby_led(hub.hash, enabled=False)

# Start a firmware update when one is available
if hub.firmware and hub.firmware.update_available:
    await genie.update_firmware(hub.hash)
```

### Error handling

```python
from ritualsgenie import (
    RitualsGenieAuthenticationError,
    RitualsGenieConnectionError,
    RitualsGenieError,
    RitualsGenieRateLimitError,
    RitualsGenieResponseError,
    RitualsGenieValueError,
)

try:
    await genie.update_firmware(hub.hash)
except RitualsGenieValueError:
    ...  # Invalid argument, caught before anything was sent
except RitualsGenieAuthenticationError:
    ...  # Invalid credentials; a stale token is already retried for you
except RitualsGenieRateLimitError as err:
    ...  # Back off, err.retry_after tells for how long (if known)
except RitualsGenieResponseError as err:
    ...  # The API said no; err.status has the HTTP status, like 409
except RitualsGenieConnectionError:
    ...  # Network trouble, including timeouts
except RitualsGenieError:
    ...  # Anything else
```

### Reusing a token

The token is available as `genie.token`. Pass it back in when creating a new
client to skip a login (and save a request) after a restart:

```python
genie = RitualsGenie(email="you@example.com", password="secret", token=token)
```

## Changelog & Releases

This repository keeps a change log using [GitHub's releases][releases]
functionality. The format of the log is based on
[Keep a Changelog][keepchangelog].

Releases are based on [Semantic Versioning][semver], and use the format
of `MAJOR.MINOR.PATCH`. In a nutshell, the version will be incremented
based on the following:

- `MAJOR`: Incompatible or major changes.
- `MINOR`: Backwards-compatible new features and enhancements.
- `PATCH`: Backwards-compatible bugfixes and package updates.

## Contributing

This is an active open-source project. We are always open to people who want to
use the code or contribute to it.

We've set up a separate document for our
[contribution guidelines](.github/CONTRIBUTING.md).

Thank you for being involved! :heart_eyes:

## Setting up development environment

The easiest way to start, is by opening a CodeSpace here on GitHub, or by using
the [Dev Container][devcontainer] feature of Visual Studio Code.

[![Open in Dev Containers][devcontainer-shield]][devcontainer]

This Python project is fully managed using the [Poetry][poetry] dependency
manager. But also relies on the use of NodeJS for certain checks during
development.

You need at least:

- Python 3.12+
- [Poetry][poetry-install]
- NodeJS 24+ (including NPM)

To install all packages, including all development requirements:

```bash
npm install
poetry install --all-extras
```

As this repository uses the [prek][prek] framework, all changes
are linted and tested with each commit. You can run all checks and tests
manually, using the following command:

```bash
poetry run prek run --all-files
```

To run just the Python tests:

```bash
poetry run pytest
```

## Authors & contributors

The original setup of this repository is by [Franck Nijhof][frenck].

For a full list of all authors and contributors,
check [the contributor's page][contributors].

## Disclaimer

This project is an independent, community-driven effort. It is **not
affiliated with, endorsed by, or supported by** Rituals Cosmetics or
Sense Company. All product names, trademarks, and registered trademarks are
property of their respective owners.

Use this software at your own risk.

## License

MIT License

Copyright (c) 2026 Franck Nijhof

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.

[build-shield]: https://github.com/frenck/python-ritualsgenie/actions/workflows/tests.yaml/badge.svg
[build]: https://github.com/frenck/python-ritualsgenie/actions/workflows/tests.yaml
[codecov-shield]: https://codecov.io/gh/frenck/python-ritualsgenie/branch/main/graph/badge.svg
[codecov]: https://codecov.io/gh/frenck/python-ritualsgenie
[contributors]: https://github.com/frenck/python-ritualsgenie/graphs/contributors
[devcontainer-shield]: https://img.shields.io/static/v1?label=Dev%20Containers&message=Open&color=blue&logo=visualstudiocode
[devcontainer]: https://vscode.dev/redirect?url=vscode://ms-vscode-remote.remote-containers/cloneInVolume?url=https://github.com/frenck/python-ritualsgenie
[frenck]: https://github.com/frenck
[github-sponsors-shield]: https://frenck.dev/wp-content/uploads/2019/12/github_sponsor.png
[github-sponsors]: https://github.com/sponsors/frenck
[keepchangelog]: http://keepachangelog.com/en/1.0.0/
[license-shield]: https://img.shields.io/github/license/frenck/python-ritualsgenie.svg
[maintenance-shield]: https://img.shields.io/maintenance/yes/2026.svg
[patreon-shield]: https://frenck.dev/wp-content/uploads/2019/12/patreon.png
[patreon]: https://www.patreon.com/frenck
[poetry-install]: https://python-poetry.org/docs/#installation
[poetry]: https://python-poetry.org
[prek]: https://github.com/frenck/prek
[project-stage-shield]: https://img.shields.io/badge/project%20stage-experimental-yellow.svg
[pypi]: https://pypi.org/project/ritualsgenie/
[python-versions-shield]: https://img.shields.io/pypi/pyversions/ritualsgenie
[releases-shield]: https://img.shields.io/github/release/frenck/python-ritualsgenie.svg
[releases]: https://github.com/frenck/python-ritualsgenie/releases
[scorecard-shield]: https://api.scorecard.dev/projects/github.com/frenck/python-ritualsgenie/badge
[scorecard]: https://scorecard.dev/viewer/?uri=github.com/frenck/python-ritualsgenie
[semver]: http://semver.org/spec/v2.0.0.html
