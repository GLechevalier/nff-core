DEVICE_TRANSITIONS: dict[str, set[str]] = {
    "pending": {"accepted", "rejected"},
    "preauthorized": {"accepted"},
    "accepted": {"rejected", "decommissioned"},
    "rejected": {"accepted"},
    "decommissioned": set(),
}

DEPLOYMENT_TRANSITIONS: dict[str, set[str]] = {
    "scheduled": {"active", "aborted"},
    "active": {"paused", "finished", "aborted"},
    "paused": {"active", "aborted"},
    "finished": set(),
    "aborted": set(),
}

DEVICE_DEPLOYMENT_TRANSITIONS: dict[str, set[str]] = {
    "pending": {"downloading", "aborted", "decommissioned"},
    "downloading": {"installing", "failure", "aborted"},
    "installing": {"rebooting", "failure", "aborted"},
    "rebooting": {"success", "failure", "aborted"},
    "success": set(),
    "failure": {"pending"},
    "already-installed": set(),
    "aborted": set(),
    "decommissioned": set(),
}

_REGISTRIES = {
    "device": DEVICE_TRANSITIONS,
    "deployment": DEPLOYMENT_TRANSITIONS,
    "device_deployment": DEVICE_DEPLOYMENT_TRANSITIONS,
}


def validate_transition(entity: str, current: str, target: str) -> None:
    registry = _REGISTRIES.get(entity)
    if registry is None:
        raise ValueError(f"Unknown entity type: {entity!r}")
    allowed = registry.get(current, set())
    if target not in allowed:
        raise ValueError(
            f"Illegal {entity} transition: {current!r} → {target!r}. "
            f"Allowed from {current!r}: {sorted(allowed) or 'none (terminal state)'}"
        )
