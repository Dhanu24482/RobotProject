# OmniServ Firmware

Arduino Mega 2560 firmware for the OmniServ / Lumi robot. Handles motor control
(BTS7960 dual driver), animatronic servos (8-DOF arms: two shoulder axes, an elbow
and a wrist per side), the HC-05 Bluetooth remote, and — as of the sensor upgrade —
6 ultrasonic + 4 IR pit sensors streamed to the Raspberry Pi.

Sketch: [`omniserv_firmware/omniserv_firmware.ino`](omniserv_firmware/omniserv_firmware.ino)

---

## Pinout

### Motors — BTS7960 dual driver

| Signal | Pin | Side |
|--------|-----|------|
| R_EN_1 / L_EN_1 | 8 / 9 | Right driver enable |
| RPWM_1 / LPWM_1 | 5 / 6 | Right motor PWM |
| R_EN_2 / L_EN_2 | 4 / 7 | Left driver enable |
| RPWM_2 / LPWM_2 | 10 / 11 | Left motor PWM |

### Servos

| Servo | Pin |
|-------|-----|
| Head pan | 2 |
| Eye left | 3 |
| Eye right | 12 |
| Eye lid | 13 |
| Left shoulder pitch | 38 |
| Left shoulder roll | 39 |
| Left elbow | 40 |
| Right shoulder pitch | 41 |
| Right shoulder roll | 42 |
| Right elbow | 43 |
| Right wrist | 44 |
| Left wrist | 45 |

> Pins 38–45 avoid the BTS7960 motor pins (4–11). If you validated 6-DOF poses on a
> standalone Mega using `{2,8,4,5,6,7}`, rewire the arms to 38–43 before flashing
> OmniServ (or change `ARM_PINS` in the sketch).

> **Servo budget — do not exceed 12.** 4 head/eye + 8 arm = 12, which is exactly what
> one AVR timer drives. The Servo library fills Timer5 first, then Timer1, and Timer1
> generates `analogWrite()` on pin 11 = `LPWM_2` = left motor reverse. A 13th servo
> silently kills reverse on the left wheel (the robot spins instead of backing up).
> Move `LPWM_2` to free pin 9 before adding one.

> Wrists run off the same external 5 V BEC as the other servos — never the Mega's
> onboard regulator — with a common ground back to the board.

### Wrist calibration

The wrist joint only flexes the hand up and down (the waving motion). It is not a
gripper and does not rotate the hand, so there is no open/close.

Run `<WRIST:TEST>` — a slow sweep through the full travel on both sides — and adjust
two things in the sketch:

- **Travel:** `WRIST_MIN` / `WRIST_MAX` (default `60` / `120`, neutral `90`). Every
  wrist command is clamped to this pair, and the wave and salute angles live inside
  it, so this is the only place to widen or narrow the range. Keep it a few degrees
  short of the mechanical stops.
- **Direction:** if the left wrist flexes opposite to the right during the sweep,
  flip the seventh entry of `reverseArmServo`.

> The wrist entries in `ARM_PINS` are `{45, 44}`, not `{44, 45}` — pin 44 goes to the
> **right** wrist. Crossed the other way each wrist also inherits the wrong mirror
> flag, so both hands flex outward.

> **Do not lower a servo step delay below ~25 ms.** All 12 servos share one AVR
> timer, so each one is only pulsed every 20–25 ms. A faster loop discards the
> intermediate angles and the joint lurches instead of sweeping. Speed comes from
> `WRIST_STEP_DEG` (degrees per tick), never from a shorter delay.

### Lights & horn

| Signal | Pin |
|--------|-----|
| light_SR / light_SL / light_BR / light_HL | A0 / A1 / A2 / A3 |
| horn_Buzz | A4 |

### Sensors (added in the sensor upgrade)

6 x HC-SR04 ultrasonic (3 front spread ~120 deg, 3 rear for reversing) + 4 x digital IR
(down-facing, pit/cliff detection).

| Sensor | Trig | Echo |
|--------|------|------|
| Ultrasonic front-left  | 22 | 23 |
| Ultrasonic front-center| 24 | 25 |
| Ultrasonic front-right | 26 | 27 |
| Ultrasonic rear-left   | 28 | 29 |
| Ultrasonic rear-center | 30 | 31 |
| Ultrasonic rear-right  | 32 | 33 |

