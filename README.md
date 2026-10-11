# CometBlue
[![GitHub version](https://badge.fury.io/gh/rikroe%2Feurotronic-cometblue.svg)](https://badge.fury.io/gh/rikroe%2Feurotronic-cometblue)
[![PyPI version](https://badge.fury.io/py/eurotronic-cometblue-ha.svg)](https://badge.fury.io/py/eurotronic-cometblue-ha)

eurotronic-cometblue-ha is a library to communicate with your Eurotronic GmbH Comet Blue radiator controllers.

> [!NOTE]
> This library is a fork of [zero-udo/eurotronic-cometblue](https://github.com/zero-udo/eurotronic-cometblue) with changes specifically for using it in a [Home Assistant custom component](https://github.com/rikroe/cometblue-custom-component). For changes, see [zero-udo/eurotronic-cometblue#8](https://github.com/zero-udo/eurotronic-cometblue/pull/8).

The device supports up to four programmable schedules for each weekday. Longer periods can be set as holidays. There are
8 holiday slots available.
It depends on [bleak](https://github.com/hbldh/bleak) and [bleak-retry-connector](https://github.com/Bluetooth-Devices/bleak-retry-connector) and runs therefore on Linux, Windows and macOS in addition to the Home Assistant component.

## Compatible devices

Comet Blue radiator controllers are sold under different names:

| "Manufacturer" | Model      |       Tested       |
|----------------|------------|:------------------:|
| Eurotronic     | Comet Blue | :heavy_check_mark: |
| Sygonix        | HT100 BT   | :heavy_check_mark: |
| Xavax          | Hama       |  :grey_question:   |
| Silvercrest    | RT2000BT   |  :grey_question:   |

This library should work with all listed radiator controllers, but is only tested with a Eurotronic Comet Blue which is the
only one I own.

If your device is not listed here but looks similar (or you know it is a rebranded Comet Blue), or if you are able to
test this library with another device - let me know your results.

Just

```
pip install eurotronic-cometblue-ha
```

## Usage

The client is async-only. Use `AsyncCometBlue` with a `BLEDevice` returned by Bleak
discovery and an optional `pin` (default `0`). Discovery timeouts belong to the scan,
not the client constructor.

An address string is resolved during `connect_async()` using the existing BLE timeout;
a supplied `BLEDevice` is used directly without scanning.

All device operations must be awaited. Use `async with` to connect and disconnect
automatically, or explicitly await `connect_async()` and `disconnect_async()`.

Connection contexts on the same instance are serialized. Callers
using explicit connect/disconnect calls must serialize their own sessions. Retry delays
and refresh scheduling remain the caller's responsibility.

| Method | Parameters | Return Value |
|--------|------------|--------------|
| `get_device_info_async` | None | `dict` with `model`, `version`, `manufacturer` |
| `get_temperature_async` | None | `dict` with `currentTemp`, `manualTemp`, `targetTempLow`, `targetTempHigh`, `tempOffset`, `windowOpen`, `windowOpenMinutes` |
| `set_temperature_async` | `dict` containing any of `manualTemp`, `targetTempLow`, `targetTempHigh`, `tempOffset` | None |
| `get_battery_async` | None | Battery percentage as `int` |
| `get_datetime_async` | None | Device-local `datetime`, used for schedules |
| `set_datetime_async` | Optional `datetime`, defaults to now | None |
| `get_weekday_async` | `Weekday`, e.g. `Weekday.MONDAY` | `dict` with `start#` and `end#` keys (# = 1-4), times as `HH:mm` strings |
| `set_weekday_async` | `Weekday` and schedule `dict` | None |
| `set_weekdays_async` | `dict` mapping weekday names (e.g. `"monday"`) to schedule dictionaries | None |
| `get_holiday_async` | Slot number (1-8) | `dict` with `start`, `end`, `temperature`; empty for an inactive slot |
| `set_holiday_async` | Slot number (1-8) and `dict` with `start`, `end`, `temperature` | None |
| `get_manual_mode_async` | None | `True` for manual mode, `False` for scheduled mode |
| `set_manual_mode_async` | `bool` selecting manual mode | None |
| `get_multiple_async` | List of names: `temperature`, `battery`, `datetime`, `holiday1`-`holiday8`, `holidays`, `monday`-`sunday`, `weekdays`, `manual` | `dict` of results from the corresponding getters |
| `discover_async` | Optional scan `timeout`, defaults to 5 seconds | List of matching `BLEDevice` objects |

:warning: the device applies set values when the connection is closed, not directly after setting them.

### Last-known state

`AsyncCometBlue.raw` returns a dictionary mapping characteristic UUIDs to their latest
immutable bytes, without BLE I/O or decoding. Unread characteristics are
absent. Malformed responses remain available even when the getter raises

### Migrating from the synchronous client

The synchronous `CometBlue` class has been removed. Replace it with `AsyncCometBlue`,
use `async with` instead of `with`, and await the existing `*_async` methods.
Replace `CometBlue.discover()` with `await AsyncCometBlue.discover_async()`; discovery
returns `BLEDevice` objects, whose `.address` attribute provides the address.
The library no longer creates or runs event loops. In an async application, use its
existing loop; a standalone script can call `asyncio.run(main())` as shown below.

## Examples

### Instantiating and retrieving the current temperature

```python
import asyncio

from bleak import BleakScanner

from eurotronic_cometblue_ha import AsyncCometBlue


async def main():
    device = await BleakScanner.find_device_by_address("00:00:00:00:00:00", timeout=10)
    if device is None:
        raise RuntimeError("Comet Blue device not found")
    async with AsyncCometBlue(device=device, pin=123456) as blue:
        temp = await blue.get_temperature_async()
        print(temp)


asyncio.run(main())
```

results in

```python
{
    "currentTemp": 24.5,
    "manualTemp": 16.0,
    "targetTempLow": 16.0,
    "targetTempHigh": 20.0,
    "tempOffset": 0.0,
    "windowOpen": True,
    "windowOpenMinutes": 10,
}
```

### Setting a new schedule

Setting a new schedule for Mondays with two heating periods:

- Period 1 from 06:00 to 08:00

- Period 2 from 16:00 to 22:00

All unmentioned periods (periods 3 and 4) will be disabled (start and end set to 00:00).

Set periods will be consolidated, meaning setting only period 3 via this method will write period 1 and disable the
others.

To use this schedule, make sure to disable manual mode. Run the following inside
an async function while `blue` is connected (for example, inside the `async with`
block above).

```python
from eurotronic_cometblue_ha import Weekday

await blue.set_weekday_async(Weekday.MONDAY, {"start1": "06:00", "end1": "08:00", "start2": "16:00", "end2": "22:00"})
await blue.set_manual_mode_async(False)
```

### Setting a holiday

Setting a holiday (slot 2) from 26th December 2020 18:00 till 5th January 2021 14:00 and keep the temperature at 16.5 °C

```python
from datetime import datetime

await blue.set_holiday_async(
    2, {"start": datetime(2020, 12, 26, 18), "end": datetime(2021, 1, 5, 14), "temperature": 16.5}
)
```

## Development and tests

See [tests/README.md](tests/README.md) for setup, running tests in the project's default
virtual environment, and using the shared BLE mocks.

## Credits:

* Thorsten Tränker for his reverse engineering work
  done [here](https://www.torsten-traenkner.de/wissen/smarthome/heizung.php)
* [@zero-udo](https://github.com/zero-udo) for the initial library [zero-udo/eurotronic-cometblue](https://github.com/zero-udo/eurotronic-cometblue)
* [@FloSchmidt](https://github.com/FloSchmidt) for [checking](https://github.com/zero-udo/eurotronic-cometblue/issues/5) the Eurotronic Comet Blue
* [@rikroe](https://github.com/rikroe) for the switch to bleak
