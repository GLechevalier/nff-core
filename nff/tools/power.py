"""Energy measurement — drives the nff-power-meter (see ../../../nff-power-meter/).

An STM32 Nucleo watches a 1 Ω low-side shunt in the ESP32's ground return, accumulates
charge and energy at 200 kSps, and reports raw integer sums. This module converts those
sums to joules and wraps a subprocess so you can ask what `nff ota` cost the device.

Shared layer: both ``commands/power.py`` and ``mcp_server.py`` call in here — neither
holds measurement logic of its own.

HONESTY CONTRACT. The meter cannot lose samples silently: a missed ADC conversion sets an
overrun flag that arrives as ``ovr``. When ``ovr`` is non-zero the accumulated energy is an
under-count, so we report ``ok: False`` and refuse to hand back a joules figure. A
plausible-but-wrong energy number is worse than no number, because nothing downstream
could tell the difference.
"""

import json
import subprocess
import time
from dataclasses import dataclass, field
from typing import Callable, Optional

import serial
import serial.tools.list_ports

from nff import config

_BAUD = 921600  # must match monitor_speed in nff-power-meter/platformio.ini
_ADC_FULL_SCALE = 4096
_PROBE_TIMEOUT = 1.5
_FRAME_TIMEOUT = 3.0
_STLINK_VID = 0x0483  # STMicroelectronics — the Nucleo's on-board ST-Link VCP


class PowerError(Exception):
    pass


@dataclass
class MeterFrame:
    """One cumulative reading since the last ZERO.

    The meter reports raw integer sums rather than derived joules, so that a later
    re-calibration can re-derive the energy of a past run without re-measuring it.
    """

    t_ms: int
    n: int  # samples accumulated
    ovr: int  # ADC overruns — non-zero means samples were LOST
    sq: int  # Σ q       (q = shunt ADC counts)
    sqq: int  # Σ q²
    suq: int  # Σ u·q    (u = supply-divider ADC counts)
    su: int  # Σ u       — only used to sanity-check that the divider is wired
    qmax: int
    fs: int  # sample rate, Hz
    vdda_mv: int
    shunt_uohm: int
    kdiv_milli: int

    @property
    def volts_per_count(self) -> float:
        return (self.vdda_mv / 1000.0) / _ADC_FULL_SCALE

    @property
    def shunt_ohms(self) -> float:
        return self.shunt_uohm / 1_000_000.0

    @property
    def duration_s(self) -> float:
        """Integration time, derived from the sample count rather than the wall clock —
        it is the duration the energy figure actually covers."""
        return self.n / self.fs if self.fs else 0.0

    @property
    def mean_current_a(self) -> float:
        if not self.n:
            return 0.0
        return (self.sq / self.n) * self.volts_per_count / self.shunt_ohms

    @property
    def peak_current_a(self) -> float:
        return self.qmax * self.volts_per_count / self.shunt_ohms

    @property
    def mean_supply_v(self) -> float:
        """The rail feeding the ESP32, read back through the divider. Should be ~5 V."""
        if not self.n:
            return 0.0
        k = self.kdiv_milli / 1000.0
        return (self.su / self.n) * self.volts_per_count * k

    @property
    def charge_c(self) -> float:
        if not self.fs:
            return 0.0
        return self.sq * self.volts_per_count / self.shunt_ohms / self.fs

    @property
    def energy_j(self) -> float:
        """E = Σ V_esp·I·dt, with V_esp = (K·u − q)·L and I = q·L/R.

        Falls out of the sums as (L²/R)·(1/fs)·Σ(K·u·q − q²) — see the firmware header.
        """
        if not self.fs:
            return 0.0
        k = self.kdiv_milli / 1000.0
        lsb = self.volts_per_count
        return (lsb * lsb / self.shunt_ohms) * (k * self.suq - self.sqq) / self.fs

    @property
    def mean_power_w(self) -> float:
        d = self.duration_s
        return self.energy_j / d if d else 0.0

    @classmethod
    def parse(cls, line: str) -> Optional["MeterFrame"]:
        """Parse one frame line, or return None if it isn't one (command acks share the wire)."""
        try:
            d = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            return None
        if not isinstance(d, dict) or "sq" not in d:
            return None
        try:
            return cls(
                t_ms=int(d["t"]),
                n=int(d["n"]),
                ovr=int(d["ovr"]),
                sq=int(d["sq"]),
                sqq=int(d["sqq"]),
                suq=int(d["suq"]),
                su=int(d["su"]),
                qmax=int(d["qmax"]),
                fs=int(d["fs"]),
                vdda_mv=int(d["vdda"]),
                shunt_uohm=int(d["r"]),
                kdiv_milli=int(d["kdiv"]),
            )
        except (KeyError, TypeError, ValueError):
            return None


