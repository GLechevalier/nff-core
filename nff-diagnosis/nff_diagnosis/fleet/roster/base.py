from abc import ABC, abstractmethod

from nff_diagnosis.schemas import FleetDevice


class FleetRosterBase(ABC):
    """Source of the fleet population — every device we know about, healthy or not.

    Crashers come from our own crash store; the roster supplies the *denominator*
    so a hypothesis can be checked against devices that did NOT crash.
    """

    @abstractmethod
    def list_devices(
        self,
        build_id: str | None = None,
        firmware_version: str | None = None,
        project_id: str | None = None,
    ) -> list[FleetDevice]:
        """Return devices, optionally filtered. None filters mean 'no filter'."""
        ...


def matches_filters(
    device: FleetDevice,
    build_id: str | None,
    firmware_version: str | None,
    project_id: str | None,
) -> bool:
    """Shared equality filtering used by the in-memory backends."""
    if build_id is not None and device.build_id != build_id:
        return False
    if firmware_version is not None and device.firmware_version != firmware_version:
        return False
    if project_id is not None and device.project_id != project_id:
        return False
    return True
