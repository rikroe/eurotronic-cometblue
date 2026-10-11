"""Test async client behavior and write/read workflows with mocked BLE transport."""

import asyncio
from datetime import datetime
from inspect import iscoroutinefunction
from unittest.mock import AsyncMock, MagicMock, call
from uuid import UUID

import pytest
from bleak.backends.device import BLEDevice
from bleak.backends.scanner import AdvertisementData
from bleak.exc import BleakCharacteristicNotFoundError, BleakError

import eurotronic_cometblue_ha as cometblue
from eurotronic_cometblue_ha import AsyncCometBlue, CometBlueBleakClient, InvalidByteValueError, Weekday, const

pytestmark = pytest.mark.asyncio


async def test_async_only_public_api() -> None:
    """Expose the async client without a synchronous adapter or context manager."""
    assert not hasattr(cometblue, "CometBlue")
    for method in (
        "connect",
        "disconnect",
        "get_device_info",
        "get_temperature",
        "set_temperature",
        "get_battery",
        "get_datetime",
        "set_datetime",
        "get_weekday",
        "set_weekday",
        "get_holiday",
        "set_holiday",
        "get_manual_mode",
        "set_manual_mode",
        "get_multiple",
        "discover",
    ):
        assert not hasattr(AsyncCometBlue, method)
        assert iscoroutinefunction(getattr(AsyncCometBlue, f"{method}_async"))
    assert not hasattr(AsyncCometBlue, "__enter__")
    assert not hasattr(AsyncCometBlue, "__exit__")


