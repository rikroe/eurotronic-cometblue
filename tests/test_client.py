"""Test async client behavior and write/read workflows with mocked BLE transport."""

import asyncio
from datetime import datetime
from inspect import iscoroutinefunction
from unittest.mock import AsyncMock, MagicMock, PropertyMock, call
from uuid import UUID

import pytest
from bleak.backends.device import BLEDevice
from bleak.backends.scanner import AdvertisementData
from bleak.exc import BleakCharacteristicNotFoundError, BleakDeviceNotFoundError, BleakError

import eurotronic_cometblue_ha as cometblue
from eurotronic_cometblue_ha import (
    AsyncCometBlue,
    CometBlueBleakClient,
    HolidayValues,
    InvalidByteValueError,
    TemperatureValues,
    Weekday,
    const,
)

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
    assert blue.connected is False
    await blue.disconnect_async()
    gatt_client.disconnect.assert_not_awaited()
    await blue.connect_async()
    await blue.connect_async()
    connector.assert_awaited_once_with(
        CometBlueBleakClient,
        device,
        name="Comet Blue",
        use_services_cache=False,
        server_pin=bytearray(b"\x40\xe2\x01\x00"),
    )
    assert blue.connected is True

    await blue.disconnect_async()
    await blue.disconnect_async()
    gatt_client.disconnect.assert_awaited_once_with()
    assert blue.connected is False
    assert blue.client is gatt_client


@pytest.mark.parametrize("pin", [-1, 1000000])
async def test_invalid_pin(device: BLEDevice, pin: int) -> None:
    """Reject PINs outside the six-digit range during construction."""
    with pytest.raises(ValueError, match="Pin"):
        AsyncCometBlue(device, pin=pin)


@pytest.mark.parametrize(
    ("address", "system"),
    [("aa:bb:cc:dd:ee:ff", "Linux"), ("01234567-89ab-cdef-0123-456789abcdef", "Darwin")],
)
@pytest.mark.parametrize("found", [False, True])
async def test_address_lookup_on_connect(
    device: BLEDevice,
    gatt_client: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
    *,
    address: str,
    system: str,
    found: bool,
) -> None:
    """Resolve string addresses lazily and report missing devices without connecting."""
    monkeypatch.setattr(cometblue.platform, "system", lambda: system)
    finder = AsyncMock(return_value=device if found else None)
    connector = AsyncMock(return_value=gatt_client)
    monkeypatch.setattr(cometblue.BleakScanner, "find_device_by_address", finder)
    monkeypatch.setattr(cometblue, "establish_connection", connector)
    blue = AsyncCometBlue(address)
    finder.assert_not_awaited()

    if found:
        await blue.connect_async()
        assert blue.device is device
        assert connector.call_args.args[1] is device
        await blue.connect_async()
        connector.assert_awaited_once()
    else:
        with pytest.raises(BleakDeviceNotFoundError):
            await blue.connect_async()
        connector.assert_not_awaited()
    finder.assert_awaited_once_with(address, timeout=cometblue.BLEAK_TIMEOUT)


@pytest.mark.parametrize(
    ("address", "system"),
    [("AA:BB:CC:DD:EE:FF-extra", "Linux"), ("01234567-89ab-cdef-0123-456789abcdef-extra", "Darwin")],
)
async def test_address_must_match_completely(monkeypatch: pytest.MonkeyPatch, address: str, system: str) -> None:
    """Reject addresses with valid prefixes but invalid trailing characters."""
    monkeypatch.setattr(cometblue.platform, "system", lambda: system)
    with pytest.raises(ValueError, match="device must"):
        AsyncCometBlue(address)


@pytest.mark.parametrize("method", ["get_battery_async", "set_manual_mode_async"])
async def test_disconnected_io(device: BLEDevice, method: str) -> None:
    """Report disconnected I/O as a transport error, not a missing attribute."""
    blue = AsyncCometBlue(device)
    args = (True,) if method == "set_manual_mode_async" else ()
    with pytest.raises(BleakError, match="not connected"):
        await getattr(blue, method)(*args)


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


