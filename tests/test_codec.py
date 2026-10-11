"""Test hardware-free protocol conversions used by the async client."""

from datetime import datetime
from unittest.mock import MagicMock

import pytest

from eurotronic_cometblue_ha import AsyncCometBlue, InvalidByteValueError, Weekday


@pytest.mark.parametrize(
    ("pin", "expected"),
    [(0, b"\x00\x00\x00\x00"), (123456, b"\x40\xe2\x01\x00")],
)
def test_pin_conversion(pin: int, expected: bytes) -> None:
    """Encode PINs as four-byte little-endian bytearrays."""
    result = AsyncCometBlue.transform_pin(pin)

    assert isinstance(result, bytearray)
    assert result == bytearray(expected)


@pytest.mark.asyncio
@pytest.mark.parametrize(("offset", "expected"), [(-5, 246), (-1.5, 253), (0, 0), (0.5, 1), (1, 2), (1.5, 3), (5, 10)])
async def test_temperature_offset_wire_values(
    blue: AsyncCometBlue, gatt_client: MagicMock, offset: float, expected: int
) -> None:
    """Encode signed half-degree offsets without changing other fields."""
    await blue.set_temperature_async({"tempOffset": offset})

    gatt_client.write_gatt_char.assert_awaited_once()
    assert gatt_client.write_gatt_char.call_args.args[1] == bytearray([128, 128, 128, 128, expected, 128, 128])


@pytest.mark.asyncio
@pytest.mark.parametrize("buffer_type", [bytes, bytearray])
@pytest.mark.parametrize(
    ("method", "args", "payload", "expected"),
    [
        pytest.param(
            "get_temperature_async",
            (),
            bytes([49, 32, 32, 40, 253, 240, 10]),
            {
                "currentTemp": 24.5,
                "manualTemp": 16.0,
                "targetTempLow": 16.0,
                "targetTempHigh": 20.0,
                "tempOffset": -1.5,
                "windowOpen": True,
                "windowOpenMinutes": 10,
            },
            id="temperature",
        ),
        pytest.param(
            "get_datetime_async", (), bytes([59, 23, 29, 2, 24]), datetime(2024, 2, 29, 23, 59), id="datetime-leap-day"
        ),
        pytest.param(
            "get_weekday_async",
            (Weekday.MONDAY,),
            bytes([36, 48, 0, 0, 0, 0, 0, 0]),
            {"start1": "06:00", "end1": "08:00"},
            id="weekday-single-period",
        ),
        pytest.param(
            "get_weekday_async",
            (Weekday.MONDAY,),
            bytes([36, 48, 60, 72, 84, 96, 108, 120]),
            {
                "start1": "06:00",
                "end1": "08:00",
                "start2": "10:00",
                "end2": "12:00",
                "start3": "14:00",
                "end3": "16:00",
                "start4": "18:00",
                "end4": "20:00",
            },
            id="weekday-four-periods",
        ),
        pytest.param(
            "get_weekday_async",
            (Weekday.MONDAY,),
            bytes([144, 255, 36, 48, 42, 42, 255, 255]),
            {"start2": "06:00", "end2": "08:00"},
            id="weekday-sentinels-preserve-slots",
        ),
        pytest.param(
            "get_holiday_async",
            (1,),
            bytes([10, 29, 2, 24, 14, 31, 3, 24, 33]),
            {"start": datetime(2024, 2, 29, 10), "end": datetime(2024, 3, 31, 14), "temperature": 16.5},
            id="holiday-leap-day-and-month-end",
        ),
        pytest.param(
            "get_holiday_async",
            (2,),
            bytes([10, 31, 1, 27, 14, 1, 2, 27, 33]),
            {"start": datetime(2027, 1, 31, 10), "end": datetime(2027, 2, 1, 14), "temperature": 16.5},
            id="holiday-start-day-31",
        ),
        pytest.param("get_battery_async", (), bytes([255]), 255, id="battery"),
        pytest.param("get_manual_mode_async", (), bytes([3]), True, id="manual-mode"),
    ],
)
async def test_fixed_layout_response_boundaries(
    blue: AsyncCometBlue,
    gatt_client: MagicMock,
    *,
    buffer_type: type[bytes] | type[bytearray],
    method: str,
    args: tuple[object, ...],
    payload: bytes,
    expected: object,
) -> None:
    """Accept complete buffers and trailing bytes without mutation, but reject every truncated prefix."""
    getter = getattr(blue, method)
    for complete in (payload, payload + b"\xff"):
        received = buffer_type(complete)
        gatt_client.read_gatt_char.return_value = received
        assert await getter(*args) == expected
        assert received == complete

    for length in range(len(payload)):
        gatt_client.read_gatt_char.return_value = buffer_type(payload[:length])
        with pytest.raises(InvalidByteValueError):
            await getter(*args)