async def test_connect_and_disconnect(
    device: BLEDevice, gatt_client: MagicMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Pass device and PIN details to the connector and delegate disconnection."""
    connector = AsyncMock(return_value=gatt_client)
    monkeypatch.setattr(cometblue, "establish_connection", connector)
    blue = AsyncCometBlue(device, pin=123456)

    assert blue.device is device
    assert await blue.connect_async() is None
    connector.assert_awaited_once_with(
        CometBlueBleakClient,
        device,
        name="Comet Blue",
        use_services_cache=False,
        server_pin=bytearray(b"\x40\xe2\x01\x00"),
    )
    assert blue.connected is True

    assert await blue.disconnect_async() is None
    gatt_client.disconnect.assert_awaited_once_with()
    assert blue.connected is False


async def test_async_context_manager(
    device: BLEDevice, gatt_client: MagicMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Connect on context entry, return the client, and disconnect on exit."""
    connector = AsyncMock(return_value=gatt_client)
    monkeypatch.setattr(cometblue, "establish_connection", connector)
    blue = AsyncCometBlue(device)

    async with blue as entered:
        assert entered is blue
        assert blue.connected is True
        gatt_client.disconnect.assert_not_awaited()

    connector.assert_awaited_once()
    gatt_client.disconnect.assert_awaited_once_with()


async def test_context_disconnects_on_body_error(
    device: BLEDevice, gatt_client: MagicMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Disconnect when the context body raises without suppressing its error."""
    monkeypatch.setattr(cometblue, "establish_connection", AsyncMock(return_value=gatt_client))
    error = RuntimeError("body failed")

    with pytest.raises(RuntimeError) as raised:
        async with AsyncCometBlue(device):
            raise error

    assert raised.value is error
    gatt_client.disconnect.assert_awaited_once_with()


@pytest.mark.parametrize(("raw_offset", "expected_offset"), [(0, 0.0), (3, 1.5), (253, -1.5)])
async def test_temperature_read(
    blue: AsyncCometBlue, gatt_client: MagicMock, raw_offset: int, expected_offset: float
) -> None:
    """Decode temperatures, signed offsets, and window settings from raw bytes."""
    gatt_client.read_gatt_char.return_value = bytearray([49, 32, 32, 40, raw_offset, 240, 10])

    assert await blue.get_temperature_async() == {
        "currentTemp": 24.5,
        "manualTemp": 16.0,
        "targetTempLow": 16.0,
        "targetTempHigh": 20.0,
        "tempOffset": expected_offset,
        "windowOpen": True,
        "windowOpenMinutes": 10,
    }
    gatt_client.read_gatt_char.assert_awaited_once_with(const.CHARACTERISTIC_TEMPERATURE)


async def test_getters_read_fresh_values(blue: AsyncCometBlue, gatt_client: MagicMock) -> None:
    """Read the battery characteristic again on each getter call."""
    gatt_client.read_gatt_char.side_effect = [bytearray([80]), bytearray([79])]

    assert await blue.get_battery_async() == 80
    assert await blue.get_battery_async() == 79
    assert gatt_client.read_gatt_char.await_args_list == [call(const.CHARACTERISTIC_BATTERY)] * 2


async def test_device_info(blue: AsyncCometBlue, gatt_client: MagicMock) -> None:
    """Read and decode model, firmware version, and manufacturer in order."""
    gatt_client.read_gatt_char.side_effect = [bytearray(b"HT100BT"), bytearray(b"1.0"), bytearray(b"Sygonix")]

    assert await blue.get_device_info_async() == {"model": "HT100BT", "version": "1.0", "manufacturer": "Sygonix"}
    assert gatt_client.read_gatt_char.await_args_list == [
        call(const.CHARACTERISTIC_MODEL),
        call(const.CHARACTERISTIC_VERSION),
        call(const.CHARACTERISTIC_MANUFACTURER),
    ]


async def test_datetime_read(blue: AsyncCometBlue, gatt_client: MagicMock) -> None:
    """Decode a leap-day response into a naive datetime with minute precision."""
    gatt_client.read_gatt_char.return_value = bytearray([34, 12, 29, 2, 24])

    assert await blue.get_datetime_async() == datetime(2024, 2, 29, 12, 34)
    gatt_client.read_gatt_char.assert_awaited_once_with(const.CHARACTERISTIC_DATETIME)


@pytest.mark.parametrize(("mode", "expected"), [(0, False), (1, True), (2, False), (3, True)])
async def test_manual_mode_read(blue: AsyncCometBlue, gatt_client: MagicMock, mode: int, expected: bool) -> None:
    """Determine manual mode from the least significant settings bit."""
    gatt_client.read_gatt_char.return_value = bytearray([mode, 128, 128])

    assert await blue.get_manual_mode_async() is expected
    gatt_client.read_gatt_char.assert_awaited_once_with(const.CHARACTERISTIC_SETTINGS)


async def test_holiday_read(blue: AsyncCometBlue, gatt_client: MagicMock) -> None:
    """Read the requested holiday slot and decode its dates and temperature."""
    gatt_client.read_gatt_char.return_value = bytearray([18, 26, 12, 26, 14, 5, 1, 27, 33])

    assert await blue.get_holiday_async(2) == {
        "start": datetime(2026, 12, 26, 18),
        "end": datetime(2027, 1, 5, 14),
        "temperature": 16.5,
    }
    gatt_client.read_gatt_char.assert_awaited_once_with(const.CHARACTERISTIC_HOLIDAY_2)


async def test_empty_holiday(blue: AsyncCometBlue, gatt_client: MagicMock) -> None:
    """Return an empty mapping when the holiday end-hour sentinel is set."""
    gatt_client.read_gatt_char.return_value = bytearray([0, 0, 0, 0, 128, 0, 0, 0, 0])

    assert await blue.get_holiday_async(1) == {}


@pytest.mark.parametrize(
    ("values", "expected"),
    [
        ({"manualTemp": 8}, [128, 16, 128, 128, 128, 128, 128]),
        (
            {"manualTemp": 20, "targetTempLow": 16, "targetTempHigh": 22, "tempOffset": -1.5},
            [128, 40, 32, 44, 253, 128, 128],
        ),
    ],
)
async def test_temperature_write(
    blue: AsyncCometBlue, gatt_client: MagicMock, values: dict[str, float], expected: list[int]
) -> None:
    """Write the expected temperature payload without implicit reads or disconnects."""
    assert await blue.set_temperature_async(values) is None
    gatt_client.write_gatt_char.assert_awaited_once_with(
        const.CHARACTERISTIC_TEMPERATURE, bytearray(expected), response=True
    )
    gatt_client.read_gatt_char.assert_not_awaited()
    gatt_client.disconnect.assert_not_awaited()


@pytest.mark.parametrize(("reported_raw", "expected_temperature"), [(40, 20.0), (32, 16.0)])
async def test_temperature_write_then_read_reports_device_response(
    writable_blue: AsyncCometBlue,
    gatt_client: MagicMock,
    device_values: dict[UUID, bytearray],
    reported_raw: int,
    expected_temperature: float,
) -> None:
    """Report the readback temperature even when it differs from the requested value."""
    assert (await writable_blue.get_temperature_async())["manualTemp"] == 16.0
    await writable_blue.set_temperature_async({"manualTemp": 20.0})
    await writable_blue.disconnect_async()
    await writable_blue.connect_async()
    device_values[const.CHARACTERISTIC_TEMPERATURE][1] = reported_raw
    result = await writable_blue.get_temperature_async()

    assert result["manualTemp"] == expected_temperature
    assert result["currentTemp"] == 24.5
    assert gatt_client.mock_calls == [
        call.read_gatt_char(const.CHARACTERISTIC_TEMPERATURE),
        call.write_gatt_char(
            const.CHARACTERISTIC_TEMPERATURE, bytearray([128, 40, 128, 128, 128, 128, 128]), response=True
        ),
        call.disconnect(),
        call.read_gatt_char(const.CHARACTERISTIC_TEMPERATURE),
    ]


@pytest.mark.parametrize(
    "values",
    [{"manualTemp": 7}, {"targetTempLow": 29}, {"targetTempHigh": 7}, {"tempOffset": 5.5}],
)
async def test_invalid_temperature_not_written(
    blue: AsyncCometBlue, gatt_client: MagicMock, values: dict[str, float]
) -> None:
    """Reject out-of-range temperature settings before issuing a GATT write."""
    with pytest.raises(ValueError):
        await blue.set_temperature_async(values)

    gatt_client.write_gatt_char.assert_not_awaited()


async def test_datetime_write(blue: AsyncCometBlue, gatt_client: MagicMock) -> None:
    """Encode datetime fields in wire order and omit seconds."""
    assert await blue.set_datetime_async(datetime(2026, 10, 10, 12, 34, 56)) is None
    gatt_client.write_gatt_char.assert_awaited_once_with(
        const.CHARACTERISTIC_DATETIME, bytearray([34, 12, 10, 10, 26]), response=True
    )


@pytest.mark.parametrize(
    ("values", "expected"),
    [
        ({}, [0] * 8),
        ({"start1": "08:00", "end1": "09:00", "start2": "10:00", "end2": "11:00"}, [48, 54, 60, 66, 0, 0, 0, 0]),
        (
            {"start2": "08:00", "end2": "09:00", "start4": "10:00", "end4": "11:00"},
            [48, 54, 60, 66, 0, 0, 0, 0],
        ),
        (
            {
                "start1": "08:00",
                "end1": "09:00",
                "start2": "10:00",
                "end2": "11:00",
                "start3": "12:00",
                "end3": "13:00",
                "start4": "14:00",
                "end4": "15:00",
            },
            [48, 54, 60, 66, 72, 78, 84, 90],
        ),
        ({"start1": "08:19", "end1": "09:29"}, [49, 56, 0, 0, 0, 0, 0, 0]),
    ],
)
async def test_weekday_write(
    blue: AsyncCometBlue, gatt_client: MagicMock, values: dict[str, str], expected: list[int]
) -> None:
    """Encode compacted schedule periods in ten-minute units and zero unused slots."""
    assert await blue.set_weekday_async(Weekday.MONDAY, values) is None
    gatt_client.write_gatt_char.assert_awaited_once_with(
        const.CHARACTERISTIC_MONDAY, bytearray(expected), response=True
    )


async def test_multiple_weekday_writes(blue: AsyncCometBlue, gatt_client: MagicMock) -> None:
    """Accept case-insensitive weekday names and skip entries with no schedule."""
    values = {"start1": "08:00", "end1": "09:00"}

    assert await blue.set_weekdays_async({"monday": values, "TUESDAY": None, "Friday": values}) is None
    assert gatt_client.write_gatt_char.await_args_list == [
        call(const.CHARACTERISTIC_MONDAY, bytearray([48, 54, 0, 0, 0, 0, 0, 0]), response=True),
        call(const.CHARACTERISTIC_FRIDAY, bytearray([48, 54, 0, 0, 0, 0, 0, 0]), response=True),
    ]


async def test_holiday_write(blue: AsyncCometBlue, gatt_client: MagicMock) -> None:
    """Encode holiday dates and half-degree temperature into the selected slot."""
    values = {"start": datetime(2026, 12, 26, 18), "end": datetime(2027, 1, 5, 14), "temperature": 16.5}

    assert await blue.set_holiday_async(2, values) is None
    gatt_client.write_gatt_char.assert_awaited_once_with(
        const.CHARACTERISTIC_HOLIDAY_2, bytearray([18, 26, 12, 26, 14, 5, 1, 27, 33]), response=True
    )


@pytest.mark.parametrize("enabled", [False, True])
async def test_manual_mode_write(blue: AsyncCometBlue, gatt_client: MagicMock, enabled: bool) -> None:
    """Write the manual-mode flag while marking the remaining settings unchanged."""
    assert await blue.set_manual_mode_async(enabled) is None
    gatt_client.write_gatt_char.assert_awaited_once_with(
        const.CHARACTERISTIC_SETTINGS, bytearray([int(enabled), 128, 128]), response=True
    )


async def test_multiple_reads(blue: AsyncCometBlue, gatt_client: MagicMock) -> None:
    """Read requested characteristics sequentially and return their decoded values."""
    gatt_client.read_gatt_char.side_effect = [
        bytearray([82]),
        bytearray([1, 128, 128]),
        bytearray([34, 12, 10, 10, 26]),
    ]

    assert await blue.get_multiple_async(["battery", "manual", "datetime"]) == {
        "battery": 82,
        "manual": True,
        "datetime": datetime(2026, 10, 10, 12, 34),
    }
    assert gatt_client.read_gatt_char.await_args_list == [
        call(const.CHARACTERISTIC_BATTERY),
        call(const.CHARACTERISTIC_SETTINGS),
        call(const.CHARACTERISTIC_DATETIME),
    ]


async def test_empty_multiple_read(blue: AsyncCometBlue, gatt_client: MagicMock) -> None:
    """Return an empty mapping without I/O when no values are requested."""
    assert await blue.get_multiple_async([]) == {}
    gatt_client.read_gatt_char.assert_not_awaited()


async def test_multiple_expands_groups_without_duplicates(
    blue: AsyncCometBlue, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Expand weekday and holiday groups without rereading explicitly repeated entries."""
    weekday_reader = AsyncMock(side_effect=[{"start1": "08:00", "end1": "09:00"} for _ in Weekday])
    holiday_reader = AsyncMock(side_effect=[{} for _ in range(8)])
    monkeypatch.setattr(blue, "get_weekday_async", weekday_reader)
    monkeypatch.setattr(blue, "get_holiday_async", holiday_reader)

    result = await blue.get_multiple_async(["weekdays", "monday", "holidays", "holiday1"])

    assert list(result) == [weekday.name.lower() for weekday in Weekday] + [
        f"holiday{number}" for number in range(1, 9)
    ]
    assert result["monday"] == {"start1": "08:00", "end1": "09:00"}
    assert result["holiday1"] == {}
    assert weekday_reader.await_args_list == [call(weekday) for weekday in Weekday]
    assert holiday_reader.await_args_list == [call(number) for number in range(1, 9)]


async def test_multiple_continues_after_invalid_values_and_raises_last(
    blue: AsyncCometBlue, gatt_client: MagicMock, caplog: pytest.LogCaptureFixture
) -> None:
    """Continue batch reads after conversion failures and raise the last such error."""
    gatt_client.read_gatt_char.side_effect = [
        bytearray([255, 32, 32, 40, 0, 0, 0]),
        bytearray([82]),
        bytearray([0, 0, 1, 13, 26]),
        bytearray([1, 128, 128]),
    ]

    with pytest.raises(InvalidByteValueError, match="Invalid datetime"):
        await blue.get_multiple_async(["temperature", "battery", "datetime", "manual"])

    assert "Invalid temperature" in caplog.text
    assert "Invalid datetime" in caplog.text
    assert gatt_client.read_gatt_char.await_args_list == [
        call(const.CHARACTERISTIC_TEMPERATURE),
        call(const.CHARACTERISTIC_BATTERY),
        call(const.CHARACTERISTIC_DATETIME),
        call(const.CHARACTERISTIC_SETTINGS),
    ]


@pytest.mark.parametrize("error", [BleakError("read failed"), TimeoutError(), asyncio.CancelledError()])
async def test_multiple_aborts_on_transport_failure(
    blue: AsyncCometBlue, gatt_client: MagicMock, error: BaseException
) -> None:
    """Stop batch reads immediately on transport errors, timeouts, or cancellation."""
    gatt_client.read_gatt_char.side_effect = error

    with pytest.raises(type(error)) as raised:
        await blue.get_multiple_async(["battery", "manual"])

    assert raised.value is error
    gatt_client.read_gatt_char.assert_awaited_once_with(const.CHARACTERISTIC_BATTERY)


async def test_write_error_propagates(blue: AsyncCometBlue, gatt_client: MagicMock) -> None:
    """Propagate the original transport error without retrying the write."""
    error = BleakError("write failed")
    gatt_client.write_gatt_char.side_effect = error

    with pytest.raises(BleakError) as raised:
        await blue.set_manual_mode_async(True)

    assert raised.value is error
    assert gatt_client.write_gatt_char.await_count == 1


async def test_discovery_filters_service(device: BLEDevice, monkeypatch: pytest.MonkeyPatch) -> None:
    """Return only devices advertising the Comet Blue service and forward the timeout."""
    other = BLEDevice("11:22:33:44:55:66", "Other device", {})
    matching = AdvertisementData("Comet Blue", {}, {}, [const.SERVICE], None, -60, ())
    nonmatching = AdvertisementData("Other device", {}, {}, [], None, -60, ())
    discover = AsyncMock(return_value={device.address: (device, matching), other.address: (other, nonmatching)})
    monkeypatch.setattr(cometblue.BleakScanner, "discover", discover)

    result = await AsyncCometBlue.discover_async(30)

    assert result == [device]
    assert result[0] is device
    discover.assert_awaited_once_with(30, return_adv=True)


async def test_discovery_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    """Return an empty discovery result using the default scan timeout."""
    discover = AsyncMock(return_value={})
    monkeypatch.setattr(cometblue.BleakScanner, "discover", discover)

    assert await AsyncCometBlue.discover_async() == []
    discover.assert_awaited_once_with(5, return_adv=True)


@pytest.mark.parametrize(
    ("setter", "write_args", "getter", "read_args", "expected"),
    [
        pytest.param(
            "set_temperature_async",
            ({"manualTemp": 20.0},),
            "get_temperature_async",
            (),
            {
                "currentTemp": 24.5,
                "manualTemp": 20.0,
                "targetTempLow": 16.0,
                "targetTempHigh": 20.0,
                "tempOffset": 0.0,
                "windowOpen": False,
                "windowOpenMinutes": 10,
            },
            id="temperature",
        ),
        pytest.param(
            "set_datetime_async",
            (datetime(2026, 10, 10, 12, 34),),
            "get_datetime_async",
            (),
            datetime(2026, 10, 10, 12, 34),
            id="datetime",
        ),
        pytest.param(
            "set_weekday_async",
            (Weekday.MONDAY, {"start1": "08:00", "end1": "09:00"}),
            "get_weekday_async",
            (Weekday.MONDAY,),
            {"start1": "08:00", "end1": "09:00"},
            id="weekday",
        ),
        pytest.param(
            "set_weekdays_async",
            (
                {
                    "monday": {"start1": "08:00", "end1": "09:00"},
                    "friday": {"start1": "10:00", "end1": "11:00"},
                },
            ),
            "get_multiple_async",
            (["monday", "friday"],),
            {
                "monday": {"start1": "08:00", "end1": "09:00"},
                "friday": {"start1": "10:00", "end1": "11:00"},
            },
            id="multiple-weekdays",
        ),
        pytest.param(
            "set_holiday_async",
            (2, {"start": datetime(2026, 12, 26, 18), "end": datetime(2027, 1, 5, 14), "temperature": 16.5}),
            "get_holiday_async",
            (2,),
            {"start": datetime(2026, 12, 26, 18), "end": datetime(2027, 1, 5, 14), "temperature": 16.5},
            id="holiday",
        ),
        pytest.param("set_manual_mode_async", (True,), "get_manual_mode_async", (), True, id="manual-mode-on"),
        pytest.param("set_manual_mode_async", (False,), "get_manual_mode_async", (), False, id="manual-mode-off"),
    ],
)
async def test_writable_function_round_trip(
    writable_blue: AsyncCometBlue,
    *,
    setter: str,
    write_args: tuple[object, ...],
    getter: str,
    read_args: tuple[object, ...],
    expected: object,
) -> None:
    """Read each setter's committed result after disconnecting and reconnecting the mock."""
    write = getattr(writable_blue, setter)
    read = getattr(writable_blue, getter)
    original = await read(*read_args)

    assert await write(*write_args) is None
    assert await read(*read_args) == original
    await writable_blue.disconnect_async()
    await writable_blue.connect_async()

    assert await read(*read_args) == expected


async def test_mock_preserves_consecutive_partial_writes(
    writable_blue: AsyncCometBlue, device_values: dict[UUID, bytearray]
) -> None:
    """Merge queued partial writes without losing earlier updates or unchanged fields."""
    await writable_blue.set_temperature_async({"manualTemp": 20.0, "targetTempLow": 17.0})
    await writable_blue.set_temperature_async({"manualTemp": 21.0})
    await writable_blue.set_manual_mode_async(True)
    await writable_blue.disconnect_async()
    await writable_blue.connect_async()

    result = await writable_blue.get_temperature_async()
    assert result["manualTemp"] == 21.0
    assert result["targetTempLow"] == 17.0
    assert device_values[const.CHARACTERISTIC_SETTINGS] == bytearray([1, 17, 18])


async def test_mock_read_returns_detached_bytes(writable_blue: AsyncCometBlue) -> None:
    """Keep device state intact when a caller mutates a mock read response."""
    result = await writable_blue.client.read_gatt_char(const.CHARACTERISTIC_BATTERY)
    result[0] = 0

    assert await writable_blue.get_battery_async() == 80


async def test_mock_rejects_read_only_writes(writable_blue: AsyncCometBlue) -> None:
    """Reject writes to read-only characteristics without changing their stored values."""
    with pytest.raises(BleakCharacteristicNotFoundError):
        await writable_blue.client.write_gatt_char(const.CHARACTERISTIC_BATTERY, bytearray([0]), response=True)

    assert await writable_blue.get_battery_async() == 80
