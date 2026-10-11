"""Shared fixtures for hardware-free async client tests."""

from unittest.mock import AsyncMock, MagicMock
from uuid import UUID

import pytest
from bleak.backends.device import BLEDevice
from bleak.exc import BleakCharacteristicNotFoundError
from bleak_retry_connector import BleakClientWithServiceCache

import eurotronic_cometblue_ha as cometblue
from eurotronic_cometblue_ha import AsyncCometBlue, const


@pytest.fixture
def device() -> BLEDevice:
    """Provide a BLE device descriptor without scanning for hardware."""
    return BLEDevice("AA:BB:CC:DD:EE:FF", "Comet Blue", {})


@pytest.fixture
def gatt_client() -> MagicMock:
    """Provide an inspectable GATT mock that starts connected."""
    client = MagicMock(spec=BleakClientWithServiceCache)
    client.is_connected = True

    def disconnect() -> None:
        """Mark the basic GATT mock as disconnected."""
        client.is_connected = False

    client.disconnect.side_effect = disconnect
    return client


@pytest.fixture
def blue(device: BLEDevice, gatt_client: MagicMock) -> AsyncCometBlue:
    """Attach the shared GATT mock to a real async client instance."""
    client = AsyncCometBlue(device, pin=123456)
    client.client = gatt_client
    return client


@pytest.fixture(autouse=True)
def prevent_bluetooth_access(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fail tests that attempt unmocked connection establishment or discovery."""
    monkeypatch.setattr(
        cometblue,
        "establish_connection",
        AsyncMock(side_effect=AssertionError("Unit tests must mock BLE connections")),
    )
    monkeypatch.setattr(
        cometblue.BleakScanner,
        "discover",
        AsyncMock(side_effect=AssertionError("Unit tests must mock BLE discovery")),
    )


@pytest.fixture
def device_values() -> dict[UUID, bytearray]:
    """Provide fresh raw characteristic values that tests can inspect or override."""
    return {
        const.CHARACTERISTIC_MODEL: bytearray(b"HT100BT"),
        const.CHARACTERISTIC_VERSION: bytearray(b"1.0"),
        const.CHARACTERISTIC_MANUFACTURER: bytearray(b"Sygonix"),
        const.CHARACTERISTIC_BATTERY: bytearray([80]),
        const.CHARACTERISTIC_TEMPERATURE: bytearray([49, 32, 32, 40, 0, 0, 10]),
        const.CHARACTERISTIC_DATETIME: bytearray([0, 12, 1, 1, 26]),
        const.CHARACTERISTIC_SETTINGS: bytearray([0, 17, 18]),
        **{characteristic: bytearray(8) for characteristic in cometblue.WEEKDAY.values()},
        **{characteristic: bytearray([0, 0, 0, 0, 128, 0, 0, 0, 0]) for characteristic in cometblue.HOLIDAY.values()},
    }


@pytest.fixture
def writable_blue(
    blue: AsyncCometBlue,
    gatt_client: MagicMock,
    device_values: dict[UUID, bytearray],
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncCometBlue:
    """Mock writable characteristics, including partial updates and commit on disconnect."""
    pending: dict[UUID, bytearray] = {}
    writable = {
        const.CHARACTERISTIC_TEMPERATURE,
        const.CHARACTERISTIC_DATETIME,
        const.CHARACTERISTIC_SETTINGS,
        *cometblue.WEEKDAY.values(),
        *cometblue.HOLIDAY.values(),
    }

    def read(characteristic: UUID) -> bytearray:
        """Return a copy of the committed characteristic bytes while connected."""
        assert gatt_client.is_connected
        if characteristic not in device_values:
            raise BleakCharacteristicNotFoundError(characteristic)
        return bytearray(device_values[characteristic])

    def write(characteristic: UUID, value: bytearray, *, response: bool) -> None:
        """Queue a permitted write, preserving fields marked as unchanged."""
        assert gatt_client.is_connected
        assert response is True
        if characteristic not in writable:
            raise BleakCharacteristicNotFoundError(characteristic)
        current = pending.get(characteristic, device_values[characteristic])
        assert len(value) == len(current)
        if characteristic in (const.CHARACTERISTIC_TEMPERATURE, const.CHARACTERISTIC_SETTINGS):
            pending[characteristic] = bytearray(
                previous if replacement == const.UNCHANGED_VALUE else replacement
                for previous, replacement in zip(current, value, strict=True)
            )
        else:
            pending[characteristic] = bytearray(value)

    def disconnect() -> None:
        """Commit pending writes and mark the stateful mock as disconnected."""
        device_values.update(pending)
        pending.clear()
        gatt_client.is_connected = False

    def reconnect(*args: object, **kwargs: object) -> MagicMock:
        """Reconnect the same GATT mock without resetting its device state."""
        gatt_client.is_connected = True
        return gatt_client

    gatt_client.read_gatt_char.side_effect = read
    gatt_client.write_gatt_char.side_effect = write
    gatt_client.disconnect.side_effect = disconnect
    monkeypatch.setattr(cometblue, "establish_connection", AsyncMock(side_effect=reconnect))
    return blue
