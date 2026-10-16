from __future__ import annotations


class Nvs:
    """In-memory key/value store, mirroring the SDK's NVS (non-volatile storage).

    Mirrors nff-sdk-c/src/port/nff_port_posix.c: an in-RAM dict that survives a
    *simulated* reboot (the process stays alive; only the MQTT session + state
    machine cycle). This is what carries the `ota_pending`/`crash_simulate` flags
    from one boot to the next, exactly like real NVS carries them across a reset.
    """

    def __init__(self) -> None:
        self._store: dict[str, str | int] = {}

    def get_str(self, key: str, default: str | None = None) -> str | None:
        val = self._store.get(key, default)
        return None if val is None else str(val)

    def get_u32(self, key: str, default: int = 0) -> int:
        return int(self._store.get(key, default))

    def set_str(self, key: str, value: str) -> None:
        self._store[key] = value

    def set_u32(self, key: str, value: int) -> None:
        self._store[key] = int(value)

    def erase(self, key: str) -> None:
        self._store.pop(key, None)
