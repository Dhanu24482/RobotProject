/*============================================================================================
  OmniServ Robot - Full Control
  Hardware : Arduino Mega 2560
  Motors   : BTS7960 Dual Driver (ROS2 Nav2 + Bluetooth App)
  Servos   : Head Pan, Eyes, Eyelid + 6-DOF humanoid arms
  Comms    : Serial  (USB) → Raspberry Pi / ROS2
             Serial3 (BT)  → HC-05 Bluetooth App

  Motor Command  (from Pi):  <left_pwm,right_pwm>     e.g. <100,80>
  Servo Commands (from Pi):  <HP:90>  <EL:80>  <EY:10,-5>  <NOD>  <SALUTE>  etc.
  Arm poses                :  <HOME> <HAND_UP> <HAND_DOWN> <PULL_UP> <PULL_DOWN>
                              <SALUTE> <GOODBYE>
  Bluetooth      (from App): W F A D S 0-9 k B b M m  (unchanged)

  Arm pins 38-43 are free of motor/sensor conflicts. If you validated poses on a
  standalone Mega with pins {2,8,4,5,6,7}, rewire arms here before flashing OmniServ
  (those pins are used by head pan + BTS7960).
============================================================================================*/

#include <Servo.h>

// ─── SIGNAL & LIGHT PINS ───────────────────────────────────────────────────
#define light_SR  A0
#define light_SL  A1
#define light_BR  A2
#define light_HL  A3
#define horn_Buzz A4

// ─── BTS7960 DRIVER #1 (Right Side) ───────────────────────────────────────
#define R_EN_1  8
#define L_EN_1  9
#define RPWM_1  5
#define LPWM_1  6

// ─── BTS7960 DRIVER #2 (Left Side) ────────────────────────────────────────
#define R_EN_2  4

#define L_EN_2  7
#define RPWM_2  10
#define LPWM_2  11

// ─── SERVO PINS ───────────────────────────────────────────────────────────
#define PIN_HEAD_PAN   2    // Single head servo — left/right only
#define PIN_EYE_LEFT   3
#define PIN_EYE_RIGHT  12
#define PIN_EYE_LID    13

// 6-DOF arms (robot angles; reverse flags applied in getServoAngle)
// Order: L shoulder pitch, L shoulder roll, L elbow,
//        R shoulder pitch, R shoulder roll, R elbow
const byte ARM_PINS[6] = {38, 39, 40, 41, 42, 43};

// ─── SENSOR PINS (sensor upgrade — telemetry only, never stops the bot) ─────
// 6x HC-SR04 ultrasonic: 3 front (~120 deg spread) + 3 rear (for reversing).
// Order: front-left, front-center, front-right, rear-left, rear-center, rear-right.
#define NUM_US 6
const uint8_t US_TRIG[NUM_US] = { 22, 24, 26, 28, 30, 32 };
const uint8_t US_ECHO[NUM_US] = { 23, 25, 27, 29, 31, 33 };

// 4x digital IR (down-facing pit/cliff): front, back, left, right.
#define NUM_IR 4
const uint8_t IR_PIN[NUM_IR] = { 34, 35, 36, 37 };
// Set true if your IR modules read HIGH when there is NO floor (a pit/drop).
// Set false if they read LOW on a pit. Flip this to match your hardware.
const bool IR_PIT_WHEN_HIGH = true;

// HC-SR04 timing: 25 ms timeout ~= 4.3 m max range (echo speed of sound).
const unsigned long US_TIMEOUT_US = 25000UL;

// Telemetry cadence
const unsigned long TELEMETRY_PERIOD_MS = 100;  // ~10 Hz

// ─── SERVO OBJECTS ────────────────────────────────────────────────────────
Servo headPan;
Servo eyeLeft, eyeRight, eyeLid;
Servo armServos[6];

// ─── SERVO DEFAULT POSITIONS ──────────────────────────────────────────────
const int HP_CENTER   = 90;   // Head center
const int HP_MIN      = 30;   // Head full left
const int HP_MAX      = 150;  // Head full right