@dataclass
class PowerResult:
    """Outcome of a `nff power measure` run. ``ok`` is the single field to branch on."""

    ok: bool
    command: Optional[str] = None
    duration_s: float = 0.0
    energy_j: float = 0.0
    marginal_energy_j: float = 0.0
    baseline_power_w: float = 0.0
    mean_current_ma: float = 0.0
    peak_current_ma: float = 0.0
    supply_v: float = 0.0
    samples: int = 0
    overruns: int = 0
    exit_code: Optional[int] = None
    shunt_uohm: int = 0
    budget_j: Optional[float] = None
    within_budget: Optional[bool] = None
    error: Optional[str] = None
    warnings: list = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "ok": self.ok,
            "command": self.command,
            "duration_s": round(self.duration_s, 4),
            "energy_j": round(self.energy_j, 6),
            "marginal_energy_j": round(self.marginal_energy_j, 6),
            "baseline_power_w": round(self.baseline_power_w, 6),
            "mean_current_ma": round(self.mean_current_ma, 3),
            "peak_current_ma": round(self.peak_current_ma, 3),
            "supply_v": round(self.supply_v, 3),
            "samples": self.samples,
            "overruns": self.overruns,
            "exit_code": self.exit_code,
            "shunt_uohm": self.shunt_uohm,
            "budget_j": self.budget_j,
            "within_budget": self.within_budget,
            "error": self.error,
            "warnings": self.warnings,
        }

    def summary(self) -> str:
        if not self.ok:
            return f"ERROR: {self.error or 'measurement failed'}"
        lines = [
            f"OK: {self.marginal_energy_j:.3f} J over {self.duration_s:.1f} s"
            f"  ({self.command or 'idle'})",
            f"  total energy    {self.energy_j:.3f} J   "
            f"(idle baseline {self.baseline_power_w * 1000:.0f} mW accounts for "
            f"{self.energy_j - self.marginal_energy_j:.3f} J of it)",
            f"  mean current    {self.mean_current_ma:.1f} mA",
            f"  peak current    {self.peak_current_ma:.1f} mA",
            f"  supply          {self.supply_v:.2f} V",
            f"  samples         {self.samples:,} @ 0 overruns",
        ]
        if self.budget_j is not None:
            verdict = "within" if self.within_budget else "OVER"
            lines.append(f"  budget          {verdict} ({self.budget_j:.3f} J)")
        for w in self.warnings:
            lines.append(f"  warning: {w}")
        return "\n".join(lines)


# --------------------------------------------------------------------------- config


def get_config() -> dict:
    cfg = config.get_power_config()
    return cfg


def _cal_from_config() -> tuple:
    cfg = get_config()
    return (
        int(cfg.get("shunt_uohm") or 1_000_000),
        int(cfg.get("kdiv_milli") or 2000),
        int(cfg.get("vdda_mv") or 3300),
    )


# ---------------------------------------------------------------------------- meter