| IR pit sensor | Pin (digital in) |
|---------------|------------------|
| Front | 34 |
| Back  | 35 |
| Left  | 36 |
| Right | 37 |

> Digital IR modules output LOW when floor is detected and HIGH when no reflection
> (a drop/pit). Adjust `IR_PIT_WHEN_HIGH` in the sketch if your modules are inverted.

---

## Serial protocol

Two links: `Serial` (USB, 115200) to the Pi/ROS2, and `Serial3` (9600) to the HC-05 app.

### Pi -> Arduino (unchanged, USB)

| Command | Meaning |
|---------|---------|
| `<L,R>` | Motor PWM, e.g. `<100,80>` (-255..255 each) |
| `<NOD>` `<SHAKE>` `<EBLINK>` `<CENTER>` | Animations |
| `<WAVE:L>` `<WAVE:R>` | Wave a hand — shoulder lifts, elbow holds, wrist flicks |
| `<HOME>` `<HAND_UP>` `<HAND_DOWN>` | Arm poses (synchronized) |
| `<PULL_UP>` `<PULL_DOWN>` `<SALUTE>` | Arm poses |
| `<GOODBYE>` | Salute, hold, wave the raised wrist, return home |
| `<HP:90>` | Head pan angle |
| `<EL:80>` `<ER:100>` `<EY:x,y>` | Eye servos |
| `<HL:90>` `<HR:90>` `<HANDS:l,r>` | Compat: map to shoulder pitch |
| `<WL:110>` `<WR:110>` `<WRISTS:l,r>` | Wrist flex angles, clamped to 60–120 |
| `<WRIST:UP>` `<WRIST:DOWN>` `<WRIST:CENTER>` | Both wrists to max / min / neutral |
| `<WRIST:TEST>` | Slow calibration sweep of the full travel |
| `<LOOK:L>` `<LOOK:R>` `<LOOK:C>` | Look left / right / center |
| `<EBLINK2>` | Double blink |

Wrist angles are in robot space: `90` is the hand in line with the forearm, and the
left wrist is mirrored in firmware (`reverseArmServo`) so the same number means the
same flex on both hands. The named arm poses all keep the wrists neutral — they set
where the hand *is*, not how it is angled — except `SALUTE`, which tilts the right
hand up to the brow. `WAVE:L` / `WAVE:R` raise the arm with the shoulder, hold the
forearm with the elbow, and flap only the wrist.

### Arduino -> Pi (telemetry, added in the sensor upgrade)

Emitted at ~10 Hz, never alters motor behavior (Nav2 decides what to do):

```
#US:f1,f2,f3,b1,b2,b3;IR:F,B,L,R
```

- `US:` six ultrasonic distances in cm, order front-left, front-center, front-right,
  rear-left, rear-center, rear-right. A value of `0` means out-of-range / no echo.
- `IR:` four pit flags (1 = pit/drop detected) in order front, back, left, right.

### Bluetooth app -> Arduino (unchanged, Serial3)

`W` forward, `F` back, `A` left, `D` right, `S` stop, `0`-`9`/`k` speed levels,
`B`/`b` lights, `M`/`m` horn, `N`/`n` nod/shake, `V`/`v` wave, `K` blink, `C` center,
`G`/`g`/`H` wrist up/down/neutral.

---

## Flashing from the Raspberry Pi

No PC or Arduino IDE needed — `arduino-cli` runs on the Pi and flashes over the
existing USB cable. See [../ENVIRONMENT_SETUP.md](../ENVIRONMENT_SETUP.md) for the
one-time install.

```bash
# From the workspace root:
./scripts/flash_arduino.sh
```

The script compiles, frees `/dev/arduino` (stops `arduino_bridge`), uploads, and
restarts the node. To only check that the sketch compiles:

```bash
./scripts/flash_arduino.sh --compile-only
```

> A serial port has a single owner. If `arduino_bridge` is holding `/dev/arduino`,
> the upload fails with "port busy" — the script handles this for you.
