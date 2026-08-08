# Sensor Upgrade & Improvements — Status Document

This document records the software, firmware, and configuration changes made to
prepare OmniServ / Lumi for ultrasonic + IR pit sensing, plus several related
improvements (on-Pi Arduino flashing, Gemini key rotation, web UI).

> IMPORTANT — current status: **The new sensors are NOT physically installed yet.**
> Everything described here is implemented in code and ready, but it is *dormant*
> until the hardware is wired and the firmware is flashed. The robot's existing
> behavior (motors, servos, Bluetooth, Nav2, voice) is unchanged and keeps working
> exactly as before. Nothing below is required for the robot to operate today.

---

## 1. What this upgrade will add (once hardware is installed)

- **6 × HC-SR04 ultrasonic** sensors: 3 across the front (~120° spread) and 3 on
  the rear (for reversing). They detect low obstacles that sit *below* the LiDAR
  plane (e.g. a small rock).
- **4 × digital IR** sensors (front/back/left/right), down-facing, to detect pits
  / drop-offs in the floor.
- These feed the Nav2 **local costmap** exactly like the LiDAR does, so during
  autonomous goal navigation the robot reroutes around obstacles and pits on the
  fly. They never hard-stop the robot. During manual Bluetooth mapping, only the
  LiDAR is used — these sensors do not participate.

This is purely a navigation aid. The decision to keep it costmap-based (not a
firmware emergency stop) was deliberate per the design discussion.

---

## 2. Current implementation status

| Area | Change | State |
|------|--------|-------|
| Firmware | Sensor reading + telemetry + `<LOOK:*>`/`<EBLINK2>` commands | Implemented and flashed; still streams telemetry |
| ROS `arduino_bridge` | Reads telemetry, publishes `Range` + pit `PointCloud2` | Implemented but **disabled by default** (`publish_sensors:=false`) |
| URDF | 10 sensor TF frames | Implemented |
| Nav2 | Ultrasonic + pit costmap layers | Configured but **not loaded** — `range_layer`/`pit_layer` are off the `plugins` list |
| voice_node | `look`/`hands` commands wired; Gemini key rotation | Implemented and active now |
| Web UI | New dashboard with sensor panels | Implemented (sensor panels show `--` while telemetry is off) |
| Tooling | `arduino-cli` flashing from the Pi | Implemented |

### Why they are switched off

Once the sensors were physically installed, both produced frequent false positives
indoors: the HC-SR04 ring picked up phantom returns and the IR modules flagged pits
on ordinary flooring. That mattered more than it sounds, because `range_layer` and
`pit_layer` both **mark** cells that the LiDAR cannot **clear** — `pit_layer` is
explicitly `clearing: False` — so every false reading left a permanent keep-out blob
in the local costmap until it scrolled out of the rolling window. The robot refused
routes through clear space.

Navigation therefore runs on the RPLiDAR alone, which is what the global costmap
already used.

### Turning them back on

Two independent switches, both reversible without touching the firmware:

1. **Telemetry** — launch with `publish_sensors:=true` to restore `/ultrasonic/*` and
   `/pit_obstacles`. Useful for watching raw values while filtering.
2. **Costmap influence** — add `"range_layer"` and/or `"pit_layer"` back to the
   `plugins` list of `local_costmap` in
   [`config/nav2_params.yaml`](../src/omni_base/config/nav2_params.yaml). Both blocks
   are still there, marked inert, with their tuning intact.

Do step 1 first and confirm the readings are trustworthy before step 2. If you re-add
`pit_layer`, consider giving it a non-`False` clearing policy or a short expiry so a
single bad frame cannot permanently block a route.

---

## 3. Files added / changed

### Added
- [`firmware/omniserv_firmware/omniserv_firmware.ino`](../firmware/omniserv_firmware/omniserv_firmware.ino) — the robot sketch, now versioned in the repo.
- [`firmware/README.md`](../firmware/README.md) — pinout, serial protocol, flash procedure.
- [`scripts/flash_arduino.sh`](../scripts/flash_arduino.sh) — compile + flash from the Pi.
- This document.

### Changed
- [`src/omni_base/omni_base/arduino_bridge.py`](../src/omni_base/omni_base/arduino_bridge.py) — bidirectional serial, write lock, sensor publishers.
- [`src/omni_base/omni_base/voice_node.py`](../src/omni_base/omni_base/voice_node.py) — look/hands commands, Gemini key rotation.
- [`src/omni_base/urdf/omniserv.urdf`](../src/omni_base/urdf/omniserv.urdf) — sensor TF frames.
- [`src/omni_base/config/nav2_params.yaml`](../src/omni_base/config/nav2_params.yaml) — local-costmap sensor layers.
- [`src/omni_base/web_interface/index.html`](../src/omni_base/web_interface/index.html) — full dashboard.
- [`ENVIRONMENT_SETUP.md`](../ENVIRONMENT_SETUP.md) — arduino-cli install, rotating API keys.

---

## 4. Features active NOW (no hardware needed)

These work today on the current robot:

- **On-Pi Arduino flashing** — `./scripts/flash_arduino.sh` compiles and uploads the
  sketch over USB; no PC or Arduino IDE. (Requires the one-time `arduino-cli`
  install in [ENVIRONMENT_SETUP.md](../ENVIRONMENT_SETUP.md).)
- **Gemini API key rotation** — set `GEMINI_API_KEYS="key1,key2,key3"`; the voice
  node rotates to the next key on a rate-limit/quota error (60 s per-key cooldown)
  and falls back gracefully if all are exhausted. Still honors a single
  `GEMINI_API_KEY` if that's all you set.
- **New voice commands** — "look left/right/forward/center" and "hands up/down"
  now actuate (they were previously recognized but did nothing). These require the
  updated firmware to be flashed to drive the servos, but they are harmless before
  then (the Arduino simply ignores unknown commands).
- **New web dashboard** — configurable Pi IP, connection status, live map with
  robot pose + planned path, click-to-navigate, room buttons, voice transcript,
  and manual drive/animation buttons. Sensor panels display `--` until sensors
  exist.

---

## 5. When you ARE ready to install the sensors

A short checklist for later — no action needed now.

1. **Wire the sensors** to the Mega using the pins in
   [firmware/README.md](../firmware/README.md):
   - Ultrasonic trig/echo: pins 22–33.
   - IR digital out: pins 34–37.
   - Adjust `IR_PIT_WHEN_HIGH` in the sketch to match your IR module polarity.
2. **Flash the firmware**: `./scripts/flash_arduino.sh`.
3. **Verify telemetry**: with the node running,
   `ros2 topic echo /ultrasonic/front_center` and `ros2 topic echo /pit_obstacles`.
4. **Confirm TF frames**: `ros2 run tf2_tools view_frames` should show the 10 new
   sensor frames under `base_link`.
5. **Tune mounting** — the sensor positions/yaws in `omniserv.urdf` are estimates;
   measure the real mounts and update them.
6. **Test rerouting** — place a low obstacle and a simulated drop, send a goal, and
   confirm Nav2 plans around them.

Until step 1–2 are done, the sensor topics stay silent and the costmap layers have
no effect — which is the intended, safe behavior.

---

## 6. Notes

- The existing serial protocol (`<L,R>`, `<NOD>`, `<HP:90>`, Bluetooth `W/F/A/D/S`,
  etc.) is byte-for-byte unchanged. The new firmware only *adds* sensor reading,
  telemetry output, and the `<LOOK:*>` / `<EBLINK2>` commands.
- All changes are additive and reversible: dropping the new firmware or reverting
  the config returns the system to its prior state with no side effects.