class Meter:
    """Serial client for the nff-power-meter. Use as a context manager."""

    def __init__(self, port: str, baud: int = _BAUD):
        self.port = port
        self.baud = baud
        self._sp: Optional[serial.Serial] = None

    def __enter__(self) -> "Meter":
        self.open()
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def open(self) -> None:
        # Idempotent: open_calibrated() hands back an already-open meter, and callers then
        # (reasonably) use it as a context manager. Re-opening a port we already hold is an
        # Access-Denied on Windows, so absorb the second open rather than making every call
        # site remember which state it got the meter in.
        if self._sp is not None:
            return
        try:
            self._sp = serial.Serial(self.port, self.baud, timeout=0.2)
        except serial.SerialException as exc:
            raise PowerError(f"could not open meter on {self.port}: {exc}") from exc
        # The meter greets with an INFO line on reset; discard whatever is queued so the
        # first frame we read belongs to a command we actually sent.
        time.sleep(0.1)
        self._sp.reset_input_buffer()

    def close(self) -> None:
        if self._sp is not None:
            try:
                self._sp.close()
            finally:
                self._sp = None

    def _require(self) -> serial.Serial:
        if self._sp is None:
            raise PowerError("meter is not open")
        return self._sp

    def send(self, line: str) -> None:
        sp = self._require()
        try:
            sp.write((line + "\n").encode("ascii"))
            sp.flush()
        except serial.SerialException as exc:
            raise PowerError(f"meter write failed: {exc}") from exc

    def _read_line(self, deadline: float) -> Optional[str]:
        sp = self._require()
        while time.monotonic() < deadline:
            try:
                raw = sp.readline()
            except serial.SerialException as exc:
                raise PowerError(f"meter read failed: {exc}") from exc
            if raw:
                return raw.decode("utf-8", errors="replace").strip()
        return None

    def read_frame(self, timeout: float = _FRAME_TIMEOUT) -> MeterFrame:
        deadline = time.monotonic() + timeout
        while True:
            line = self._read_line(deadline)
            if line is None:
                raise PowerError(
                    f"no frame from the meter on {self.port} within {timeout:.0f}s "
                    "— is nff-power-meter flashed?"
                )
            frame = MeterFrame.parse(line)
            if frame is not None:
                return frame

    def info(self, timeout: float = _PROBE_TIMEOUT) -> dict:
        self.send("INFO")
        deadline = time.monotonic() + timeout
        while True:
            line = self._read_line(deadline)
            if line is None:
                raise PowerError(f"meter on {self.port} did not answer INFO")
            if not isinstance(line, str):
                continue
            try:
                d = json.loads(line)
            except (json.JSONDecodeError, ValueError, TypeError):
                continue
            if isinstance(d, dict) and d.get("meter") == "nff-power-meter":
                return d

    def zero(self, timeout: float = _PROBE_TIMEOUT) -> None:
        """Reset the accumulators, and CONFIRM the meter did it.

        Fire-and-forget is not good enough here. A ZERO that is silently dropped leaves the
        meter accumulating from boot, so the next frame covers a window that has nothing to
        do with the command being measured — and the result looks entirely healthy
        (plausible current, no overruns) while being wrong by whatever the uptime was.
        Observed in bring-up: a 10 s run reported 91 s and 26 J. So: wait for the ack.
        """
        self.send("ZERO")
        deadline = time.monotonic() + timeout
        while True:
            line = self._read_line(deadline)
            if line is None:
                raise PowerError(
                    f"meter on {self.port} did not acknowledge ZERO — refusing to measure "
                    "against an unknown accumulation window"
                )
            if isinstance(line, str) and '"zeroed"' in line:
                return

    def snap(self, timeout: float = _FRAME_TIMEOUT) -> MeterFrame:
        self.send("SNAP")
        return self.read_frame(timeout)

    def wirecheck(self, timeout: float = _FRAME_TIMEOUT) -> dict:
        """Actively probe whether PA0/PA1 are connected to anything. See check_wiring()."""
        self.send("WIRECHECK")
        deadline = time.monotonic() + timeout
        while True:
            line = self._read_line(deadline)
            if line is None:
                raise PowerError(f"meter on {self.port} did not answer WIRECHECK")
            if not isinstance(line, str):
                continue
            try:
                d = json.loads(line)
            except (json.JSONDecodeError, ValueError, TypeError):
                continue
            if isinstance(d, dict) and d.get("wirecheck"):
                return d

    def stream(self, on: bool) -> None:
        self.send(f"STREAM {1 if on else 0}")

    def push_calibration(
        self, shunt_uohm: int, kdiv_milli: int, vdda_mv: int
    ) -> None:
        """Calibration is owned by the host (~/.nff/config.json) and pushed down on every
        connect, so the meter stays stateless across resets and there is one source of truth."""
        self.send(f"CAL {shunt_uohm}")
        self.send(f"KDIV {kdiv_milli}")
        self.send(f"VDDA {vdda_mv}")
        # Drain the three INFO acks so they can't be mistaken for a frame later.
        deadline = time.monotonic() + _PROBE_TIMEOUT
        while time.monotonic() < deadline:
            if not self._read_line(deadline):
                break


