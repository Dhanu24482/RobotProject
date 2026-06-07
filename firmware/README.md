# OmniServ Firmware

Arduino Mega 2560 firmware for the OmniServ / Varys robot. Handles motor control
(BTS7960 dual driver), animatronic servos, the HC-05 Bluetooth remote, and — as of
the sensor upgrade — 6 ultrasonic + 4 IR pit sensors streamed to the Raspberry Pi.

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
| Hand left | 44 |
| Hand right | 45 |

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
| `<WAVE:L>` `<WAVE:R>` | Wave a hand |
| `<HP:90>` | Head pan angle |
| `<EL:80>` `<ER:100>` `<EY:x,y>` | Eye servos |
| `<HL:90>` `<HR:90>` `<HANDS:l,r>` | Hand servos |
| `<LOOK:L>` `<LOOK:R>` `<LOOK:C>` | Look left / right / center (sensor upgrade) |
| `<EBLINK2>` | Double blink (sensor upgrade) |

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
`B`/`b` lights, `M`/`m` horn, `N`/`n` nod/shake, `V`/`v` wave, `K` blink, `C` center.

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
