/**
 * GeigerFixed_V3.ino
 * Fixes applied vs V2:
 *  1. [CRITICAL] sendToPi() - replaced String object with char[] + dtostrf()
 *     → eliminates heap fragmentation that caused the 24-48h freeze
 *  2. [HIGH]     totalPulseCount reads are now atomic (cli/sei guards)
 *  3. [HIGH]     WDT enabled AFTER setup() to prevent boot-loop on slow LCD init
 *  4. [MEDIUM]   millis() rollover-safe comparison using unsigned subtraction
 *  5. [MEDIUM]   attachInterrupt uses digitalPinToInterrupt() macro
 *  6. [LOW]      Display enum renamed to avoid collision with AVR macros
 *  7. [LOW]      Float literals use 'f' suffix (saves flash, avoids double promos)
 */

#include <EncButton.h>
#include <GyverFilters.h>
#include <LiquidCrystal_I2C.h>
#include <avr/wdt.h>
#include <avr/interrupt.h>

// ── Timing constants ──────────────────────────────────────────────────────────
#define LOG_PERIOD      12000UL   // ms: each measurement sub-window
#define MAX_PERIOD      60000UL   // ms: full CPM integration window
#define MEAS_PERIODS    50        // staggered sub-windows
#define TUBE_COEFF      0.00662f  // CPM → µSv/h for SBM-20

// ── Smart LCD auto-off ────────────────────────────────────────────────────────
#define AUTO_OFF_DELAY   30000UL  // ms after init before LCD turns off
#define WARN_BEFORE_OFF   5000UL  // ms warning countdown before off

// ── Hardware objects ──────────────────────────────────────────────────────────
LiquidCrystal_I2C lcd(0x27, 16, 2);
GKalman sivertFilter(0.05f, 0.01f);
Button btn1(3);

// ── Display state ─────────────────────────────────────────────────────────────
// Renamed from bare {mrh, usv, cpms} to avoid potential macro clashes on AVR
enum DispMode : uint8_t { DISP_MRH, DISP_USV, DISP_CPM } dispState = DISP_MRH;

// ── Staggered interval bookkeeping ────────────────────────────────────────────
struct GeigerInterval {
  unsigned long startPulses;
  unsigned long nextResetTime;
  bool          active;
};

GeigerInterval intervals[MEAS_PERIODS];

// FIX #2: must be volatile; ISR writes, main loop reads
volatile unsigned long totalPulseCount = 0;

// ── Live measurement values ───────────────────────────────────────────────────
float         uSvh        = 0.0f;
float         MRh         = 0.0f;
float         dose        = 0.0f;
long          currentCPM  = 0;
bool          isInitialized = false;
bool          isLcdOn       = true;
bool          isFilterOn    = true;
unsigned long initEndTime   = 0;

const float multiplier = (float)MAX_PERIOD / (float)LOG_PERIOD;
const float timeDelay  = (float)LOG_PERIOD / (float)MEAS_PERIODS;

// ─────────────────────────────────────────────────────────────────────────────
// FIX #2 — Atomic read of volatile 4-byte counter.
// On AVR, multi-byte reads are NOT atomic. An ISR firing between the high
// and low byte reads corrupts the value. Save/restore SREG instead of a
// bare cli/sei to be safe inside nested ISR contexts.
// ─────────────────────────────────────────────────────────────────────────────
static inline unsigned long atomicReadPulses() {
  uint8_t sreg = SREG;
  cli();
  unsigned long v = totalPulseCount;
  SREG = sreg;
  return v;
}

// ─────────────────────────────────────────────────────────────────────────────
// ISR — keep it as lean as possible: one increment, nothing else.
// ─────────────────────────────────────────────────────────────────────────────
void tubePulseHandler() {
  totalPulseCount++;
}

// ─────────────────────────────────────────────────────────────────────────────
// setup()
// FIX #3 — WDT is enabled at the VERY END of setup, not at the start.
// If the LCD/I2C bus hangs during init and WDT is already armed, the board
// enters an infinite reset loop and never boots. Enable it only after all
// blocking init work is done.
// ─────────────────────────────────────────────────────────────────────────────
void setup() {
  Serial.begin(19200);

  lcd.init();
  lcd.backlight();
  lcd.setCursor(0, 0);
  lcd.print("N_AV Geiger 2.5");
  lcd.setCursor(0, 1);
  lcd.print("Initializing...");

  unsigned long now = millis();
  for (int i = 0; i < MEAS_PERIODS; i++) {
    intervals[i].startPulses   = 0;
    intervals[i].nextResetTime = now + (unsigned long)(i * timeDelay);
    intervals[i].active        = false;
  }

  // FIX #5 — use digitalPinToInterrupt() rather than hard-coding '0'
  attachInterrupt(digitalPinToInterrupt(2), tubePulseHandler, FALLING);

  // FIX #3 — arm WDT only now that all slow init is complete
  wdt_enable(WDTO_8S);
}