# ------------------------------------------------------------------------ discovery


def candidate_ports() -> list:
    """Serial ports that could plausibly be the meter — ST-Link Virtual COM Ports only.

    Deliberately NOT every port. Opening a port asserts DTR, which reboots an ESP32; a
    blind scan looking for the meter would reset the very device we are trying to measure
    (and stall `nff doctor` for seconds per port). The Nucleo's ST-Link is STMicro's
    VID 0x0483, so filter on that and touch nothing else.
    """
    return [
        p.device
        for p in serial.tools.list_ports.comports()
        if (p.vid == _STLINK_VID)
    ]


def find_meter(port: Optional[str] = None) -> Optional[str]:
    """Return the serial port the meter is on, or None.

    An explicit or configured port is still probed rather than trusted — a stale
    ~/.nff/config.json pointing at a port some other board now owns would otherwise send
    `CAL`/`ZERO` at the wrong device.
    """
    if port:
        candidates = [port]
    else:
        candidates = []
        cfg = get_config()
        if cfg.get("port"):
            candidates.append(cfg["port"])
        candidates += [p for p in candidate_ports() if p not in candidates]

    for cand in candidates:
        try:
            with Meter(cand) as m:
                m.info()
                return cand
        except Exception:
            # Anything at all — port held by another process, a board that isn't the meter,
            # a hostile pile of bytes on the wire — means "not the meter here", never a crash.
            continue
    return None


def status(port: Optional[str] = None) -> dict:
    """Is a meter attached, and is it calibrated? Never raises — `nff doctor` calls this."""
    try:
        cfg = get_config()
        found = find_meter(port)
    except Exception as exc:  # pragma: no cover - defensive
        return {"ok": False, "attached": False, "error": f"{type(exc).__name__}: {exc}"}
    if not found:
        return {
            "ok": False,
            "attached": False,
            "error": "no nff-power-meter found on any serial port",
            "fix": "Flash nff-power-meter/ to the Nucleo (`pio run -t upload`) and check wiring",
        }
    return {
        "ok": True,
        "attached": True,
        "port": found,
        "calibrated": bool(cfg.get("calibrated")),
        "shunt_uohm": int(cfg.get("shunt_uohm") or 1_000_000),
        "kdiv_milli": int(cfg.get("kdiv_milli") or 2000),
        "vdda_mv": int(cfg.get("vdda_mv") or 3300),
    }


# ---------------------------------------------------------------------- measurement


def open_calibrated(port: Optional[str]) -> Meter:
    found = find_meter(port)
    if not found:
        raise PowerError(
            "no nff-power-meter found — flash nff-power-meter/ to the Nucleo and check "
            "it is the only board on USB (the ESP32 must NOT be plugged into the PC)"
        )
    shunt, kdiv, vdda = _cal_from_config()
    meter = Meter(found)
    meter.open()
    meter.push_calibration(shunt, kdiv, vdda)
    return meter