const int EYE_CENTER  = 90;
const int EYE_MIN     = 50;
const int EYE_MAX     = 130;

const int EYE_OPEN    = 30;   // Eyelid open position
const int EYE_CLOSED  = 90;   // Eyelid closed position

// Calibrated robot-space arm angles (HOME)
int currentArmAngle[6] = {0, 90, 90, 0, 90, 90};

// Mirror left-side mechanical mounting
bool reverseArmServo[6] = {true, true, true, false, false, false};

const int ARM_SPEED_DELAY_MS = 40;   // ms per degree step (smooth parallel move)
const int GOODBYE_HOLD_MS    = 2500; // hold salute before returning home

// Named poses (robot angles)
int POSE_HOME[6]      = {0, 90, 90, 0, 90, 90};
int POSE_HAND_UP[6]   = {180, 90, 90, 180, 90, 90};
int POSE_HAND_DOWN[6] = {0, 90, 90, 0, 90, 90};
int POSE_PULL_UP[6]   = {0, 0, 180, 0, 0, 180};
int POSE_PULL_DOWN[6] = {0, 180, 0, 0, 180, 0};
int POSE_SALUTE[6]    = {0, 90, 90, 180, 60, 150};  // right hand salute

// ─── BLUETOOTH STATE ──────────────────────────────────────────────────────
int  command;
int  speedCar    = 30;
bool lightFront  = false;
bool horn        = false;

// ─── ROS2 WATCHDOG ────────────────────────────────────────────────────────
unsigned long lastRosCommandTime = 0;
bool usingROS = false;

// ─── SENSOR STATE ───────────────────────────────────────────────────────────
int  usDistCm[NUM_US] = { 0, 0, 0, 0, 0, 0 };  // 0 = out of range / no echo
bool irPit[NUM_IR]    = { false, false, false, false };
uint8_t usIndex = 0;                            // round-robin: one US per loop
unsigned long lastTelemetryMs = 0;


// ═══════════════════════════════════════════════════════════════════════════
//  SETUP
// ═══════════════════════════════════════════════════════════════════════════
void setup() {
  // Light & horn pins
  pinMode(light_SR, OUTPUT);
  pinMode(light_SL, OUTPUT);
  pinMode(light_BR, OUTPUT);
  pinMode(light_HL, OUTPUT);
  pinMode(horn_Buzz, OUTPUT);

  // BTS7960 pins
  pinMode(R_EN_1, OUTPUT); pinMode(L_EN_1, OUTPUT);
  pinMode(RPWM_1, OUTPUT); pinMode(LPWM_1, OUTPUT);
  pinMode(R_EN_2, OUTPUT); pinMode(L_EN_2, OUTPUT);
  pinMode(RPWM_2, OUTPUT); pinMode(LPWM_2, OUTPUT);

  // Enable both BTS7960 drivers
  digitalWrite(R_EN_1, HIGH); digitalWrite(L_EN_1, HIGH);
  digitalWrite(R_EN_2, HIGH); digitalWrite(L_EN_2, HIGH);

  // Sensor pins
  for (uint8_t i = 0; i < NUM_US; i++) {
    pinMode(US_TRIG[i], OUTPUT);
    digitalWrite(US_TRIG[i], LOW);
    pinMode(US_ECHO[i], INPUT);
  }
  for (uint8_t i = 0; i < NUM_IR; i++) {
    pinMode(IR_PIN[i], INPUT);
  }

  // Attach servos
  headPan.attach(PIN_HEAD_PAN);
  eyeLeft.attach(PIN_EYE_LEFT);
  eyeRight.attach(PIN_EYE_RIGHT);
  eyeLid.attach(PIN_EYE_LID);
  for (int i = 0; i < 6; i++) {
    armServos[i].attach(ARM_PINS[i]);
    armServos[i].write(getArmServoAngle(i, currentArmAngle[i]));
  }

  // Servo startup positions (head/eyes + arm HOME already written above)
  centerAll();

  // Serial ports
  Serial.begin(115200);    // USB → Raspberry Pi / ROS2
  Serial3.begin(9600);     // BT  → HC-05 App

  Serial.println("OmniServ READY");
}