@pytest.mark.parametrize("error", [RuntimeError("body failed"), TimeoutError(), asyncio.CancelledError()])
@pytest.mark.parametrize("cleanup_fails", [False, True])
async def test_context_disconnects_on_body_error(
    device: BLEDevice,
    gatt_client: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
    error: BaseException,
    cleanup_fails: bool,
) -> None:
    """Preserve body errors and cancellation even when disconnect also fails."""
    monkeypatch.setattr(cometblue, "establish_connection", AsyncMock(return_value=gatt_client))
    if cleanup_fails:
        gatt_client.disconnect.side_effect = BleakError("cleanup failed")

    with pytest.raises(type(error)) as raised:
        async with AsyncCometBlue(device):
            raise error

    assert raised.value is error
    gatt_client.disconnect.assert_awaited_once_with()


@pytest.mark.parametrize("cancel_waiter", [False, True])
async def test_contexts_are_serialized(
    writable_blue: AsyncCometBlue, gatt_client: MagicMock, cancel_waiter: bool
) -> None:
    """Keep a second context from entering or disconnecting the first context's connection."""
    attempting = asyncio.Event()
    entered = asyncio.Event()
    finish = asyncio.Event()

    async def second_context() -> None:
        """Signal when another task attempts and then acquires a connection session."""
        attempting.set()
        async with writable_blue:
            entered.set()
            await finish.wait()

    async with asyncio.timeout(1), asyncio.TaskGroup() as tasks:
        async with writable_blue:
            waiter = tasks.create_task(second_context())
            await attempting.wait()
            assert not entered.is_set()
            gatt_client.disconnect.assert_not_awaited()
            if cancel_waiter:
                waiter.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await waiter
                assert await writable_blue.get_battery_async() == 80
        if cancel_waiter:
            async with writable_blue:
                assert writable_blue.connected
        else:
            await entered.wait()
            assert writable_blue.connected
            finish.set()

    assert gatt_client.disconnect.await_count == 2


async def test_nested_context_rejected(writable_blue: AsyncCometBlue) -> None:
    """Reject same-task nesting rather than deadlock or disconnect the outer session."""
    async with asyncio.timeout(1), writable_blue:
        with pytest.raises(BleakError, match="Nested"):
            async with writable_blue:
                pytest.fail("Nested context should not enter")
        assert writable_blue.connected


@pytest.mark.parametrize(
    ("method", "args"),
    [("connect_async", ()), ("disconnect_async", ()), ("get_battery_async", ()), ("set_manual_mode_async", (True,))],
)
async def test_other_task_cannot_use_session(
    writable_blue: AsyncCometBlue, method: str, args: tuple[object, ...]
) -> None:
    """Prevent direct calls from interfering with another task's connection context."""
    async with writable_blue:
        with pytest.raises(BleakError, match="another task"):
            await asyncio.create_task(getattr(writable_blue, method)(*args))
        assert writable_blue.connected