def check_window(frame: MeterFrame, wall_s: float) -> Optional[str]:
    """Two independent cross-checks on a frame, or None if it is sound.

    The overrun flag catches *lost* samples. It does not catch a frame whose accumulation
    WINDOW is wrong — and that failure mode is far nastier, because the result looks
    perfectly healthy: plausible current, zero overruns, ok=true, and an energy figure
    inflated by whatever the window was off by. Both of these were caught in bring-up.

      (a) The meter's own elapsed clock must agree with how long the host actually waited.
          Disagreement means a ZERO went missing and we are integrating from boot.
      (b) The samples delivered must match the rate the firmware claims. Disagreement means
          the ADC is not running at FS_HZ, so every dt — and so every joule — is scaled wrong.

    Neither check can be satisfied by a broken meter that merely looks confident.
    """
    meter_s = frame.t_ms / 1000.0

    if abs(meter_s - wall_s) > max(0.5, 0.2 * wall_s):
        return (
            f"the meter accumulated over {meter_s:.1f}s but the host only waited {wall_s:.1f}s "
            "— a ZERO was lost, so this window is not the command's. No energy figure reported."
        )

    if meter_s > 0.5:
        actual_fs = frame.n / meter_s
        if abs(actual_fs - frame.fs) > 0.05 * frame.fs:
            return (
                f"the meter claims {frame.fs:,} Hz but delivered {actual_fs:,.0f} Hz over "
                f"{meter_s:.1f}s — every dt is wrong, so the energy would be scaled by "
                f"{actual_fs / frame.fs:.2f}×. No energy figure reported."
            )

    return None


# A node held by a real source springs straight back after being driven — the divider is a
# ~500 Ω source, the shunt node a near short to ground, so both restore in nanoseconds and read
# within a few tens of counts of where they were. An unconnected pin has nothing to restore it:
# measured on the bench, a floating PA0 moved 1746 counts and a floating PA1 moved 2425. There
# is a very wide gap to sit in; 400 leaves room for a noisy but genuinely-wired shunt node.
_RESTORE_COUNTS = 400


def check_connectivity(
    probe: dict, kdiv_milli: int = 2000, vdda_mv: int = 3300
) -> Optional[str]:
    """Is the rig actually wired, per the meter's active drive-and-release probe?

    This cannot be inferred from the readings, and two weaker designs failed here first.
    A floating ADC pin does not read zero — it holds charge — so an unwired rig reported a
    confident 590 mA and a supply rail of 4.94 V, and passive plausibility waved it through.
    A pull-up/pull-down probe then reported both pins "connected" with nothing attached at
    all, because the F446's ADC cannot see a pad in digital-input mode.

    What survives: drive the node, release it, and see whether anything pulls it back. And for
    the divider, require BOTH that it reads like a 5 V rail AND that it springs back. Either
    test alone can be passed by luck — the bench's floating PA1 sat at a wholly convincing
    4.94 V — but it restored to 0.5 V, and nothing that is actually connected does that.
    """
    q_base = int(probe.get("q_base", 0))
    q_lo = int(probe.get("q_lo", 0))
    u_base = int(probe.get("u_base", 0))
    u_lo = int(probe.get("u_lo", 0))
    u_hi = int(probe.get("u_hi", 0))

    lsb = (vdda_mv / 1000.0) / _ADC_FULL_SCALE
    supply_v = u_base * lsb * (kdiv_milli / 1000.0)
    u_moved = max(abs(u_lo - u_base), abs(u_hi - u_base))
    q_moved = abs(q_lo - q_base)

    problems = []

    if not 3.5 <= supply_v <= 6.5:
        problems.append(
            f"PA1 (the 5 V divider) reads {supply_v:.2f} V, which is not a 5 V rail"
        )
    elif u_moved > _RESTORE_COUNTS:
        problems.append(
            f"PA1 (the 5 V divider) is not connected — it reads a plausible {supply_v:.2f} V, "
            f"but when driven it does not spring back ({u_moved} counts off), so nothing is "
            "holding it there"
        )

    if q_moved > _RESTORE_COUNTS:
        problems.append(
            f"PA0 (the shunt) is not connected — when driven, the node does not spring back "
            f"({q_moved} counts off)"
        )

    if problems:
        return (
            "; ".join(problems)
            + ". Wire the rig per nff-power-meter/README.md. Nothing was measured."
        )
    return None


