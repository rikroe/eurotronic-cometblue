# Development and tests

The tests use pytest and pytest-asyncio and cover the async client only. No Bluetooth
adapter or device is needed; all BLE interactions are mocked.

## Setup and execution

Use Python 3.12 or later because the current source imports `collections.abc.Buffer`.
From the repository root, install the project and its development dependencies into
the default `.venv` and run the tests:

```bash
uv sync
uv run python -m pytest
```

The `dev` dependency group includes pytest, pytest-asyncio, and Ruff and is installed
by default by `uv sync`. `uv run` uses the project's virtual environment; no separate
tool environment or temporary dependencies are needed. Async tests use function-scoped
event loops. There are no hardware tests or device setup.

Test discovery, strict marker checking, asyncio mode, and loop scopes are configured
in [pyproject.toml](../pyproject.toml).

## Coverage and fixtures

The hardware-free tests check PIN bytes, async reads and writes, result shapes,
connection/context-manager delegation, discovery filtering, and batch ordering and
error handling. Schedule and holiday writes are checked against wire payloads, not
against real device settings. Every async setter has a write/disconnect/reconnect/read
test, including multi-day schedules. Temperature readback tests cover both matching
and unchanged responses: sending a command must not be mistaken for confirmation
that the device applied it. These mocks validate client behavior, not real device
firmware or radio communication.

The central fixtures in [conftest.py](conftest.py) provide:

- `blue` and `gatt_client`: an async client and inspectable BLE mocks for individual calls or injected errors.
- `writable_blue`: the same client backed by a stateful mock that applies pending writes on disconnect.
- `device_values`: a fresh UUID-to-bytearray store per test. Change its values to simulate device responses.

The stateful mock supports all writable characteristics, preserves unchanged (`0x80`)
fields and consecutive partial writes, and copies read responses to avoid accidental
mutation of device state. For every writable characteristic, the mock assumes reads
before disconnect echo that characteristic's last command without updating committed
state. Disconnect applies pending updates and clears the echoes; after reconnect,
reads return committed values.

See `test_writable_function_round_trip` in
[test_client.py](test_client.py) for examples using every setter.

Protocol regressions in [test_codec.py](test_codec.py) exercise the existing client
converters through the async API.