@pytest.mark.asyncio
@pytest.mark.parametrize("position", range(8))
@pytest.mark.parametrize("invalid", [145, 254])
async def test_invalid_weekday_byte_beside_sentinel(
    blue: AsyncCometBlue, gatt_client: MagicMock, position: int, invalid: int
) -> None:
    """Validate every weekday byte even when its paired value is an unset sentinel."""
    payload = bytearray([255] * 8)
    payload[position] = invalid
    gatt_client.read_gatt_char.return_value = payload

    with pytest.raises(InvalidByteValueError):
        await blue.get_weekday_async(Weekday.MONDAY)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("start", "end"),
    [("06", "08:00"), ("06:00:30", "08:00"), ("06:bad", "08:00"), ("06:00", "24:00"), ("06:00", "08:60")],
)
async def test_invalid_schedule_times_not_written(
    blue: AsyncCometBlue, gatt_client: MagicMock, start: str, end: str
) -> None:
    """Reject malformed or out-of-range times with ValueError before writing."""
    with pytest.raises(ValueError):
        await blue.set_weekday_async(Weekday.MONDAY, {"start1": start, "end1": end})
    gatt_client.write_gatt_char.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("position", "invalid"),
    [(0, 24), (1, 0), (2, 4), (3, 100), (4, 24), (5, 31), (6, 13), (7, 100)],
)
async def test_invalid_holiday_calendar_fields(
    blue: AsyncCometBlue, gatt_client: MagicMock, position: int, invalid: int
) -> None:
    """Normalize invalid calendar dates and field ranges to the public decoding error."""
    payload = bytearray([10, 31, 1, 27, 14, 1, 2, 27, 33])
    payload[position] = invalid
    gatt_client.read_gatt_char.return_value = payload

    with pytest.raises(InvalidByteValueError):
        await blue.get_holiday_async(1)


@pytest.mark.asyncio
async def test_non_leap_holiday_rejected(blue: AsyncCometBlue, gatt_client: MagicMock) -> None:
    """Reject February 29 in a non-leap year with the public decoding error."""
    gatt_client.read_gatt_char.return_value = bytearray([10, 29, 2, 27, 14, 1, 3, 27, 33])

    with pytest.raises(InvalidByteValueError):
        await blue.get_holiday_async(1)


@pytest.mark.asyncio
@pytest.mark.parametrize("end_hour", [14, 128, 255])
async def test_holiday_sentinels(blue: AsyncCometBlue, gatt_client: MagicMock, end_hour: int) -> None:
    """Preserve active-start and inactive-end holiday sentinel behavior."""
    gatt_client.read_gatt_char.return_value = bytearray([128, 1, 1, 27, end_hour, 1, 2, 27, 33])
    expected = {"start": None, "end": datetime(2027, 2, 1, 14), "temperature": 16.5} if end_hour == 14 else {}

    assert await blue.get_holiday_async(1) == expected