def check_wiring(frame: MeterFrame) -> Optional[str]:
    """Sanity-check a frame against what the rig must look like when correctly wired.

    Weaker than check_connectivity() — these are plausibility checks on the values, and a
    floating pin can land on a plausible value by chance. Use both.
    """
    supply = frame.mean_supply_v
    if not 3.0 <= supply <= 7.0:
        return (
            f"the supply divider reads {supply:.2f} V, which is not a 5 V rail. Either the "
            "divider ratio is wrong (default 1k/1k = 2.000) or it is miswired. Every joule "
            "scales directly with this voltage, so no figure is reported."
        )

    # A clipped shunt channel means the drop across the shunt exceeded VDDA — the peaks were
    # truncated, so the energy is an under-count in exactly the samples that matter most.
    if frame.qmax >= 4090:
        return (
            "the shunt channel saturated the ADC (hit full scale). The drop across the shunt "
            "exceeded 3.3 V, so current peaks were clipped and the energy is an under-count. "
            "Use a smaller shunt."
        )

    return None


def selftest(
    seconds: float = 25.0,
    window_s: float = 0.5,
    port: Optional[str] = None,
    emit: Optional[Callable[[str], None]] = None,
) -> dict:
    """Prove the shunt is really wired, by watching a load that MOVES.

    The connectivity probe answers "is something holding this pin?". This answers the question
    that actually matters: "does the current we report track the current the device draws?"

    Flash esp32-loadtest/ to the ESP32 first. It walks a four-step staircase — idle, CPU,
    radio, scan — spanning roughly 40 mA to 200 mA. A correctly wired meter follows it. A
    floating PA0 sits flat at whatever charge it happens to hold, which during bring-up was a
    wholly convincing 57 mA — indistinguishable from a real idle ESP32 until you make the load
    move.
    """
    swing_floor_ma = 25.0  # the staircase spans >100 mA; anything under this is not following it

    with open_calibrated(port) as meter:
        meter.zero()
        samples = []
        deadline = time.monotonic() + seconds
        last = 0.0
        while time.monotonic() < deadline:
            meter.zero()
            time.sleep(window_s)
            f = meter.snap()
            ma = f.mean_current_a * 1000
            samples.append(ma)
            if emit and abs(ma - last) > 1.0:
                bar = "#" * max(1, int(ma / 4))
                emit(f"  {ma:6.1f} mA  {bar}")
                last = ma
            rail = f.mean_supply_v
            ovr = f.ovr

    if not samples:
        return {"ok": False, "error": "no samples — is the meter attached?"}

    lo, hi = min(samples), max(samples)
    swing = hi - lo
    wired = swing >= swing_floor_ma

    result = {
        "ok": bool(wired),
        "min_ma": round(lo, 1),
        "max_ma": round(hi, 1),
        "swing_ma": round(swing, 1),
        "rail_v": round(rail, 2),
        "overruns": ovr,
        "windows": len(samples),
    }
    if not wired:
        result["error"] = (
            f"the reported current barely moved ({swing:.1f} mA swing, {lo:.1f}-{hi:.1f} mA) while "
            "the ESP32 should have been stepping across ~40-200 mA. The meter is NOT following the "
            "load. Either PA0 is not on the shunt's ESP32-GND leg, or esp32-loadtest is not running "
            "(is the ESP32 powered from the Nucleo's 5V pin, with its own USB unplugged?)."
        )
    return result


def sample(duration_s: float, port: Optional[str] = None) -> MeterFrame:
    """Accumulate for `duration_s` and return the cumulative frame."""
    with open_calibrated(port) as meter:
        problem = check_connectivity(meter.wirecheck(), *_cal_from_config()[1:])
        if problem:
            raise PowerError(problem)
        meter.zero()
        started = time.monotonic()
        time.sleep(duration_s)
        frame = meter.snap()
    problem = check_window(frame, time.monotonic() - started) or check_wiring(frame)
    if problem:
        raise PowerError(problem)
    return frame


