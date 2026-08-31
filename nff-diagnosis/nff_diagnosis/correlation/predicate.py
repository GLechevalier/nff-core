from __future__ import annotations

from nff_diagnosis.schemas import FleetDevice, Predicate, TimelineEntry

# Attributes that describe a *device* (and so define a population, crashed or not).
DEVICE_ATTRS = {
    "firmware_version",
    "build_id",
    "project",
    "project_id",
    "device_id",
    "device_type",
}


def parse_version(value: object) -> tuple[int, ...] | None:
    """Parse a dotted firmware version into a comparable tuple. 'v3.5' -> (3, 5).
    Returns None if there are no numeric components (caller falls back to strings)."""
    if value is None:
        return None
    s = str(value)
    parts: list[int] = []
    for chunk in s.replace("-", ".").replace("_", ".").split("."):
        digits = "".join(ch for ch in chunk if ch.isdigit())
        if digits:
            parts.append(int(digits))
        elif parts:
            # stop at the first non-numeric trailing segment (e.g. "3.5rc" -> (3,5))
            break
    return tuple(parts) if parts else None


def _pad(a: tuple[int, ...], b: tuple[int, ...]) -> tuple[tuple[int, ...], tuple[int, ...]]:
    n = max(len(a), len(b))
    return a + (0,) * (n - len(a)), b + (0,) * (n - len(b))


def is_version_attr(attribute: str) -> bool:
    return attribute == "firmware_version" or attribute.endswith("_version")


def entry_attr(entry: TimelineEntry, attribute: str) -> object:
    if attribute.startswith("location."):
        return entry.location.get(attribute.split(".", 1)[1])
    return getattr(entry, attribute, None)


def device_attr(device: FleetDevice, attribute: str) -> object:
    return getattr(device, attribute, None)


def _compare(actual: object, op: str, expected: object, version: bool) -> bool:
    if actual is None:
        return False

    if op == "in":
        try:
            return actual in expected  # type: ignore[operator]
        except TypeError:
            return False
    if op == "matches":
        return str(expected).lower() in str(actual).lower()
    if op == "==":
        if version:
            pa, pe = parse_version(actual), parse_version(expected)
            if pa is not None and pe is not None:
                a, e = _pad(pa, pe)
                return a == e
        return str(actual) == str(expected)

    # ordered comparisons: <=, <, >=, >
    a_cmp: object
    e_cmp: object
    if version:
        pa, pe = parse_version(actual), parse_version(expected)
        if pa is not None and pe is not None:
            a_cmp, e_cmp = _pad(pa, pe)
        else:
            a_cmp, e_cmp = str(actual), str(expected)
    else:
        try:
            a_cmp, e_cmp = float(actual), float(expected)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            a_cmp, e_cmp = str(actual), str(expected)

    if op == "<=":
        return a_cmp <= e_cmp  # type: ignore[operator]
    if op == "<":
        return a_cmp < e_cmp  # type: ignore[operator]
    if op == ">=":
        return a_cmp >= e_cmp  # type: ignore[operator]
    if op == ">":
        return a_cmp > e_cmp  # type: ignore[operator]
    return False


def eval_predicate(actual: object, pred: Predicate) -> bool:
    return _compare(actual, pred.op, pred.value, is_version_attr(pred.attribute))


def entry_satisfies(entry: TimelineEntry, preds: list[Predicate]) -> bool:
    return all(eval_predicate(entry_attr(entry, p.attribute), p) for p in preds)


def device_satisfies(device: FleetDevice, preds: list[Predicate]) -> bool:
    return all(eval_predicate(device_attr(device, p.attribute), p) for p in preds)


def population_predicates(preds: list[Predicate]) -> list[Predicate]:
    """Predicates over device attributes — these define who is in the population."""
    return [p for p in preds if p.attribute in DEVICE_ATTRS]