@pytest.mark.asyncio
@pytest.mark.parametrize("key", ["start", "end", "temperature", "extra"])
async def test_invalid_holiday_fields_not_written(blue: AsyncCometBlue, gatt_client: MagicMock, key: str) -> None:
    """Reject missing and extra holiday fields before any BLE write."""
    values = {"start": datetime(2027, 1, 1), "end": datetime(2027, 1, 2), "temperature": 16.5}
    if key == "extra":
        values[key] = 1
    else:
        values.pop(key)

    with pytest.raises(ValueError, match="required"):
        await blue.set_holiday_async(1, values)
    gatt_client.write_gatt_char.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("key", ["start", "end"])
async def test_invalid_holiday_date_type_not_written(blue: AsyncCometBlue, gatt_client: MagicMock, key: str) -> None:
    """Reject non-datetime holiday endpoints with a deliberate input error."""
    values: dict[str, object] = {"start": datetime(2027, 1, 1), "end": datetime(2027, 1, 2), "temperature": 16.5}
    values[key] = "2027-01-01"

    with pytest.raises(ValueError):
        await blue.set_holiday_async(1, values)
    gatt_client.write_gatt_char.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("position", range(3))
async def test_invalid_device_info_text(blue: AsyncCometBlue, gatt_client: MagicMock, position: int) -> None:
    """Normalize invalid UTF-8 in each device-information field without additional reads."""
    gatt_client.read_gatt_char.side_effect = [b"valid"] * position + [b"\xff"]

    with pytest.raises(InvalidByteValueError):
        await blue.get_device_info_async()
    assert gatt_client.read_gatt_char.await_count == position + 1


@pytest.mark.asyncio
@pytest.mark.parametrize("offset", [-5.5, 5.5, float("inf"), float("-inf"), float("nan")])
async def test_invalid_offset_not_written(blue: AsyncCometBlue, gatt_client: MagicMock, offset: float) -> None:
    """Reject nonfinite and out-of-range offsets before issuing a command."""
    with pytest.raises(ValueError):
        await blue.set_temperature_async({"tempOffset": offset})
    gatt_client.write_gatt_char.assert_not_awaited()


@pytest.mark.asyncio
async def test_schedule_quantization_and_compaction(blue: AsyncCometBlue, gatt_client: MagicMock) -> None:
    """Keep ten-minute flooring and move complete periods to the first slot."""
    await blue.set_weekday_async(Weekday.MONDAY, {"start3": "06:09", "end3": "08:19"})

    assert gatt_client.write_gatt_char.call_args.args[1] == bytearray([36, 49, 0, 0, 0, 0, 0, 0])


@pytest.mark.asyncio
@pytest.mark.parametrize(("position", "invalid"), [(0, 60), (1, 24), (2, 0), (3, 13), (4, 27)])
async def test_invalid_datetime_fields(
    blue: AsyncCometBlue, gatt_client: MagicMock, position: int, invalid: int
) -> None:
    """Keep malformed clock fields and non-leap February 29 under the public decoding error."""
    payload = bytearray([59, 23, 29, 2, 24])
    payload[position] = invalid
    gatt_client.read_gatt_char.return_value = payload

    with pytest.raises(InvalidByteValueError):
        await blue.get_datetime_async()


@pytest.mark.asyncio
@pytest.mark.parametrize(("temperature", "same_date"), [(7.5, False), (29, False), (16.5, True)])
async def test_holiday_zero_command_is_preserved(
    blue: AsyncCometBlue, gatt_client: MagicMock, temperature: float, same_date: bool
) -> None:
    """Preserve the existing zero command for equal dates or temperatures outside the holiday range."""
    start = datetime(2027, 1, 1)
    end = start if same_date else datetime(2027, 1, 2)
    await blue.set_holiday_async(1, {"start": start, "end": end, "temperature": temperature})

    assert gatt_client.write_gatt_char.call_args.args[1] == bytearray(9)
