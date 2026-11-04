// Motor test: spins X, Y, Z, A one after another for ~500 ms each.
// A uses the native 4th axis path D12/D13 (AZ-Delivery datasheet).
// Runs once, then EN HIGH (drivers off, no vibration).
//
// Pin mapping (CNC Shield V3 / UNO):
//   X STEP=2  DIR=5
//   Y STEP=3  DIR=6
//   Z STEP=4  DIR=7
//   A STEP=12 DIR=13

#include <Arduino.h>

const uint8_t EN_PIN = 8;
const uint8_t STEP_PINS[4] = {2, 3, 4, 12};
const uint8_t DIR_PINS[4]  = {5, 6, 7, 13};
const char    LABELS[4]    = {'X', 'Y', 'Z', 'A'};

const float    TEST_HZ    = 200.0f;   // 200 Hz = 1 rev/s at 200 steps/rev -> half a turn in 500 ms
const unsigned long ROTATE_MS = 500;
const unsigned long GAP_MS    = 150;

static void rotate(uint8_t stepPin) {
  const unsigned long halfPeriodMicros = (unsigned long)(500000.0f / TEST_HZ);
  const unsigned long endMillis = millis() + ROTATE_MS;
  unsigned long nextToggle = micros() + halfPeriodMicros;
  bool state = false;

  while ((long)(millis() - endMillis) < 0) {
    unsigned long now = micros();
    if ((long)(now - nextToggle) >= 0) {
      state = !state;
      digitalWrite(stepPin, state ? HIGH : LOW);
      nextToggle += halfPeriodMicros;
    }
  }
  digitalWrite(stepPin, LOW);
}

void setup() {
  Serial.begin(115200);

  pinMode(EN_PIN, OUTPUT);
  digitalWrite(EN_PIN, LOW);

  for (uint8_t i = 0; i < 4; i++) {
    pinMode(STEP_PINS[i], OUTPUT);
    pinMode(DIR_PINS[i],  OUTPUT);
    digitalWrite(STEP_PINS[i], LOW);
    digitalWrite(DIR_PINS[i],  LOW);
  }

  Serial.println(F("=== motor test: X -> Y -> Z -> A ==="));
  for (uint8_t i = 0; i < 4; i++) {
    Serial.print(F("spin "));
    Serial.println(LABELS[i]);
    rotate(STEP_PINS[i]);
    delay(GAP_MS);
  }

  digitalWrite(EN_PIN, HIGH);
  Serial.println(F("=== motor test done ==="));
}

void loop() {
}
