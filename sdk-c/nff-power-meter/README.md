# nff-power-meter

Turns an STM32 Nucleo-F446RE into a low-side shunt ammeter and energy accumulator, so
`nff power` can answer *"what did that OTA cost the device, in joules?"*.

Built from a resistor and a breadboard — no INA219/INA228, no current-sense amplifier,
no Power Profiler Kit.

## Wiring

```
   Nucleo 5V ─────────────────────────────► ESP32 VIN   (its onboard LDO makes 3V3)

   Nucleo PA0 ◄─────────┬────────────────── ESP32 GND
                        │
                     [ 1 Ω ]  ← shunt, ¼ W
                        │
   Nucleo GND ──────────┴──────────────────

   Nucleo 5V ──[10k]──┬──[10k]── Nucleo GND
                      └───────── Nucleo PA1      (rail sense; 1k/1k works too)
```

Only the Nucleo connects to the PC.

### Three rules

**1. The ESP32 must NOT be plugged into the PC's USB.** A USB cable ties its ground to PC
ground, which shorts the shunt out — you would read ~0 mA and never know why. This is why
`nff flash` and `nff monitor` cannot be profiled on this rig; they need USB. `nff ota` goes
over WiFi and needs none, which is the whole point.

**2. Use 1 Ω, and don't go above it.** The shunt sits in the ground return, so the whole ESP32
board floats up by I×R. That's harmless to the ESP32 itself — its LDO regulates 3V3 relative to
the board's *own* ground, so the chip still sees a clean 3.3 V, and the drop is absorbed by the
LDO's input headroom.

But that headroom is thin, and thinner than it looks. The Nucleo's 5V pin sits near **4.7 V**
when USB-powered (the ST-Link protection diode drops the rest), and an AMS1117 needs ~4.4 V in.
So a 300 mA WiFi peak across 1 Ω lands you *at* the dropout floor, not comfortably above it.
If the ESP32 resets during TX, in order of preference: drop to 0.5 Ω (two 1 Ω in parallel);
fit a 100 µF bulk cap across VIN↔ESP32-GND; or power the Nucleo from an external 7–12 V supply
on its own VIN, which gives a solid 5.0 V rail.

No 1 Ω on hand? **Ten 10 Ω in parallel** makes one, and averages their tolerances down.

**3. The divider can be 1k/1k or 10k/10k — use what you have.** Both settle comfortably. The
ADC input is an RC: its 4 pF sample-and-hold charges through your source plus ~6 kΩ of internal
mux resistance. A 10k/10k divider is a 5 kΩ source, so τ ≈ 44 ns and 12-bit settling needs ~9τ
≈ 0.4 µs — against the 2.49 µs that 56 cycles gives you at a 22.5 MHz ADC clock. About 6× margin.
(The 480-cycle figure you'll see quoted is the spec for a ~200 kΩ source, which a divider is
nowhere near.) The ratio is 2.0 either way, so `kdiv` is unchanged.

1k/1k has more margin still but draws 2.5 mA from a rail that's already tight for the ESP32's
peaks; 10k/10k draws 0.25 mA but picks up marginally more noise on a breadboard. Neither
difference matters here.

*Do verify it rather than trust it*, though: a source too high-impedance for its sampling window
biases the rail **low, silently**, and every joule scales with the rail. Compare the rail
`nff power monitor` reports against your multimeter. If PA1 reads low, drop to 1k/1k or fit a
100 nF cap from PA1 to ground.

## Build and flash

```
pio run -t upload
```

The ST-Link's Virtual COM Port is the host link — the same USB cable you program with. No
second adapter.

## Protocol

921600 baud, line-based. Calibration lives on the **host** (`~/.nff/config.json`) and is pushed
down on connect, so the meter is stateless across resets and there is one source of truth.

| Host → meter | Effect |
|---|---|
| `PING` | liveness |
| `INFO` | current config |
| `ZERO` | reset the accumulators and the clock |
| `SNAP` | emit one frame *now* |
| `STREAM <0\|1>` | free-run frames at 10 Hz (for the live `monitor` view) |
| `WIRECHECK` | actively probe whether PA0/PA1 are connected to anything |
| `CAL <micro_ohm>` | set the calibrated shunt resistance |
| `KDIV <milli>` | set the divider ratio ×1000 (`2000` = any matched pair) |
| `VDDA <mv>` | set the assumed ADC reference |

Frames are emitted **on demand**, not free-running. If the meter chattered continuously, the
host could not say exactly when accumulation stopped relative to the measured command exiting,
and would over-attribute up to a frame's worth of idle energy to it. `measure` therefore drives
`ZERO` → run → `SNAP`.

A frame is one JSON line:

```json
{"t":10200,"n":2040576,"ovr":0,"sq":141600000,"sqq":9832000000,"suq":439000000000,
 "su":6330000000,"qmax":410,"fs":200000,"vdda":3300,"r":1000000,"kdiv":2000}
```

