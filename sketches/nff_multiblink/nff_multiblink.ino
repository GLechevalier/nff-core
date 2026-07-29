// nff browser-flasher demo — "any board" blink.
//
// Flashed straight from the dashboard over Web Serial. Because different ESP32 dev boards wire
// their onboard LED to different GPIOs (GPIO 2 on DevKitC/DOIT, GPIO 5 on many LOLIN/Wemos,
// GPIO 22/25 on others, some none), this toggles ALL the common, SAFE LED pins at once — so an
// LED lights on essentially any board. Toggling (not just driving HIGH) also covers active-low
// LEDs, which simply blink in inverted phase.
//
// Excluded on purpose: flash pins 6-11 (driving them crashes the chip), UART pins 1/3 (would kill
// the serial heartbeat below), boot-strapping pins 0/12/15, and input-only pins 34-39.
//
// It also prints a heartbeat at 115200 so the flasher's post-flash serial readout proves the
// board is alive even if it has no visible LED at all.

const int LED_PINS[] = { 2, 4, 5, 13, 14, 16, 17, 18, 19, 21, 22, 23, 25, 26, 27, 32, 33 };
const int LED_COUNT  = sizeof(LED_PINS) / sizeof(LED_PINS[0]);
const int INTERVAL   = 250;   // ms per blink half-cycle

unsigned long n = 0;
bool on = false;

void setup() {
  Serial.begin(115200);
  for (int i = 0; i < LED_COUNT; i++) pinMode(LED_PINS[i], OUTPUT);
  delay(200);
  Serial.println();
  Serial.println("nff live on ESP32 — flashed from your browser via Web Serial");
}

void loop() {
  on = !on;
  for (int i = 0; i < LED_COUNT; i++) digitalWrite(LED_PINS[i], on ? HIGH : LOW);
  Serial.print(on ? "LED ON   heartbeat " : "LED OFF  heartbeat ");
  Serial.println(n++);
  delay(INTERVAL);
}