// ═══════════════════════════════════════════════════════════════════════════
//  MAIN LOOP
// ═══════════════════════════════════════════════════════════════════════════
void loop() {

  // ── 1. RASPBERRY PI / ROS2 (USB Serial) ──────────────────────────────
  if (Serial.available() > 0) {
    String input = Serial.readStringUntil('\n');
    input.trim();
    parseSerialCommand(input);
  }

  // ── 2. BLUETOOTH APP (HC-05 on Serial3) ──────────────────────────────
  if (Serial3.available() > 0) {
    command = Serial3.read();
    usingROS = false;  // BT takes control — disable watchdog

    switch (command) {
      case 'W': goAhead();   break;
      case 'F': goBack();    break;
      case 'A': goLeft();    break;
      case 'D': goRight();   break;
      case 'S': stopRobot(); break;

      // Speed levels
      case '0': speedCar = 30;  break;
      case '1': speedCar = 115; break;
      case '2': speedCar = 130; break;
      case '3': speedCar = 145; break;
      case '4': speedCar = 160; break;
      case '5': speedCar = 175; break;
      case '6': speedCar = 190; break;
      case '7': speedCar = 205; break;
      case '8': speedCar = 220; break;
      case '9': speedCar = 235; break;
      case 'k': speedCar = 255; break;

      // Lights & horn
      case 'B': lightFront = true;  break;
      case 'b': lightFront = false; break;
      case 'M': horn = true;        break;
      case 'm': horn = false;       break;

      // Bluetooth body shortcuts
      case 'N': doNod();         break;  // Nod head
      case 'n': doShake();       break;  // Shake head
      case 'V': doWave(true);    break;  // Wave left
      case 'v': doWave(false);   break;  // Wave right
      case 'K': doBlink();       break;  // Blink eyes
      case 'C': centerAll();     break;  // Center everything

      default: stopRobot(); break;
    }

    digitalWrite(light_HL, lightFront ? HIGH : LOW);
    digitalWrite(horn_Buzz, horn      ? HIGH : LOW);
  }

  // ── 3. SAFETY WATCHDOG (deadman switch) ──────────────────────────────
  // If ROS was driving but 1 second passes with no command → stop!
  if (usingROS && (millis() - lastRosCommandTime > 1000)) {
    stopRobot();
    usingROS = false;
  }

  // ── 4. SENSORS (telemetry only — never changes motor behavior) ───────
  // Round-robin: measure exactly ONE ultrasonic per loop so the blocking
  // pulseIn (up to ~25 ms) cannot starve motor/BT handling or the watchdog.
  usDistCm[usIndex] = readUltrasonicCm(usIndex);
  usIndex = (usIndex + 1) % NUM_US;

  // IR reads are instantaneous — sample all four every loop.
  for (uint8_t i = 0; i < NUM_IR; i++) {
    bool high = (digitalRead(IR_PIN[i]) == HIGH);
    irPit[i] = IR_PIT_WHEN_HIGH ? high : !high;
  }

  // Emit a telemetry line to the Pi at ~10 Hz.
  if (millis() - lastTelemetryMs >= TELEMETRY_PERIOD_MS) {
    lastTelemetryMs = millis();
    sendTelemetry();
  }
}


