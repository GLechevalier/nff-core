import json
from pathlib import Path

from nff_diagnosis.fleet.roster.base import FleetRosterBase, matches_filters
from nff_diagnosis.schemas import FleetDevice


class LocalFleetRoster(FleetRosterBase):
    """Roster backed by a JSON file: an array of device objects (snake_case keys
    matching FleetDevice). Used for tests, offline runs, and feeding a roster
    export without a live Supabase connection.
    """

    def __init__(self, path: Path) -> None:
        self._path = Path(path)
        self._devices: list[FleetDevice] | None = None

    def _load(self) -> list[FleetDevice]:
        if self._devices is None:
            data = json.loads(self._path.read_text(encoding="utf-8"))
            self._devices = [FleetDevice.model_validate(d) for d in data]
        return self._devices

    def list_devices(
        self,
        build_id: str | None = None,
        firmware_version: str | None = None,
        project_id: str | None = None,
    ) -> list[FleetDevice]:
        return [
            d
            for d in self._load()
            if matches_filters(d, build_id, firmware_version, project_id)
        ]
