#include <Arduino.h>

// esp32dev has no LED_BUILTIN macro in this core; use a concrete GPIO.
static const int LED_PIN = 2;

void setup() {
  pinMode(LED_PIN, OUTPUT);
  Serial.begin(115200);
}

void loop() {
  digitalWrite(LED_PIN, HIGH);
  delay(500);
  digitalWrite(LED_PIN, LOW);
  delay(500);
  Serial.println("blink");
}