// ═══════════════════════════════════════════════════════════════════════════
//  SERIAL COMMAND PARSER
//  Handles both motor commands and servo commands from Pi
//
//  Format: <COMMAND:VALUE>  or  <left_pwm,right_pwm>
// ═══════════════════════════════════════════════════════════════════════════
void parseSerialCommand(String cmd) {
  if (cmd.length() < 3) return;

  // ── MOTOR COMMAND: <left,right>  e.g. <100,80> ──
  int left_pwm = 0, right_pwm = 0;
  if (sscanf(cmd.c_str(), "<%d,%d>", &left_pwm, &right_pwm) == 2) {
    driveMotors(left_pwm, right_pwm);
    lastRosCommandTime = millis();
    usingROS = true;
    return;
  }

  // ── SERVO / ANIMATION COMMANDS ──
  // Extract content between < and >
  if (!cmd.startsWith("<") || !cmd.endsWith(">")) return;
  String inner = cmd.substring(1, cmd.length() - 1);  // strip < >

  // ── CENTER ALL: <CENTER> ──
  if (inner == "CENTER") { centerAll(); return; }

  // ── NOD HEAD: <NOD> ──
  if (inner == "NOD") { doNod(); return; }

  // ── SHAKE HEAD: <SHAKE> ──
  if (inner == "SHAKE") { doShake(); return; }

  // ── BLINK EYES: <EBLINK> ──
  if (inner == "EBLINK") { doBlink(); return; }

  // ── DOUBLE BLINK: <EBLINK2> ──
  if (inner == "EBLINK2") { doDoubleBlink(); return; }

  // ── WAVE LEFT: <WAVE:L> ──
  if (inner == "WAVE:L") { doWave(true); return; }

  // ── WAVE RIGHT: <WAVE:R> ──
  if (inner == "WAVE:R") { doWave(false); return; }

  // ── 6-DOF ARM POSES ──
  if (inner == "HOME" || inner == "ARM_HOME") { moveArmPose(POSE_HOME); return; }
  if (inner == "HAND_UP")   { moveArmPose(POSE_HAND_UP); return; }
  if (inner == "HAND_DOWN") { moveArmPose(POSE_HAND_DOWN); return; }
  if (inner == "PULL_UP")   { moveArmPose(POSE_PULL_UP); return; }
  if (inner == "PULL_DOWN") { moveArmPose(POSE_PULL_DOWN); return; }
  if (inner == "SALUTE")    { moveArmPose(POSE_SALUTE); return; }
  if (inner == "GOODBYE")   { doGoodbye(); return; }

  // ── LOOK: <LOOK:L> <LOOK:R> <LOOK:C> ──
  if (inner == "LOOK:L") { lookLeft();  return; }
  if (inner == "LOOK:R") { lookRight(); return; }
  if (inner == "LOOK:C") { headPan.write(HP_CENTER);
                           eyeLeft.write(EYE_CENTER);
                           eyeRight.write(EYE_CENTER); return; }

  // ── HEAD PAN: <HP:90> ──
  if (inner.startsWith("HP:")) {
    int v = inner.substring(3).toInt();
    headPan.write(constrain(v, HP_MIN, HP_MAX));
    return;
  }

  // ── EYE LEFT: <EL:80> ──
  if (inner.startsWith("EL:")) {
    int v = inner.substring(3).toInt();
    eyeLeft.write(constrain(v, EYE_MIN, EYE_MAX));
    return;
  }

  // ── EYE RIGHT: <ER:100> ──
  if (inner.startsWith("ER:")) {
    int v = inner.substring(3).toInt();
    eyeRight.write(constrain(v, EYE_MIN, EYE_MAX));
    return;
  }

  // ── BOTH EYES: <EY:x,y>  x = left/right offset, y = up/down offset ──
  // Since head has no tilt servo, only x (left/right) is used for eyes
  if (inner.startsWith("EY:")) {
    String vals = inner.substring(3);
    int comma = vals.indexOf(',');
    if (comma != -1) {
      int ex = vals.substring(0, comma).toInt();  // left/right offset -40 to +40
      // Map offset to servo angle: center=90, offset adds/subtracts
      eyeLeft.write(constrain(90 + ex, EYE_MIN, EYE_MAX));
      eyeRight.write(constrain(90 + ex, EYE_MIN, EYE_MAX));
    }
    return;
  }

  // ── HAND LEFT (compat): <HL:90> → left shoulder pitch ──
  if (inner.startsWith("HL:")) {
    int v = constrain(inner.substring(3).toInt(), 0, 180);
    int pose[6];
    copyArmPose(currentArmAngle, pose);
    pose[0] = v;
    moveArmPose(pose);
    return;
  }

  // ── HAND RIGHT (compat): <HR:90> → right shoulder pitch ──
  if (inner.startsWith("HR:")) {
    int v = constrain(inner.substring(3).toInt(), 0, 180);
    int pose[6];
    copyArmPose(currentArmAngle, pose);
    pose[3] = v;
    moveArmPose(pose);
    return;
  }

  // ── BOTH HANDS (compat): <HANDS:left,right> → both shoulder pitches ──
  if (inner.startsWith("HANDS:")) {
    String vals = inner.substring(6);
    int comma = vals.indexOf(',');
    if (comma != -1) {
      int l = constrain(vals.substring(0, comma).toInt(), 0, 180);
      int r = constrain(vals.substring(comma + 1).toInt(), 0, 180);
      int pose[6];
      copyArmPose(currentArmAngle, pose);
      pose[0] = l;
      pose[3] = r;
      moveArmPose(pose);
    }
    return;
  }
}