// ─────────────────────────────────────────────────────────────────────────────
// loop()
// ─────────────────────────────────────────────────────────────────────────────
void loop() {
  wdt_reset();  // kick watchdog every iteration

  unsigned long currentMillis = millis();
  btn1.tick();
  handleInputs();

  for (int i = 0; i < MEAS_PERIODS; i++) {
    // FIX #4 — rollover-safe unsigned subtraction instead of >=.
    // When millis() wraps at ~49.7 days, (currentMillis - nextResetTime)
    // underflows correctly due to unsigned arithmetic so the timer keeps
    // firing. The original `currentMillis >= nextResetTime` stalls for the
    // full ~49-day wrap window after rollover.
    if ((currentMillis - intervals[i].nextResetTime) < 0x80000000UL) {

      if (intervals[i].active) {
        unsigned long snap  = atomicReadPulses();
        unsigned long delta = snap - intervals[i].startPulses;
        CalcAndShow(delta);

        if (!isInitialized && i == (MEAS_PERIODS - 1)) {
          isInitialized = true;
          initEndTime   = currentMillis;
        }
      }

      // FIX #2 — second atomic read to set the new baseline
      intervals[i].startPulses   = atomicReadPulses();
      intervals[i].nextResetTime = currentMillis + LOG_PERIOD;
      intervals[i].active        = true;
    }
  }

  // Smart LCD auto-off
  if (isInitialized && isLcdOn && initEndTime > 0) {
    if ((currentMillis - initEndTime) >= AUTO_OFF_DELAY) {
      toggleDisplay();
    }
  }
}

// ─────────────────────────────────────────────────────────────────────────────
// CalcAndShow — compute radiation metrics and push to display + serial
// ─────────────────────────────────────────────────────────────────────────────
void CalcAndShow(unsigned long counts) {
  currentCPM = (long)((float)counts * multiplier);
  uSvh       = (float)currentCPM * TUBE_COEFF;

  if (isFilterOn) {
    uSvh = sivertFilter.filtered(uSvh);
  }

  MRh   = uSvh * 100.0f;
  dose += uSvh * (timeDelay / 3600000.0f);

  if (isLcdOn) {
    updateDisplay();
  }

  sendToPi(MRh);
}

// ─────────────────────────────────────────────────────────────────────────────
// sendToPi — CRITICAL FIX #1
//
// The original code used:
//   String valStr = String(val, 2);
//
// On ATmega328P (2 KB SRAM), the Arduino String class calls malloc()/free()
// for every instance. This function is called ~4.2× per second (50 intervals
// / 12 s). After ~360,000 calls per day, heap fragmentation exhausts SRAM
// and the MCU locks up — this is the direct cause of the 24-48 h freeze.
//
// Fix: use a stack-allocated char[] + dtostrf(), which has zero heap impact.
// ─────────────────────────────────────────────────────────────────────────────
void sendToPi(float val) {
  char buf[12];
  // dtostrf(value, min_width, decimal_places, buffer)
  dtostrf(val, 6, 2, buf);

  // dtostrf may left-pad with spaces; skip them so the checksum is stable
  char *p = buf;
  while (*p == ' ') p++;

  // Simple additive checksum over the value string
  uint8_t checksum = 0;
  for (char *c = p; *c != '\0'; c++) {
    checksum += (uint8_t)(*c);
  }
  checksum %= 256;

  Serial.print(p);
  Serial.print('*');
  Serial.println(checksum);
}

// ─────────────────────────────────────────────────────────────────────────────
// handleInputs — button gestures
// ─────────────────────────────────────────────────────────────────────────────
void handleInputs() {
  if (btn1.hasClicks(1)) {
    if      (dispState == DISP_MRH) dispState = DISP_USV;
    else if (dispState == DISP_USV) dispState = DISP_CPM;
    else                             dispState = DISP_MRH;
    resetLcdTimer();
  }
  if (btn1.holdFor(3000)) {
    dose = 0.0f;
    resetLcdTimer();
  }
  if (btn1.hasClicks(2)) {
    isFilterOn = !isFilterOn;
    resetLcdTimer();
  }
  if (btn1.hasClicks(3)) {
    toggleDisplay();
  }
}

// ─────────────────────────────────────────────────────────────────────────────
// updateDisplay — write current values to the I2C LCD
// ─────────────────────────────────────────────────────────────────────────────
void updateDisplay() {
  unsigned long now = millis();

  // Row 0: measurement value
  lcd.setCursor(0, 0);
  switch (dispState) {
    case DISP_MRH:
      lcd.print("R: "); lcd.print(MRh,  2); lcd.print(" uRh/h  ");
      break;
    case DISP_USV:
      lcd.print("R: "); lcd.print(uSvh, 3); lcd.print(" uSv/h  ");
      break;
    case DISP_CPM:
      lcd.print("CPM: "); lcd.print(currentCPM); lcd.print("        ");
      break;
  }

  // Row 1: accumulated dose OR 5-second shutdown warning
  lcd.setCursor(0, 1);
  if (isInitialized && initEndTime > 0 &&
      (now - initEndTime) >= (AUTO_OFF_DELAY - WARN_BEFORE_OFF)) {
    lcd.print("Shutting off... ");
  } else {
    lcd.print("D: "); lcd.print(dose, 4); lcd.print(" uSv    ");
  }
}

// ─────────────────────────────────────────────────────────────────────────────
// resetLcdTimer — keep LCD alive on any user interaction
// ─────────────────────────────────────────────────────────────────────────────
void resetLcdTimer() {
  if (!isLcdOn) {
    toggleDisplay();
  } else if (isInitialized) {
    initEndTime = millis();
  }
}

// ─────────────────────────────────────────────────────────────────────────────
// toggleDisplay — backlight on/off
// ─────────────────────────────────────────────────────────────────────────────
void toggleDisplay() {
  isLcdOn = !isLcdOn;
  if (isLcdOn) {
    lcd.backlight();
    lcd.clear();
    initEndTime = millis();
    updateDisplay();
  } else {
    lcd.noBacklight();
    lcd.clear();
    initEndTime = 0;
  }
}
