"""Back-compat shim — the crash scenario catalog now lives in nff-mock-core.

The catalog moved to ``nff_mock_core.scenarios`` so the controller (Level-2 crash
injection) and the device (Level-1 self-report) share one definition. Existing
``from nff_mock import scenarios`` / ``from nff_mock.scenarios import ...`` imports keep
working through this re-export.
"""

from __future__ import annotations

from nff_mock_core.scenarios import (  # noqa: F401
    DEFAULT_SCENARIO,
    SCENARIOS,
    backtrace_frames,
    build_crash_report,
    build_raw,
    resolve,
    rtc_log_objects,
)