// ═══════════════════════════════════════════════════════════════════════════
//  MOTOR FUNCTIONS (BTS7960)
// ═══════════════════════════════════════════════════════════════════════════
void driveMotors(int left_pwm, int right_pwm) {
  // Left motor (Driver 2)
  if (left_pwm >= 0) {
    analogWrite(RPWM_2, left_pwm);
    analogWrite(LPWM_2, 0);
  } else {
    analogWrite(RPWM_2, 0);
    analogWrite(LPWM_2, abs(left_pwm));
  }
  // Right motor (Driver 1)
  if (right_pwm >= 0) {
    analogWrite(RPWM_1, right_pwm);
    analogWrite(LPWM_1, 0);
  } else {
    analogWrite(RPWM_1, 0);
    analogWrite(LPWM_1, abs(right_pwm));
  }
}

void stopRobot() { driveMotors(0, 0); digitalWrite(light_BR, HIGH); }
void goAhead()   { driveMotors( speedCar,  speedCar); digitalWrite(light_BR, LOW); }
void goBack()    { driveMotors(-speedCar, -speedCar); digitalWrite(light_BR, LOW); }
void goRight()   { driveMotors( speedCar, -speedCar); }
void goLeft()    { driveMotors(-speedCar,  speedCar); }


// ═══════════════════════════════════════════════════════════════════════════
//  SERVO HELPER — smooth write (avoids servo jerk)
// ═══════════════════════════════════════════════════════════════════════════
void smoothWrite(Servo& s, int target, int stepDelay = 8) {
  int current = s.read();
  if (current == target) return;
  int step = (target > current) ? 1 : -1;
  while (current != target) {
    current += step;
    s.write(current);
    delay(stepDelay);
  }
}


// ═══════════════════════════════════════════════════════════════════════════
//  6-DOF ARM CONTROL (synchronized poses)
// ═══════════════════════════════════════════════════════════════════════════
int getArmServoAngle(int index, int angle) {
  if (reverseArmServo[index]) return 180 - angle;
  return angle;
}

void copyArmPose(const int src[6], int dst[6]) {
  for (int i = 0; i < 6; i++) dst[i] = src[i];
}

void moveArmPose(int targetPose[6]) {
  bool moving = true;
  while (moving) {
    moving = false;
    for (int i = 0; i < 6; i++) {
      if (currentArmAngle[i] != targetPose[i]) {
        moving = true;
        if (currentArmAngle[i] < targetPose[i]) currentArmAngle[i]++;
        else currentArmAngle[i]--;
        armServos[i].write(getArmServoAngle(i, currentArmAngle[i]));
      }
    }
    delay(ARM_SPEED_DELAY_MS);
  }
}

void doGoodbye() {
  moveArmPose(POSE_SALUTE);
  delay(GOODBYE_HOLD_MS);
  moveArmPose(POSE_HOME);
}


// ═══════════════════════════════════════════════════════════════════════════
//  SERVO ANIMATIONS
// ═══════════════════════════════════════════════════════════════════════════

