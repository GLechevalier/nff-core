import re
from typing import Any


# ── format-detection ────────────────────────────────────────────────────────
# NOTE: a genuine task-watchdog dump ends by calling abort() ("Aborting." then
# "abort() was called"), so the watchdog signature MUST be tested before the abort
# signature — and abort() is deliberately NOT part of _WDT_RE (a *pure* abort/assert
# is its own root cause, not a watchdog). Order in parse(): brownout → watchdog →
# abort → guru → unknown.
_WDT_RE = re.compile(
    r"Task watchdog got triggered"
    r"|Interrupt wdt timeout"
    r"|Guru Meditation Error.*?TWDT",
    re.IGNORECASE,
)
_BROWNOUT_RE = re.compile(r"Brownout detector was triggered", re.IGNORECASE)
_ABORT_RE = re.compile(r"abort\(\) was called|assert failed:", re.IGNORECASE)
_GURU_RE = re.compile(r"Guru Meditation Error.*?Core\s+(\d+)\s+panic'ed", re.IGNORECASE | re.DOTALL)

# ── abort / assert field patterns ────────────────────────────────────────────
_ASSERT_MSG_RE = re.compile(r"assert failed:\s*(.+)")
_ABORT_LINE_RE = re.compile(
    r"abort\(\) was called at PC\s*0x[0-9a-fA-F]+(?:\s+on core\s*\d+)?",
    re.IGNORECASE,
)
_ABORT_PC_RE = re.compile(r"abort\(\) was called at PC\s*(0x[0-9a-fA-F]+)", re.IGNORECASE)
_ABORT_CORE_RE = re.compile(r"on core\s+(\d+)", re.IGNORECASE)

# ── guru-meditation field patterns ──────────────────────────────────────────
_EXCCAUSE_RE = re.compile(r"EXCCAUSE\s*[:=]\s*(0x[0-9a-fA-F]+|\d+)")
_EXCVADDR_RE = re.compile(r"EXCVADDR\s*[:=]\s*(0x[0-9a-fA-F]+)")
_PC_RE = re.compile(r"\bPC\s*[:=]\s*(0x[0-9a-fA-F]+)")
_CPU_RE = re.compile(r"Core\s+(\d+)\s+panic'ed")
_REG_RE = re.compile(r"\b(A\d{1,2}|PC|PS|SAR|EXCCAUSE|EXCVADDR)\s*[:=]\s*(0x[0-9a-fA-F]+)")
_BACKTRACE_RE = re.compile(
    r"Backtrace:\s*((?:0x[0-9a-fA-F]+(?::0x[0-9a-fA-F]+)?\s*)+)",
    re.IGNORECASE,
)
_PC_TOKEN_RE = re.compile(r"(0x[0-9a-fA-F]+)")

# ── watchdog task-name ───────────────────────────────────────────────────────
_TASK_RE = re.compile(
    r"Running task\s*['\"]?([^\s'\"]+)['\"]?"
    r"|Task\s*['\"]([^'\"]+)['\"].*?watchdog",
    re.IGNORECASE,
)


class ESP32PanicParser:
    def parse(self, raw: str) -> dict[str, Any]:
        """Return CrashRecord field dict (excluding crash_id, received_at, raw)."""
        if _BROWNOUT_RE.search(raw):
            return {"panic_type": "brownout", "registers": {}, "backtrace": []}
        if _WDT_RE.search(raw):
            return self._parse_watchdog(raw)
        if _ABORT_RE.search(raw):
            return self._parse_abort(raw)
        if _GURU_RE.search(raw):
            return self._parse_guru(raw)
        return {"panic_type": "unknown"}

    # ── abort / assert ───────────────────────────────────────────────────────
    def _parse_abort(self, raw: str) -> dict[str, Any]:
        result: dict[str, Any] = {"panic_type": "abort", "registers": {}}

        # abort_reason: prefer the assert-failed line (it names the invariant),
        # else the "abort() was called at PC ..." line.
        m = _ASSERT_MSG_RE.search(raw)
        if m:
            result["abort_reason"] = f"assert failed: {m.group(1).strip()}"
        else:
            m = _ABORT_LINE_RE.search(raw)
            if m:
                result["abort_reason"] = m.group(0).strip()

        m = _ABORT_PC_RE.search(raw)
        if m:
            result["fault_pc"] = m.group(1)

        m = _ABORT_CORE_RE.search(raw)
        if m:
            result["cpu"] = int(m.group(1))

        result["backtrace"] = self._parse_backtrace(raw)
        return result

    # ── shared backtrace extraction ──────────────────────────────────────────
    def _parse_backtrace(self, raw: str) -> list[str]:
        backtrace: list[str] = []
        m = _BACKTRACE_RE.search(raw)
        if m:
            for token in m.group(1).split():
                pcs = _PC_TOKEN_RE.findall(token)
                if pcs:
                    backtrace.append(pcs[0])
        return backtrace

    # ── watchdog ────────────────────────────────────────────────────────────
    def _parse_watchdog(self, raw: str) -> dict[str, Any]:
        task_name: str | None = None
        m = _TASK_RE.search(raw)
        if m:
            task_name = m.group(1) or m.group(2)
        return {
            "panic_type": "idf_watchdog",
            "task_name": task_name,
            "registers": {},
            "backtrace": [],
        }

    # ── guru meditation ─────────────────────────────────────────────────────
    def _parse_guru(self, raw: str) -> dict[str, Any]:
        result: dict[str, Any] = {"panic_type": "guru_meditation"}

        m = _CPU_RE.search(raw)
        if m:
            result["cpu"] = int(m.group(1))

        m = _EXCCAUSE_RE.search(raw)
        if m:
            val = m.group(1)
            result["exception_cause"] = int(val, 16) if val.startswith(("0x", "0X")) else int(val)

        m = _EXCVADDR_RE.search(raw)
        if m:
            result["fault_addr"] = m.group(1)

        # PC: prefer the explicit "PC :" line; fall back to first occurrence
        for candidate in _PC_RE.findall(raw):
            result["fault_pc"] = candidate
            break

        # registers
        registers: dict[str, str] = {}
        for name, value in _REG_RE.findall(raw):
            registers[name] = value
        result["registers"] = registers

        # backtrace
        result["backtrace"] = self._parse_backtrace(raw)

        # task name (sometimes printed in guru output too)
        m = _TASK_RE.search(raw)
        if m:
            result["task_name"] = m.group(1) or m.group(2)

        return result
