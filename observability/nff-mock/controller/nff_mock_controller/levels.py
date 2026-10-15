"""Per-level spawn profiles.

A ``LevelProfile`` is the table-driven answer to "what does spawning a level-N mock look
like": which image, what resource caps, how long it lives, whether the rootfs is read-only,
and the fleet caps that apply. ``server`` and ``docker`` stay level-agnostic — they ask
``profile_for(level)`` and act on the result — so adding a level is a config + table change,
not control-flow surgery.

Levels 0 (none) and 3 (real hardware) never reach here: the server short-circuits them
(spawn nothing / 501) before resolving a spawn profile.
"""

from __future__ import annotations

from dataclasses import dataclass

from nff_mock_controller.config import settings


@dataclass(frozen=True)
class LevelProfile:
    level: int
    image: str
    mem: str
    cpus: str
    pids: int
    ttl_default_s: int
    read_only: bool
    # Extra writable tmpfs (e.g. for QEMU's flash image) beyond the always-present /tmp.
    # "" → none. Only meaningful when read_only is False.
    tmpfs_size: str
    # Override stamped as NFF_MOCK_DEVICE_TYPE; "" → leave the device's own default.
    device_type: str
    max_per_project: int
    max_total: int


def profile_for(level: int) -> LevelProfile:
    """Resolve the spawn profile for a spawnable level (1 or 2). Raises for any other."""
    if level == 1:
        return LevelProfile(
            level=1,
            image=settings.mock_image,
            mem=settings.mock_mem,
            cpus=settings.mock_cpus,
            pids=settings.mock_pids,
            ttl_default_s=settings.mock_ttl_s,
            read_only=True,
            tmpfs_size="",
            device_type="",
            max_per_project=settings.max_per_project,
            max_total=settings.max_total,
        )
    if level == 2:
        return LevelProfile(
            level=2,
            image=settings.l2_image,
            mem=settings.l2_mem,
            cpus=settings.l2_cpus,
            pids=settings.l2_pids,
            ttl_default_s=settings.l2_ttl_s,
            read_only=settings.l2_read_only,
            tmpfs_size=settings.l2_tmpfs_size,
            device_type=settings.l2_device_type,
            max_per_project=settings.max_l2_per_project,
            max_total=settings.max_l2_total,
        )
    raise ValueError(f"no spawn profile for level {level}")
