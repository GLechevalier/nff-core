"""Price each thing nff makes an ESP32 do.

Serves the 1 MiB blob that esp32-nffload downloads, records a continuous current trace from the
meter, and segments it on the light-sleep troughs the firmware puts between phases — so no marker
wires are needed on an already-crowded breadboard.

    python nff-power-meter/profile.py            # prints the BLOB_URL to put in wifi_secrets.h
    python nff-power-meter/profile.py --record   # record and report

Energy is summed from the meter's own integer accumulators (the deltas between consecutive
frames), never from the sampled current — so a dropped serial frame costs resolution, not joules.
"""

import argparse
import http.server
import socket
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "nff"))

from nff.tools.power import Meter, MeterFrame, PowerError, open_calibrated  # noqa: E402

PORT = 8099

# 1 MiB — a realistic ESP32 OTA image. The FIRST BYTE MUST BE 0xE9: esp_ota_write() sniffs the
# app-image magic on the first chunk and rejects anything else with ESP_ERR_OTA_VALIDATE_FAILED.
# Serve plain filler and the device's OTA aborts on the very first chunk — the phase then finishes
# in a fraction of a second and reports a wonderfully low energy for work it never performed.
BLOB = b"\xe9" + b"\xa5" * (1024 * 1024 - 1)

# In firmware order. Every one is bracketed by a light-sleep trough.
PHASES = [
    ("idle", "radio off, CPU parked"),
    ("cpu", "radio off, CPU flat out"),
    ("connect", "associate to the AP from cold"),
    ("wifi_idle", "connect, then sit there connected"),
    ("download", "connect, then pull 1 MiB over the radio"),
    ("flash_write", "erase + write 1 MiB, radio OFF"),
    ("ota", "connect, pull 1 MiB, write it to flash — the real thing"),
]

# The firmware's markers are esp_light_sleep_start(). The board still burns its LDO/USB-UART/LED
# quiescent, so the trough is not near zero — it is just unmistakably below any active phase.
TROUGH_MA = 30.0
MIN_TROUGH_S = 1.0
LONG_TROUGH_S = 3.5  # the cycle-start marker is 5 s; anything over this is it


class _Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Length", str(len(BLOB)))
        self.end_headers()
        self.wfile.write(BLOB)

    def log_message(self, *a):
        pass


def lan_ip() -> str:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))  # no packet is sent; just picks the outbound interface
        return s.getsockname()[0]
    finally:
        s.close()


def serve() -> str:
    srv = http.server.ThreadingHTTPServer(("0.0.0.0", PORT), _Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return f"http://{lan_ip()}:{PORT}/blob.bin"


def record(seconds: float, port=None) -> list:
    """A continuous trace of (t, mean_mA, energy_J) per ~100 ms interval.

    The meter's frames are CUMULATIVE, so differencing consecutive frames gives the exact energy
    of each interval straight from the integer accumulators. A dropped frame merges two intervals
    rather than losing their joules.
    """
    out = []
    with open_calibrated(port) as meter:
        meter.zero()
        meter.stream(True)
        try:
            prev = meter.read_frame()
            t0 = time.monotonic()
            while time.monotonic() - t0 < seconds:
                f = meter.read_frame()
                dn = f.n - prev.n
                if dn <= 0:
                    continue
                dt = (f.t_ms - prev.t_ms) / 1000.0
                lsb, r = f.volts_per_count, f.shunt_ohms
                k = f.kdiv_milli / 1000.0
                ma = (f.sq - prev.sq) / dn * lsb / r * 1000
                joules = (lsb * lsb / r) * (
                    k * (f.suq - prev.suq) - (f.sqq - prev.sqq)
                ) / f.fs
                out.append((f.t_ms / 1000.0, dt, ma, joules, f.ovr))
                prev = f
        finally:
            try:
                meter.stream(False)
            except PowerError:
                pass
    return out


def segment(trace: list) -> list:
    """Split the trace into (kind, t_start, duration, energy) runs, where kind is 'trough' or
    'phase'. Troughs are the firmware's light-sleep markers."""
    runs = []
    cur_kind, cur = None, []
    for row in trace:
        kind = "trough" if row[2] < TROUGH_MA else "phase"
        if kind != cur_kind and cur:
            runs.append((cur_kind, cur))
            cur = []
        cur_kind, _ = kind, cur.append(row)
    if cur:
        runs.append((cur_kind, cur))

    out = []
    for kind, rows in runs:
        dur = sum(r[1] for r in rows)
        energy = sum(r[3] for r in rows)
        out.append((kind, rows[0][0], dur, energy, rows))
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--record", action="store_true")
    ap.add_argument("--seconds", type=float, default=180.0)
    args = ap.parse_args()

    url = serve()
    print(f"Serving a {len(BLOB) // 1024} KiB blob at:  {url}")
    if not args.record:
        print("\nPut that in esp32-nffload/include/wifi_secrets.h as BLOB_URL, flash it,")
        print("unplug the ESP32's USB, then re-run with --record.")
        print("Serving until Ctrl-C (the ESP32 needs this up while it downloads)...")
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            return 0

    print(f"Recording {args.seconds:.0f}s…\n")
    trace = record(args.seconds)
    if not trace:
        print("no data from the meter")
        return 1

    if any(r[4] for r in trace):
        print("WARNING: the meter reported overruns — energy is an under-count. Not reporting.")
        return 1

    runs = segment(trace)
    troughs = [i for i, r in enumerate(runs) if r[0] == "trough"]
    starts = [i for i in troughs if runs[i][2] >= LONG_TROUGH_S]

    if len(starts) < 2:
        print("Could not find two cycle-start markers (the 5 s light-sleep trough).")
        print("Segments seen:")
        for kind, t, dur, e, _ in runs:
            print(f"  {kind:7} t={t:6.1f}s  {dur:5.1f}s  {e:7.3f} J")
        return 1

    # One full cycle: the phases between the first two long troughs.
    phases = [r for r in runs[starts[0] : starts[1]] if r[0] == "phase"]

    print(f"{'phase':13} {'what it is':44} {'time':>7} {'mean mA':>8} {'ENERGY J':>9}")
    print("-" * 88)
    idle_w = None
    for (name, desc), (_, _, dur, energy, rows) in zip(PHASES, phases):
        ma = sum(r[2] * r[1] for r in rows) / dur if dur else 0
        if name == "idle":
            idle_w = energy / dur if dur else 0
        print(f"{name:13} {desc:44} {dur:6.1f}s {ma:8.1f} {energy:9.3f}")

    if len(phases) < len(PHASES):
        print(f"\n(only {len(phases)}/{len(PHASES)} phases captured — record for longer)")

    if idle_w is not None:
        print()
        print(f"Idle floor: {idle_w * 1000:.0f} mW. MARGINAL cost of each phase (above simply")
        print("being powered on) — this is the number that means something:")
        print()
        for (name, _), (_, _, dur, energy, _) in list(zip(PHASES, phases))[1:]:
            print(f"  {name:13} {energy - idle_w * dur:+7.3f} J   over {dur:5.1f}s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
