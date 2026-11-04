// Silence sketch: stops any running playback.
// The upload itself resets the UNO, so playback stops immediately.
// EN_PIN goes HIGH -> A4988 disabled -> no holding current.

#include <Arduino.h>

const uint8_t EN_PIN = 8;
const uint8_t STEP_PINS[] = {2, 3, 4, 12};
const uint8_t DIR_PINS[]  = {5, 6, 7, 13};

void setup() {
  pinMode(EN_PIN, OUTPUT);
  digitalWrite(EN_PIN, HIGH);

  for (uint8_t i = 0; i < 4; i++) {
    pinMode(STEP_PINS[i], OUTPUT);
    pinMode(DIR_PINS[i],  OUTPUT);
    digitalWrite(STEP_PINS[i], LOW);
    digitalWrite(DIR_PINS[i],  LOW);
  }

  Serial.begin(115200);
  Serial.println(F("=== silence ==="));
}

void loop() {
  // idle
}