def measure(
    during: Optional[str] = None,
    duration_s: Optional[float] = None,
    baseline_s: float = 5.0,
    max_joules: Optional[float] = None,
    port: Optional[str] = None,
    emit: Optional[Callable[[str], None]] = None,
) -> PowerResult:
    """Measure the energy a command costs the device.

    Runs `during` as a subprocess while the meter accumulates, then subtracts an idle
    baseline so the answer is the *marginal* energy the command cost — over and above the
    device merely being powered on. With no `during`, just accumulates for `duration_s`.
    """
    if not during and duration_s is None:
        return PowerResult(ok=False, error="pass during= (a command) or duration_s=")

    cfg = get_config()
    warnings = []
    if not cfg.get("calibrated"):
        # Say what is actually unknown, and what it does and does not affect. "It's a guess" is
        # both wrong (the resistor's value IS known) and useless (it doesn't say what to distrust).
        nominal = int(cfg.get("shunt_uohm") or 1_000_000) / 1_000_000
        warnings.append(
            f"UNCALIBRATED — using a nominal {nominal:.2f} Ω shunt. The real ground path is that "
            "resistor plus breadboard contact resistance (~0.1-0.4 Ω), so the ABSOLUTE energy may "
            "be off by ~10-30%. RELATIVE comparisons on this same, un-rewired rig are unaffected "
            "— which is all --max-joules regression gating needs. Run `nff power calibrate` if you "
            "need a defensible absolute figure."
        )

    try:
        meter = open_calibrated(port)
    except PowerError as exc:
        return PowerResult(ok=False, error=str(exc))

    with meter:
        # 0. Is the rig even wired? Ask before measuring, not after — a floating pin produces
        #    confident, plausible, meaningless numbers.
        try:
            problem = check_connectivity(meter.wirecheck(), *_cal_from_config()[1:])
        except PowerError as exc:
            return PowerResult(ok=False, error=str(exc))
        if problem:
            return PowerResult(ok=False, command=during, error=problem)

        # 1. Idle baseline. Everything the board burns just being on — LDO, USB-UART chip,
        #    power LED, WiFi keepalive — so we can subtract it back out below.
        baseline_w = 0.0
        if baseline_s > 0:
            if emit:
                emit(f"Sampling idle baseline for {baseline_s:.0f}s…")
            try:
                meter.zero()
            except PowerError as exc:
                return PowerResult(ok=False, error=str(exc))
            started = time.monotonic()
            time.sleep(baseline_s)
            base = meter.snap()
            if base.ovr:
                return PowerResult(
                    ok=False,
                    error=f"meter dropped samples during the baseline ({base.ovr} overruns)",
                    overruns=base.ovr,
                )
            problem = check_window(base, time.monotonic() - started)
            if problem:
                return PowerResult(ok=False, error=f"baseline: {problem}")
            baseline_w = base.mean_power_w
            if emit:
                emit(
                    f"  idle: {base.mean_current_a * 1000:.1f} mA "
                    f"({baseline_w * 1000:.0f} mW)"
                )

        # 2. Accumulate across the command.
        try:
            meter.zero()
        except PowerError as exc:
            return PowerResult(ok=False, error=str(exc))
        started = time.monotonic()
        exit_code = None
        if during:
            if emit:
                emit(f"Measuring: {during}")
            proc = subprocess.Popen(
                during,
                shell=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
            )
            assert proc.stdout is not None
            for line in proc.stdout:
                if emit:
                    emit(f"  | {line.rstrip()}")
            exit_code = proc.wait()
        else:
            time.sleep(float(duration_s or 0))

        frame = meter.snap()
        wall_s = time.monotonic() - started

    # 3. Verdict. Three ways this can fail to produce an honest number, and each is reported
    #    as ok=False rather than as a confident-looking joules figure.

    #    (a) The accumulation window doesn't describe the command we just ran, or the rig
    #        isn't wired the way the energy math assumes (a floating ADC pin reads garbage
    #        with total confidence).
    problem = check_window(frame, wall_s) or check_wiring(frame)
    if problem:
        return PowerResult(
            ok=False, command=during, samples=frame.n, exit_code=exit_code, error=problem,
        )

    #    (b) Samples were lost, so the accumulators are an under-count.
    if frame.ovr:
        return PowerResult(
            ok=False,
            command=during,
            samples=frame.n,
            overruns=frame.ovr,
            exit_code=exit_code,
            error=(
                f"meter dropped samples ({frame.ovr} ADC overruns) — the energy total is an "
                "under-count and is not reported. Re-run; if it persists, the host is starving "
                "the meter's ISR."
            ),
        )

    energy = frame.energy_j
    marginal = energy - baseline_w * frame.duration_s

    #    (c) The command itself failed — it burned energy, but calling that "the cost of an
    #        OTA" would describe a run that never did what it claims to have measured.
    if exit_code not in (None, 0):
        return PowerResult(
            ok=False,
            command=during,
            duration_s=frame.duration_s,
            energy_j=energy,
            marginal_energy_j=marginal,
            baseline_power_w=baseline_w,
            mean_current_ma=frame.mean_current_a * 1000,
            peak_current_ma=frame.peak_current_a * 1000,
        supply_v=frame.mean_supply_v,
            samples=frame.n,
            overruns=0,
            exit_code=exit_code,
            shunt_uohm=frame.shunt_uohm,
            warnings=warnings,
            error=(
                f"the measured command failed (exit {exit_code}) — the energy figure above "
                "describes a failed run, not a successful one"
            ),
        )

    within = None if max_joules is None else marginal <= max_joules

    return PowerResult(
        ok=True,
        command=during,
        duration_s=frame.duration_s,
        energy_j=energy,
        marginal_energy_j=marginal,
        baseline_power_w=baseline_w,
        mean_current_ma=frame.mean_current_a * 1000,
        peak_current_ma=frame.peak_current_a * 1000,
        supply_v=frame.mean_supply_v,
        samples=frame.n,
        overruns=0,
        exit_code=exit_code,
        shunt_uohm=frame.shunt_uohm,
        budget_j=max_joules,
        within_budget=within,
        warnings=warnings,
    )