### Why it reports raw sums instead of joules

The obvious design — stream samples to the PC and integrate there — silently under-reports
energy the moment the host drops a sample, and nothing in the result tells you it happened.

So the meter accumulates exact integer sums of every conversion at 200 kSps (`sq` = Σq,
`sqq` = Σq², `suq` = Σ(u·q), over `n` samples) and the host only multiplies them by constants.
A missed conversion sets the ADC overrun flag, lands in **`ovr`**, and `nff power` then refuses
to report a joules figure rather than handing back a plausible wrong one.

Reporting sums also means a *re*-calibration re-derives the energy of a past run without
re-measuring it.

Energy falls out of the sums directly. With `L = VDDA/4096` volts per count, `K` the divider
ratio and `R` the shunt:

```
I     = q·L / R
V_esp = u·K·L − q·L                        (rail, minus the drop across the shunt)
E     = Σ V_esp·I·dt = (L²/R)·(1/fs) · Σ(K·u·q − q²)
```

## How it refuses to lie to you

Every guard below exists because the rig produced a *confident, plausible, wrong* number
during bring-up. None of them are hypothetical.

**`ovr` — dropped samples.** A missed ADC conversion means the accumulators are an
under-count. `nff power` reports `ok: false` rather than a low joules figure, because nothing
downstream could distinguish "used less energy" from "measured less energy".

**Window cross-check — a lost `ZERO`.** A `ZERO` that never lands leaves the meter accumulating
from boot, so the frame describes the board's uptime instead of your command. Observed: a 30 s
run reported 91 s and 26 J, with zero overruns and a perfectly sane 58 mA. The host now
verifies the `ZERO` ack, and cross-checks the meter's own elapsed clock against how long it
actually waited. It also checks that the samples delivered match the claimed 200 kSps — if the
ADC isn't running at `fs`, every `dt` and every joule is scaled wrong.

**`WIRECHECK` — a rig wired to nothing.** This is the nastiest one, and it defeated two
designs before this one:

- *Passive plausibility failed.* An unconnected ADC pin does not read zero — it holds charge.
  With nothing attached at all, the meter reported **590 mA** and a supply rail of **4.94 V**.
  Both look entirely reasonable. A range check waves them straight through.
- *An internal pull-up/pull-down probe failed too.* It reported both pins "connected" with
  nothing attached, because the F446's ADC **cannot read a pad while it is in digital-input
  mode** — and the pulls are disabled in analog mode, so there is no way to have both at once.

What works is to **drive the node, release it, and see whether anything pulls it back.** A real
source restores its voltage in nanoseconds (the divider is a few-kΩ source; the shunt node is a
near short to ground). A floating pin keeps the charge: on the bench, a floating PA0 stayed
1746 counts off and a floating PA1 stayed 2425 counts off.

For the divider we require **both** that it reads like a 5 V rail *and* that it springs back.
Either test alone can be passed by luck — the bench's floating PA1 sat at a convincing 4.94 V —
but nothing that is genuinely connected fails the second one.

> **PA0 is only ever driven LOW.** Driving it high would put the GPIO against the 1 Ω shunt
> straight to ground — roughly 65 mA, well past the pin's 25 mA rating. PA1 is safe to drive
> both ways because the divider's series resistance limits the current to well under a mA.

## What this rig can and cannot measure

| | |
|---|---|
| ✅ Active current, and **joules per OTA** | The number this exists for |
| ✅ Idle baseline | So `measure` can report *marginal* energy |
| ❌ **Deep sleep** | 10 µA × 1 Ω = 10 µV; one ADC count is 800 µV. You would read zero. Needs a real front-end (INA228, PPK2) — no firmware trick recovers it |
| ❌ `nff flash` / `nff monitor` | Need USB, which shorts the shunt (rule 1) |
| ⚠️ Whole-board, not the ESP32 die | Includes the LDO's loss, the USB-UART chip and the power LED. Constant, so it cancels out of the baseline-subtracted marginal joules — but don't call the total "the ESP32's energy" |

## Calibration

Accuracy is dominated by the *actual* resistance in the ground path — the resistor's tolerance
plus 10–100 mΩ per breadboard contact, which is not negligible against 1 Ω. Don't try to measure
the resistor with a multimeter; probe leads alone are ~0.2 Ω.

Instead calibrate the whole chain at once, against a known load:

```
nff power calibrate --load 100      # a 100 Ω resistor on 3V3 ≈ 33 mA
```

It reads what the meter thinks, asks what your multimeter reads, and solves for the effective
shunt resistance — absorbing the resistor tolerance, the ADC gain error, the ±2% regulator
tolerance on VDDA, and the contact resistance in a single constant.

**Re-run it after any rewiring.** Re-seating one jumper moves the contact resistance.
