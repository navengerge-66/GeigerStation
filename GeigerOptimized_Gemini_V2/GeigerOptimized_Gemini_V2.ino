#include <EncButton.h>
#include <GyverFilters.h>
#include <LiquidCrystal_I2C.h>
#include <avr/wdt.h> // Surgery: Watchdog library

#define LOG_PERIOD 12000 
#define MAX_PERIOD 60000 
#define MEAS_PERIODS 50  
#define TUBE_COEFF 0.00662 

// Surgery: Smart LCD Settings
#define AUTO_OFF_DELAY 30000
#define WARN_BEFORE_OFF 5000

LiquidCrystal_I2C lcd(0x27, 16, 2);
GKalman sivertFilter(0.05, 0.01);
Button btn1(3);

enum { mrh, usv, cpms } dispState = mrh;

struct geigerIntervalData {
  unsigned long startPulses;   
  unsigned long nextResetTime; 
  bool active;
};

geigerIntervalData intervals[MEAS_PERIODS];
volatile unsigned long totalPulseCount = 0; 

float uSvh, MRh, dose;
long currentCPM;
bool isInitialized = false;
bool isLcdOn = true;
bool isFilterOn = true;
const float multiplier = (float)MAX_PERIOD / (float)LOG_PERIOD; 
const float timeDelay = (float)LOG_PERIOD / (float)MEAS_PERIODS; 

// Surgery: Smart LCD Timing variables
unsigned long initEndTime = 0;

void setup() {
  Serial.begin(19200);
  lcd.init();
  lcd.backlight();
  lcd.print("N_AV Geiger 2.5");
  lcd.setCursor(0, 1);
  lcd.print("Initializing...");
  
  unsigned long now = millis();
  for (int i = 0; i < MEAS_PERIODS; i++) {
    intervals[i].startPulses = 0;
    intervals[i].nextResetTime = now + (i * timeDelay);
    intervals[i].active = false;
  }

  attachInterrupt(0, tubePulseHandler, FALLING);

  // Surgery: Initialize Hardware Watchdog (8 seconds)
  wdt_enable(WDTO_8S);
}

void tubePulseHandler() {
  totalPulseCount++;
}

void loop() {
  // Surgery: Kick the Dog
  wdt_reset();

  unsigned long currentMillis = millis();
  btn1.tick();
  handleInputs();

  // Engine: Your exact staggered interval logic
  for (int i = 0; i < MEAS_PERIODS; i++) {
    if (currentMillis >= intervals[i].nextResetTime) {
      
      if (intervals[i].active) {
        unsigned long pulsesInThisWindow = totalPulseCount - intervals[i].startPulses;
        CalcAndShow(pulsesInThisWindow);

        // Surgery: Detect when initialization finishes (all 50 windows active)
        if (!isInitialized && i == MEAS_PERIODS - 1) {
          isInitialized = true;
          initEndTime = currentMillis; // Start 30s countdown
        }
      }

      intervals[i].startPulses = totalPulseCount;
      intervals[i].nextResetTime = currentMillis + LOG_PERIOD;
      intervals[i].active = true;
    }
  }

  // Surgery: Smart Auto-Off check
  if (isInitialized && isLcdOn && initEndTime > 0) {
    if (currentMillis - initEndTime >= AUTO_OFF_DELAY) {
      toggleDisplay(); 
    }
  }
}

void CalcAndShow(unsigned long counts) {
  currentCPM = counts * multiplier;
  uSvh = (float)currentCPM * TUBE_COEFF;

  if (isFilterOn) {
    uSvh = sivertFilter.filtered(uSvh);
  }

  MRh = uSvh * 100.0;
  dose += uSvh * (timeDelay / 3600000.0);

  if (isLcdOn) {
    updateDisplay();
  }

  sendToPi(MRh);
}

void sendToPi(float val) {
  String valStr = String(val, 2);
  int checksum = 0;
  for (int i = 0; i < valStr.length(); i++) {
    checksum += (int)valStr[i];
  }
  checksum = checksum % 256;

  Serial.print(valStr);
  Serial.print("*");
  Serial.println(checksum);
}

void handleInputs() {
  if (btn1.hasClicks(1)) {
    if (dispState == mrh) dispState = usv;
    else if (dispState == usv) dispState = cpms;
    else dispState = mrh;
    resetLcdTimer();
  }
  if (btn1.holdFor(3000)) {
    dose = 0;
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

// Surgery: Overhauled Display logic for 5s warning
void updateDisplay() {
  unsigned long now = millis();
  
  // Line 1: Values
  lcd.setCursor(0, 0);
  switch (dispState) {
    case mrh:
      lcd.print("R: "); lcd.print(MRh, 2); lcd.print(" uRh/h    ");
      break;
    case usv:
      lcd.print("R: "); lcd.print(uSvh, 3); lcd.print(" uSv/h    ");
      break;
    case cpms:
      lcd.print("CPM: "); lcd.print(currentCPM); lcd.print("         ");
      break;
  }

  // Line 2: Dose OR Shutdown Warning
  lcd.setCursor(0, 1);
  if (isInitialized && initEndTime > 0 && (now - initEndTime >= (AUTO_OFF_DELAY - WARN_BEFORE_OFF))) {
    lcd.print("Shutting off... ");
  } else {
    lcd.print("D: "); lcd.print(dose, 4); lcd.print(" uSv     ");
  }
}

// Surgery: Helper to keep LCD alive on interaction
void resetLcdTimer() {
  if (!isLcdOn) toggleDisplay();
  else if (isInitialized) initEndTime = millis();
}

void toggleDisplay() {
  isLcdOn = !isLcdOn;
  if (isLcdOn) {
    lcd.backlight();
    lcd.clear(); // Clear "Initializing" text
    initEndTime = millis(); // Reset timer on wake
    updateDisplay();
  } else {
    lcd.noBacklight();
    lcd.clear(); 
    initEndTime = 0; // Stop timer while off
  }
}