async def test_disconnect_timeout_releases_session(
    writable_blue: AsyncCometBlue, gatt_client: MagicMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Bound an unresponsive disconnect and permit a subsequent context to run."""
    monkeypatch.setattr(cometblue, "BLEAK_TIMEOUT", 0)
    disconnect = gatt_client.disconnect.side_effect
    gatt_client.disconnect.side_effect = asyncio.Event().wait
    with pytest.raises(TimeoutError):
        async with writable_blue:
            pass
    gatt_client.disconnect.side_effect = disconnect
    async with asyncio.timeout(1), writable_blue:
        assert writable_blue.connected


@pytest.mark.parametrize("error", [BleakError("connect failed"), asyncio.CancelledError()])
async def test_failed_context_entry_can_be_retried(
    device: BLEDevice, gatt_client: MagicMock, monkeypatch: pytest.MonkeyPatch, error: BaseException
) -> None:
    """Release session ownership after failed or cancelled entry without adding retries."""
    connector = AsyncMock(side_effect=error)
    monkeypatch.setattr(cometblue, "establish_connection", connector)
    blue = AsyncCometBlue(device)
    with pytest.raises(type(error)) as raised:
        async with blue:
            pytest.fail("Connection should not succeed")
    assert raised.value is error
    connector.assert_awaited_once()

    connector.side_effect = None
    connector.return_value = gatt_client
    async with asyncio.timeout(1), blue:
        assert blue.connected


async def test_context_disconnect_failure_is_propagated(writable_blue: AsyncCometBlue, gatt_client: MagicMock) -> None:
    """Report a disconnect failure when no body error exists, and release the session."""
    disconnect = gatt_client.disconnect.side_effect
    error = BleakError("disconnect failed")
    gatt_client.disconnect.side_effect = error
    with pytest.raises(BleakError) as raised:
        async with writable_blue:
            pass
    assert raised.value is error

    gatt_client.disconnect.side_effect = disconnect
    async with asyncio.timeout(1), writable_blue:
        assert writable_blue.connected


@pytest.mark.parametrize("error", [TimeoutError("PIN timed out"), asyncio.CancelledError()])
async def test_pin_failure_disconnects(
    device: BLEDevice, monkeypatch: pytest.MonkeyPatch, error: BaseException
) -> None:
    """Clean up a connected transport after PIN failure while preserving the original error."""
    connect = AsyncMock()
    write = AsyncMock(side_effect=error)
    disconnect = AsyncMock(side_effect=BleakError("cleanup failed"))
    monkeypatch.setattr(cometblue.BleakClientWithServiceCache, "__init__", MagicMock(return_value=None))
    monkeypatch.setattr(cometblue.BleakClientWithServiceCache, "connect", connect)
    monkeypatch.setattr(CometBlueBleakClient, "write_gatt_char", write)
    monkeypatch.setattr(CometBlueBleakClient, "disconnect", disconnect)
    monkeypatch.setattr(CometBlueBleakClient, "is_connected", PropertyMock(return_value=True))
    client = CometBlueBleakClient(device, server_pin=AsyncCometBlue.transform_pin(123456))

    with pytest.raises(type(error)) as raised:
        await client.connect()
    assert raised.value is error
    connect.assert_awaited_once()
    write.assert_awaited_once_with(const.CHARACTERISTIC_PIN, client.server_pin, response=True)
    disconnect.assert_awaited_once()


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


async def test_typed_temperature_read_modify_write(blue: AsyncCometBlue, gatt_client: MagicMock) -> None:
    """Keep typed read results as ordinary dictionaries accepted by the setter."""
    gatt_client.read_gatt_char.return_value = bytearray([49, 32, 32, 40, 0, 240, 10])
    values: TemperatureValues = await blue.get_temperature_async()
    assert isinstance(values, dict)
    assert type(values["windowOpen"]) is bool
    assert type(values["windowOpenMinutes"]) is int
    values["manualTemp"] = 18.0

    await blue.set_temperature_async(values)

    gatt_client.write_gatt_char.assert_awaited_once_with(
        const.CHARACTERISTIC_TEMPERATURE, bytearray([128, 36, 32, 40, 0, 128, 128]), response=True
    )
    gatt_client.read_gatt_char.assert_awaited_once()


async def test_temperature_none_is_noop(blue: AsyncCometBlue, gatt_client: MagicMock) -> None:
    """Accept the existing optional temperature update without BLE I/O."""
    await blue.set_temperature_async(None)
    gatt_client.write_gatt_char.assert_not_awaited()
    gatt_client.read_gatt_char.assert_not_awaited()


async def test_getters_read_fresh_values(blue: AsyncCometBlue, gatt_client: MagicMock) -> None:
    """Read the battery characteristic again on each getter call."""
    gatt_client.read_gatt_char.side_effect = [bytearray([80]), bytearray([79])]

    assert await blue.get_battery_async() == 80
    assert await blue.get_battery_async() == 79
    assert gatt_client.read_gatt_char.await_args_list == [call(const.CHARACTERISTIC_BATTERY)] * 2


async def test_latest_values_are_detached(blue: AsyncCometBlue, gatt_client: MagicMock) -> None:
    """Replace each characteristic's bytes without changing earlier snapshots."""
    empty = blue.raw
    assert not empty
    payload = bytearray([80])
    gatt_client.read_gatt_char.return_value = payload
    await blue.get_battery_async()
    snapshot = blue.raw
    payload[0] = 0
    first = snapshot[const.CHARACTERISTIC_BATTERY]
    assert isinstance(first, bytes)
    assert first == b"\x50"

    for level in (79, 78, 77):
        gatt_client.read_gatt_char.return_value = bytearray([level])
        await blue.get_battery_async()
    gatt_client.read_gatt_char.return_value = bytearray([1])
    await blue.get_manual_mode_async()

    assert blue.raw == {const.CHARACTERISTIC_BATTERY: b"\x4d", const.CHARACTERISTIC_SETTINGS: b"\x01"}
    assert snapshot == {const.CHARACTERISTIC_BATTERY: first}
    assert not empty
    assert gatt_client.read_gatt_char.await_count == 5


async def test_malformed_response_is_stored_before_decoding(blue: AsyncCometBlue, gatt_client: MagicMock) -> None:
    """Retain successfully received malformed bytes even though conversion raises."""
    gatt_client.read_gatt_char.return_value = bytearray([49, 32, 32, 40, 0, 0, 10])
    await blue.get_temperature_async()
    previous = blue.raw
    gatt_client.read_gatt_char.return_value = bytearray()
    with pytest.raises(InvalidByteValueError):
        await blue.get_temperature_async()
    assert blue.raw == {const.CHARACTERISTIC_TEMPERATURE: b""}
    assert previous == {const.CHARACTERISTIC_TEMPERATURE: bytes([49, 32, 32, 40, 0, 0, 10])}


@pytest.mark.parametrize("error", [BleakError("read failed"), TimeoutError(), asyncio.CancelledError()])
async def test_failed_reads_preserve_state(blue: AsyncCometBlue, gatt_client: MagicMock, error: BaseException) -> None:
    """Leave received values untouched when transport fails or is cancelled."""
    gatt_client.read_gatt_char.return_value = bytearray([80])
    await blue.get_battery_async()
    previous = blue.raw
    gatt_client.read_gatt_char.side_effect = error
    for getter in (blue.get_battery_async, blue.get_temperature_async):
        with pytest.raises(type(error)):
            await getter()
        assert blue.raw == previous


async def test_raw_copy_is_isolated_and_per_device(
    blue: AsyncCometBlue, device: BLEDevice, gatt_client: MagicMock
) -> None:
    """Allow copy mutation without changing values stored by the client."""
    gatt_client.read_gatt_char.return_value = bytearray([80])
    await blue.get_battery_async()
    snapshot = blue.raw
    characteristic = const.CHARACTERISTIC_BATTERY
    snapshot[characteristic] = b"\x00"
    attribute = "raw"
    with pytest.raises(AttributeError):
        setattr(blue, attribute, {})
    assert not AsyncCometBlue(device).raw

    await blue.disconnect_async()
    assert blue.raw[characteristic] == b"\x50"
    assert snapshot[characteristic] == b"\x00"
    gatt_client.read_gatt_char.assert_awaited_once()


async def test_device_info_retains_partial_raw_values(blue: AsyncCometBlue, gatt_client: MagicMock) -> None:
    """Keep every received field even if a later read or decoding fails."""
    gatt_client.read_gatt_char.side_effect = [b"HT100BT", BleakError("read failed")]
    with pytest.raises(BleakError):
        await blue.get_device_info_async()
    partial = blue.raw
    assert partial == {const.CHARACTERISTIC_MODEL: b"HT100BT"}

    gatt_client.read_gatt_char.side_effect = [b"HT100BT", b"1.0", b"\xff"]
    with pytest.raises(InvalidByteValueError):
        await blue.get_device_info_async()
    invalid = blue.raw
    assert invalid[const.CHARACTERISTIC_MANUFACTURER] == b"\xff"
    assert gatt_client.read_gatt_char.await_count == 5

    gatt_client.read_gatt_char.side_effect = [b"HT100BT", b"1.0", b"Sygonix"]
    expected = await blue.get_device_info_async()
    assert expected == {"model": "HT100BT", "version": "1.0", "manufacturer": "Sygonix"}
    expected["model"] = "changed"
    assert blue.raw == {
        const.CHARACTERISTIC_MODEL: b"HT100BT",
        const.CHARACTERISTIC_VERSION: b"1.0",
        const.CHARACTERISTIC_MANUFACTURER: b"Sygonix",
    }
    assert partial == {const.CHARACTERISTIC_MODEL: b"HT100BT"}
    assert invalid[const.CHARACTERISTIC_MANUFACTURER] == b"\xff"
    assert gatt_client.read_gatt_char.await_count == 8


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
    assert blue.raw[const.CHARACTERISTIC_HOLIDAY_1] == bytes([0, 0, 0, 0, 128, 0, 0, 0, 0])


@pytest.mark.parametrize(
    ("values", "expected"),
    [
        ({}, [128, 128, 128, 128, 128, 128, 128]),
        ({"manualTemp": 8}, [128, 16, 128, 128, 128, 128, 128]),
        ({"targetTempLow": 7.5, "targetTempHigh": 28.5}, [128, 128, 15, 57, 128, 128, 128]),
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
    await blue.set_temperature_async(values)
    gatt_client.write_gatt_char.assert_awaited_once_with(
        const.CHARACTERISTIC_TEMPERATURE, bytearray(expected), response=True
    )
    gatt_client.read_gatt_char.assert_not_awaited()
    gatt_client.disconnect.assert_not_awaited()


@pytest.mark.parametrize("key", ["manualTemp", "targetTempLow", "targetTempHigh"])
@pytest.mark.parametrize("value", [None, "20", []])
async def test_temperature_write_skips_nonnumeric_values(
    blue: AsyncCometBlue, gatt_client: MagicMock, key: str, value: object
) -> None:
    """Leave unsupported field values unchanged while applying valid updates."""
    method = "set_temperature_async"
    await getattr(blue, method)({key: value, "tempOffset": 1.0})

    gatt_client.write_gatt_char.assert_awaited_once_with(
        const.CHARACTERISTIC_TEMPERATURE, bytearray([128, 128, 128, 128, 2, 128, 128]), response=True
    )
    gatt_client.read_gatt_char.assert_not_awaited()


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
    await blue.set_datetime_async(datetime(2026, 10, 10, 12, 34, 56))
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
    await blue.set_weekday_async(Weekday.MONDAY, values)
    gatt_client.write_gatt_char.assert_awaited_once_with(
        const.CHARACTERISTIC_MONDAY, bytearray(expected), response=True
    )


async def test_multiple_weekday_writes(blue: AsyncCometBlue, gatt_client: MagicMock) -> None:
    """Accept case-insensitive weekday names and skip entries with no schedule."""
    values = {"start1": "08:00", "end1": "09:00"}

    await blue.set_weekdays_async({"monday": values, "TUESDAY": None, "Friday": values})
    assert gatt_client.write_gatt_char.await_args_list == [
        call(const.CHARACTERISTIC_MONDAY, bytearray([48, 54, 0, 0, 0, 0, 0, 0]), response=True),
        call(const.CHARACTERISTIC_FRIDAY, bytearray([48, 54, 0, 0, 0, 0, 0, 0]), response=True),
    ]


@pytest.mark.parametrize(
    "payload",
    [
        bytes([0, 0, 0, 0, 128, 0, 0, 0, 0]),
        bytes([128, 26, 12, 26, 14, 5, 1, 27, 33]),
    ],
    ids=["inactive", "active-without-start"],
)
async def test_holiday_readback_requires_valid_write_fields(
    blue: AsyncCometBlue, gatt_client: MagicMock, payload: bytes
) -> None:
    """Keep write validation even though reads and writes share a dictionary type."""
    gatt_client.read_gatt_char.return_value = bytearray(payload)
    values: HolidayValues = await blue.get_holiday_async(1)

    with pytest.raises(ValueError):
        await blue.set_holiday_async(1, values)

    gatt_client.write_gatt_char.assert_not_awaited()


async def test_holiday_write(blue: AsyncCometBlue, gatt_client: MagicMock) -> None:
    """Encode holiday dates and half-degree temperature into the selected slot."""
    values: HolidayValues = {
        "start": datetime(2026, 12, 26, 18),
        "end": datetime(2027, 1, 5, 14),
        "temperature": 16.5,
    }

    await blue.set_holiday_async(2, values)
    gatt_client.write_gatt_char.assert_awaited_once_with(
        const.CHARACTERISTIC_HOLIDAY_2, bytearray([18, 26, 12, 26, 14, 5, 1, 27, 33]), response=True
    )


@pytest.mark.parametrize("enabled", [False, True])
async def test_manual_mode_write(blue: AsyncCometBlue, gatt_client: MagicMock, enabled: bool) -> None:
    """Write the manual-mode flag while marking the remaining settings unchanged."""
    await blue.set_manual_mode_async(enabled)
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
    gatt_client: MagicMock,
    device_values: dict[UUID, bytearray],
    *,
    setter: str,
    write_args: tuple[object, ...],
    getter: str,
    read_args: tuple[object, ...],
    expected: object,
) -> None:
    """Echo each command without committing until disconnect, then read back the applied state."""
    write = getattr(writable_blue, setter)
    read = getattr(writable_blue, getter)
    assert writable_blue.client is not None
    original = {characteristic: bytearray(value) for characteristic, value in device_values.items()}
    previous = writable_blue.raw

    assert await write(*write_args) is None
    assert writable_blue.raw == previous
    assert device_values == original
    gatt_client.write_gatt_char.assert_awaited()
    for write_call in gatt_client.write_gatt_char.await_args_list:
        characteristic, command = write_call.args
        echoed = await writable_blue.client.read_gatt_char(characteristic)
        assert echoed == command
        echoed[0] ^= 0xFF
        assert await writable_blue.client.read_gatt_char(characteristic) == command
    assert device_values == original

    await writable_blue.disconnect_async()
    await writable_blue.connect_async()

    assert writable_blue.raw == previous
    for characteristic, value in device_values.items():
        assert await writable_blue.client.read_gatt_char(characteristic) == value
    assert await read(*read_args) == expected


async def test_temperature_write_echo_before_reconnect(writable_blue: AsyncCometBlue) -> None:
    """Model the observed command echo and complete temperature readback after reconnect."""
    assert writable_blue.client is not None
    original = await writable_blue.get_temperature_async()
    await writable_blue.set_temperature_async({"manualTemp": 18.0})
    expected_command = bytearray([128, 36, 128, 128, 128, 128, 128])
    echoed = await writable_blue.client.read_gatt_char(const.CHARACTERISTIC_TEMPERATURE)
    assert echoed == expected_command
    echoed[1] = 0
    assert await writable_blue.client.read_gatt_char(const.CHARACTERISTIC_TEMPERATURE) == expected_command

    with pytest.raises(InvalidByteValueError, match="Invalid temperature"):
        await writable_blue.get_temperature_async()
    assert writable_blue.raw[const.CHARACTERISTIC_TEMPERATURE] == expected_command

    await writable_blue.disconnect_async()
    await writable_blue.connect_async()
    assert await writable_blue.get_temperature_async() == {**original, "manualTemp": 18.0}


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
    assert writable_blue.client is not None
    result = await writable_blue.client.read_gatt_char(const.CHARACTERISTIC_BATTERY)
    result[0] = 0

    assert await writable_blue.get_battery_async() == 80


async def test_mock_rejects_read_only_writes(writable_blue: AsyncCometBlue) -> None:
    """Reject writes to read-only characteristics without changing their stored values."""
    assert writable_blue.client is not None
    with pytest.raises(BleakCharacteristicNotFoundError):
        await writable_blue.client.write_gatt_char(const.CHARACTERISTIC_BATTERY, bytearray([0]), response=True)

    assert await writable_blue.get_battery_async() == 80
