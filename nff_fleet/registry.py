import time
from dataclasses import dataclass, field
from typing import Any


@dataclass
class DeviceInfo:
    device_id: str
    status: str = "offline"
    project_id: str | None = None
    last_seen: float = field(default_factory=time.time)
    heartbeat_payload: dict = field(default_factory=dict)


class DeviceRegistry:
    def __init__(self) -> None:
        self._devices: dict[str, DeviceInfo] = {}

    def update_heartbeat(
        self, device_id: str, payload: dict[str, Any], project_id: str | None = None
    ) -> None:
        info = self._devices.get(device_id)
        if info is None:
            info = DeviceInfo(device_id=device_id)
            self._devices[device_id] = info
        info.status = payload.get("status", "online")
        info.last_seen = time.time()
        info.heartbeat_payload = payload
        if project_id:
            info.project_id = project_id

    def set_offline(self, device_id: str) -> None:
        if device_id in self._devices:
            self._devices[device_id].status = "offline"

    def get_device(self, device_id: str) -> DeviceInfo | None:
        return self._devices.get(device_id)

    def get_project(self, device_id: str) -> str | None:
        info = self._devices.get(device_id)
        return info.project_id if info else None

    def all_devices(self) -> list[DeviceInfo]:
        return list(self._devices.values())

    def online_devices(self) -> list[DeviceInfo]:
        return [d for d in self._devices.values() if d.status == "online"]

    def stale_online_devices(self, timeout: float) -> list[DeviceInfo]:
        cutoff = time.time() - timeout
        return [
            d
            for d in self._devices.values()
            if d.status == "online" and d.last_seen < cutoff
        ]


registry = DeviceRegistry()
