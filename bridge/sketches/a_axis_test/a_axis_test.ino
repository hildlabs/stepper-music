// Test: drive the A axis natively via D12 (STEP) / D13 (DIR).
// According to the AZ-Delivery CNC Shield V3 datasheet, the 4th axis can be
// driven through D12+D13 with custom firmware. All clone jumpers on the A slot
// must be removed, otherwise X/Y/Z STEP overrides the signal.
//
// Plays 440 Hz on A for 2 seconds, then disables the drivers.

#include <Arduino.h>

const uint8_t A_STEP = 12;
const uint8_t A_DIR  = 13;
const uint8_t EN_PIN = 8;

const unsigned long TONE_DURATION_MS = 2000;
const float TONE_HZ = 440.0f;

void setup() {
  Serial.begin(115200);

  pinMode(EN_PIN, OUTPUT);
  pinMode(A_STEP, OUTPUT);
  pinMode(A_DIR,  OUTPUT);

  digitalWrite(A_DIR, LOW);
  digitalWrite(A_STEP, LOW);
  digitalWrite(EN_PIN, LOW);

  Serial.println(F("=== A-native test: 440 Hz for 2 s on D12/D13 ==="));

  const unsigned long halfPeriodMicros = (unsigned long)(500000.0f / TONE_HZ);
  const unsigned long endMillis = millis() + TONE_DURATION_MS;
  unsigned long nextToggle = micros() + halfPeriodMicros;
  bool state = false;

  while ((long)(millis() - endMillis) < 0) {
    unsigned long now = micros();
    if ((long)(now - nextToggle) >= 0) {
      state = !state;
      digitalWrite(A_STEP, state ? HIGH : LOW);
      nextToggle += halfPeriodMicros;
    }
  }

  digitalWrite(A_STEP, LOW);
  digitalWrite(EN_PIN, HIGH);
  Serial.println(F("=== done ==="));
}

void loop() {
}