// Center all servos to default positions
void centerAll() {
  headPan.write(HP_CENTER);
  eyeLeft.write(EYE_CENTER);
  eyeRight.write(EYE_CENTER);
  eyeLid.write(EYE_OPEN);
  moveArmPose(POSE_HOME);
}

// Nod: head turns left → right → left → center (since no tilt, we simulate with pan)
// Actually for "yes" nod we use the available pan servo in a subtle way
// Better: blink eyes and wave hands slightly for "yes" expression
void doNod() {
  // Subtle left-right pan to simulate nodding acknowledgement
  for (int i = 0; i < 2; i++) {
    smoothWrite(headPan, HP_CENTER - 15, 10);
    delay(150);
    smoothWrite(headPan, HP_CENTER + 15, 10);
    delay(150);
  }
  smoothWrite(headPan, HP_CENTER, 10);
  // Also blink once as affirmation
  doBlink();
}

// Shake: head turns left → right → left → center (no = disagreement)
void doShake() {
  for (int i = 0; i < 3; i++) {
    smoothWrite(headPan, HP_MIN + 20, 8);
    delay(150);
    smoothWrite(headPan, HP_MAX - 20, 8);
    delay(150);
  }
  smoothWrite(headPan, HP_CENTER, 8);
}

// Blink eyes once
void doBlink() {
  eyeLid.write(EYE_CLOSED);
  delay(150);
  eyeLid.write(EYE_OPEN);
}

// Double blink
void doDoubleBlink() {
  doBlink(); delay(200); doBlink();
}

// Wave: raise one arm, nudge elbow, return home
void doWave(bool left) {
  int pose[6];
  copyArmPose(POSE_HOME, pose);
  if (left) {
    pose[0] = 150;
    pose[2] = 120;
  } else {
    pose[3] = 150;
    pose[5] = 120;
  }
  moveArmPose(pose);
  for (int i = 0; i < 3; i++) {
    if (left) pose[2] = (i % 2 == 0) ? 150 : 90;
    else      pose[5] = (i % 2 == 0) ? 150 : 90;
    moveArmPose(pose);
    delay(100);
  }
  moveArmPose(POSE_HOME);
}

// Look left
void lookLeft() {
  smoothWrite(headPan, HP_MIN + 20, 8);
  eyeLeft.write(EYE_MIN + 10);
  eyeRight.write(EYE_MIN + 10);
}

// Look right
void lookRight() {
  smoothWrite(headPan, HP_MAX - 20, 8);
  eyeLeft.write(EYE_MAX - 10);
  eyeRight.write(EYE_MAX - 10);
}


// ═══════════════════════════════════════════════════════════════════════════
//  SENSOR FUNCTIONS (telemetry only)
// ═══════════════════════════════════════════════════════════════════════════

// Trigger one HC-SR04 and return distance in cm (0 = no echo / out of range).
int readUltrasonicCm(uint8_t i) {
  digitalWrite(US_TRIG[i], LOW);
  delayMicroseconds(2);
  digitalWrite(US_TRIG[i], HIGH);
  delayMicroseconds(10);
  digitalWrite(US_TRIG[i], LOW);

  unsigned long dur = pulseIn(US_ECHO[i], HIGH, US_TIMEOUT_US);
  if (dur == 0) return 0;            // timeout → out of range
  return (int)(dur / 58);           // ~58 us per cm round-trip
}

// Emit one telemetry line:  #US:f1,f2,f3,b1,b2,b3;IR:F,B,L,R
void sendTelemetry() {
  Serial.print("#US:");
  for (uint8_t i = 0; i < NUM_US; i++) {
    Serial.print(usDistCm[i]);
    if (i < NUM_US - 1) Serial.print(',');
  }
  Serial.print(";IR:");
  for (uint8_t i = 0; i < NUM_IR; i++) {
    Serial.print(irPit[i] ? 1 : 0);
    if (i < NUM_IR - 1) Serial.print(',');
  }
  Serial.print('\n');
}
