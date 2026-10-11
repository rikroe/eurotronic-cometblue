"""Test hardware-free protocol conversions used by the async client."""

import pytest

from eurotronic_cometblue_ha import AsyncCometBlue


@pytest.mark.parametrize(
    ("pin", "expected"),
    [(0, b"\x00\x00\x00\x00"), (123456, b"\x40\xe2\x01\x00")],
)
def test_pin_conversion(pin: int, expected: bytes) -> None:
    """Encode PINs as four-byte little-endian bytearrays."""
    result = AsyncCometBlue.transform_pin(pin)

    assert isinstance(result, bytearray)
    assert result == bytearray(expected)
