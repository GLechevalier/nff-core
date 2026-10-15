/*
 * nff-power-meter — ESP32 load test
 *
 * A deliberately power-hungry staircase, so the meter has something unambiguous to see.
 *
 * WHY THIS EXISTS
 *
 * An idle ESP32 draws a steady ~57 mA. A *floating* ADC pin on the meter also reads a steady
 * ~57 mA — it holds charge and lands wherever it lands. During bring-up those two were literally
 * indistinguishable, and a rig wired to nothing produced a confident, plausible current.
 *
 * A flat load cannot tell them apart. A load that STEPS can: this firmware walks four phases
 * with very different draws, so the meter either follows the staircase (the shunt is wired) or
 * sits flat (it is not). No ambiguity, no judgement call.
 *
 * Nothing here needs WiFi credentials — the radio load is a scan, not an association.
 *
 * USAGE
 *   1. pio run -t upload            (over the ESP32's own USB)
 *   2. UNPLUG the ESP32's USB       <-- required; PC USB ties its GND to the meter's and
 *                                       shorts the shunt out. See the rig README, rule 1.
 *   3. Power it from the Nucleo's 5V pin.
 *   4. nff power selftest
 *
 * PHASE MARKERS (optional, for per-phase energy later)
 *   GPIO26/GPIO27 carry a 2-bit code of the current phase. Wire each through 1k to a
 *   5V-TOLERANT Nucleo pin (PA10/D2, PA9/D8 — NOT PA4, which is a DAC pin and not FT), with a
 *   10k pulldown returning to the ESP32's OWN ground (E_GND, above the shunt) — never to the
 *   Nucleo's ground, or that pulldown current bypasses the shunt and is invisible to the meter.
 *   Leave them unwired and everything below still works.
 */

#include <Arduino.h>
#include <WiFi.h>

#define MARKER_BIT0 26
#define MARKER_BIT1 27
#define LED_PIN 2  // onboard LED on most WROOM devkits — a liveness sign with USB unplugged

#define PHASE_MS 5000u

enum Phase {
  PHASE_IDLE = 0,   // radio off, CPU parked      -> lowest
  PHASE_CPU = 1,    // radio off, CPU flat out    -> mid
  PHASE_RADIO = 2,  // radio on, associating not required -> higher
  PHASE_SCAN = 3,   // radio actively transmitting -> highest, with peaks
};

static void mark(Phase p) {
  digitalWrite(MARKER_BIT0, (p & 1) ? HIGH : LOW);
  digitalWrite(MARKER_BIT1, (p & 2) ? HIGH : LOW);
}

/* Park the CPU without sleeping the whole chip — we want a *low* floor, not a dead board. */
static void phase_idle(void) {
  WiFi.mode(WIFI_OFF);
  setCpuFrequencyMhz(80);
  const uint32_t until = millis() + PHASE_MS;
  while (millis() < until) {
    delay(50);  // yields to the idle task, which drops the core into its low-power wait
  }
}

/* Flat-out FPU work at full clock. volatile so the optimiser cannot delete the loop. */
static void phase_cpu(void) {
  WiFi.mode(WIFI_OFF);
  setCpuFrequencyMhz(240);
  volatile float acc = 1.0f;
  const uint32_t until = millis() + PHASE_MS;
  while (millis() < until) {
    for (int i = 0; i < 20000; i++) acc = acc * 1.000001f + 0.000001f;
  }
}

/* Radio powered up, but deliberately NOT associated — no credentials needed. */
static void phase_radio(void) {
  setCpuFrequencyMhz(240);
  WiFi.mode(WIFI_STA);
  WiFi.disconnect(false, false);
  const uint32_t until = millis() + PHASE_MS;
  while (millis() < until) delay(20);
}

/* An active scan is a real TX load — probe requests on every channel — and needs no network. */
static void phase_scan(void) {
  setCpuFrequencyMhz(240);
  WiFi.mode(WIFI_STA);
  const uint32_t until = millis() + PHASE_MS;
  while (millis() < until) {
    WiFi.scanNetworks(/*async=*/false, /*show_hidden=*/true);
    WiFi.scanDelete();
  }
}

void setup() {
  pinMode(MARKER_BIT0, OUTPUT);
  pinMode(MARKER_BIT1, OUTPUT);
  pinMode(LED_PIN, OUTPUT);
  mark(PHASE_IDLE);
}

void loop() {
  static const Phase order[] = {PHASE_IDLE, PHASE_CPU, PHASE_RADIO, PHASE_SCAN};
  for (Phase p : order) {
    mark(p);
    digitalWrite(LED_PIN, (p == PHASE_SCAN) ? HIGH : LOW);
    switch (p) {
      case PHASE_IDLE:  phase_idle();  break;
      case PHASE_CPU:   phase_cpu();   break;
      case PHASE_RADIO: phase_radio(); break;
      case PHASE_SCAN:  phase_scan();  break;
    }
  }
}
