import re
from typing import Any


# ── format-detection ────────────────────────────────────────────────────────
_WDT_RE = re.compile(
    r"Task watchdog got triggered"
    r"|Guru Meditation Error.*?TWDT"
    r"|abort\(\) was called",
    re.IGNORECASE,
)
_GURU_RE = re.compile(r"Guru Meditation Error.*?Core\s+(\d+)\s+panic'ed", re.IGNORECASE | re.DOTALL)

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
        if _WDT_RE.search(raw):
            return self._parse_watchdog(raw)
        if _GURU_RE.search(raw):
            return self._parse_guru(raw)
        return {"panic_type": "unknown"}

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
        backtrace: list[str] = []
        m = _BACKTRACE_RE.search(raw)
        if m:
            for token in m.group(1).split():
                pcs = _PC_TOKEN_RE.findall(token)
                if pcs:
                    backtrace.append(pcs[0])
        result["backtrace"] = backtrace

        # task name (sometimes printed in guru output too)
        m = _TASK_RE.search(raw)
        if m:
            result["task_name"] = m.group(1) or m.group(2)

        return result