def solve_shunt_uohm(
    frame_with_load: MeterFrame,
    load_current_ma: float,
    frame_without_load: Optional[MeterFrame] = None,
) -> int:
    """Solve for the effective resistance of the ground path, against a known current.

    DIFFERENTIAL, and it has to be. The shunt carries the device's own current *plus* the
    calibration load's, while the multimeter in series with that load sees only the load's. So
    comparing the meter's total against the multimeter's load-only reading solves for the wrong
    resistance — badly wrong, because the ESP32 draws more than the load does. Take the
    difference instead, and the device's contribution cancels:

        R_eff = (V_shunt_with_load - V_shunt_idle) / I_load

    The one constant this yields absorbs the resistor's tolerance, the ±2% VDDA regulator error,
    the ADC gain error AND the breadboard contact resistance — none of which are separable, and
    all of which move when a jumper is re-seated.

    Passing no `frame_without_load` falls back to the absolute form, which is only valid when the
    load is the ONLY thing drawing through the shunt.
    """
    if load_current_ma <= 0:
        raise PowerError("the reference current must be positive")
    if not frame_with_load.n:
        raise PowerError("the meter returned no samples")

    def _v(f: MeterFrame) -> float:
        return (f.sq / f.n) * f.volts_per_count

    v_load = _v(frame_with_load)
    if frame_without_load is not None:
        if not frame_without_load.n:
            raise PowerError("the idle reference frame has no samples")
        v_load -= _v(frame_without_load)

    if v_load <= 0:
        raise PowerError(
            "switching the load in did not raise the shunt voltage — the meter is not seeing it. "
            "Check the load really is drawing through the shunt (across the DEVICE's 3V3 and GND, "
            "not the Nucleo's)."
        )
    return int(round(v_load / (load_current_ma / 1000.0) * 1_000_000))
