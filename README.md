# OmniServ / "Varys" — Autonomous Reception Robot

A ROS 2 (Humble) workspace for a voice-controlled, LiDAR-based autonomous service robot running on a Raspberry Pi. The robot ("Varys") navigates to named rooms, responds to spoken commands, answers questions via a Gemini LLM, and drives a physical differential-drive base plus animatronic head and arms through an Arduino Mega.

---

## Table of Contents

1. [System Overview](#1-system-overview)
2. [Repository Layout](#2-repository-layout)
3. [The Two Core Nodes](#3-the-two-core-nodes)
   - [arduino_bridge](#31-arduino_bridge)
   - [voice_node (Varys)](#32-voice_node--varys)
4. [Localization, Mapping & Navigation](#4-localization-mapping--navigation)
5. [Robot Model (URDF)](#5-robot-model-urdfurdf)
6. [Web Interface](#6-web-interface)
7. [How to Build & Run](#7-how-to-build--run)
8. [Key External Dependencies](#8-key-external-dependencies)
9. [Maintainer Notes](#9-maintainer-notes)

---

## 1. System Overview

```
                  ┌────────────────────────────────────────────────┐
                  │              Raspberry Pi (ROS 2 Humble)        │
  🎤 Mic ────────▶│  voice_node                                     │
                  │   ├─ SpeechRecognition (Google STT)             │
                  │   ├─ Gemini 2.5 Flash (AI Q&A)                  │
  🔊 Speaker ◀───│   └─ TTS: gTTS → pico2wave → espeak (fallback)  │
                  │        │                                         │
                  │        ▼  publishes                              │
                  │   /goal_pose ──────▶ Nav2 stack ──▶ /cmd_vel ──┐│
                  │   /robot/body/command ──────────────────────────┘│
                  │                                             │     │
  RPLiDAR A1 ────▶│  sllidar_ros2 ──▶ /scan                    │     │
                  │      └──▶ rf2o_laser_odometry               │     │
                  │      └──▶ slam_toolbox  (live map)          │     │
                  └────────────────────────────────────────────┼─────┘
                                                               ▼
                                                  arduino_bridge (serial)
                                                         │  <L,R>  <NOD> …
                                                         ▼
                                                  🤖 Arduino Mega
                                                  (DC motors + servos)
```

---

## 2. Repository Layout

```
ros2_ws/
├── src/
│   ├── omni_base/               ← custom robot package (original code)
│   │   ├── omni_base/
│   │   │   ├── arduino_bridge.py
│   │   │   └── voice_node.py
│   │   ├── launch/rsp.launch.py
│   │   ├── config/
│   │   │   ├── ekf.yaml
│   │   │   └── nav2_params.yaml
│   │   ├── maps/                ← my_room_map.pgm / .yaml
│   │   ├── web_interface/index.html
│   │   ├── package.xml
│   │   └── setup.py
│   ├── rf2o_laser_odometry/     ← 3rd-party laser odometry
│   └── sllidar_ros2/            ← 3rd-party RPLiDAR driver + SDK
├── omniserv.urdf                ← robot URDF model
├── omniserv_map.pgm             ← saved occupancy-grid map
├── omniserv_map.yaml            ← map metadata (res 0.05 m/px)
└── omniserv_boot.sh             ← full hardware stack launcher
```

> `src/omni_base_backup/` is a legacy copy of `arduino_bridge.py` and is **not** part of the build.

---

## 3. The Two Core Nodes

### 3.1 `arduino_bridge`

**File:** `src/omni_base/omni_base/arduino_bridge.py`

Translates ROS messages into serial commands for the Arduino Mega (`/dev/arduino`, 115 200 baud).

**Subscriptions**

| Topic | Type | Description |
|-------|------|-------------|
| `/cmd_vel` | `geometry_msgs/Twist` | Autonomous navigation velocity from Nav2 |
| `/robot/body/command` | `std_msgs/String` | High-level body/servo commands |

**`/cmd_vel` handling — differential drive mixing:**

```
left_pwm  = linear_x − angular_z × (wheel_separation / 2)
right_pwm = linear_x + angular_z × (wheel_separation / 2)
```

- `wheel_separation` = 0.35 m, `max_speed` = 1.0 m/s, `max_pwm` = 60, `min_pwm` = 45 (deadband clamp).
- Formatted as `<L,R>\n` over serial.

**`/robot/body/command` accepted strings**

| Format | Example | Action |
|--------|---------|--------|
| Direct PWM | `60,60` or `<-20,-20>` | Immediate motor command |
| Drive shortcut | `FORWARD`, `BACKWARD`, `LEFT`, `RIGHT` | Timed drive (2–2.5 s) in background thread |
| Stop | `STOP` | Sends `<0,0>` immediately |
| Servo/animation | `NOD`, `SHAKE`, `EBLINK`, `CENTER`, `WAVE:L`, `WAVE:R` | Forwarded as `<CMD>` |
| Servo pose | `HP:`, `EL:`, `ER:`, `EY:`, `HL:`, `HR:`, `HANDS:` | Forwarded as `<CMD>` |

Timed drive runs at **10 Hz** to keep the Arduino watchdog alive. On shutdown, `<0,0>` is sent to stop the motors.

---

### 3.2 `voice_node` — "Varys"

**File:** `src/omni_base/omni_base/voice_node.py`

The robot's brain: continuous microphone listener → speech-to-text → command router → TTS response.

**Publishers**

| Topic | Type | Description |
|-------|------|-------------|
| `/goal_pose` | `geometry_msgs/PoseStamped` | Nav2 navigation goal |
| `/robot/body/command` | `std_msgs/String` | Arduino body commands |
| `/robot/voice/command` | `std_msgs/String` | Raw recognized speech |
| `/robot/head/pose` | `std_msgs/Float32MultiArray` | Head servo target |
| `/robot/hands/pose` | `std_msgs/Float32MultiArray` | Hands servo target |

**Routing flow**

```
Microphone → Google STT → text
    │
    ├─ contains robot command keyword? ──yes──▶ handle_robot_command()
    │                                               ├─ navigation trigger → handle_room_navigation()
    │                                               │       └─ publishes PoseStamped to /goal_pose
    │                                               └─ motion/animation → body_cmd()
    │
    └─ no ──────────────────────────────────────▶ handle_ai_question()
                                                        └─ Gemini 2.5 Flash → speak()
```

**Known room coordinates** (`map` frame)

| Room | x (m) | y (m) | yaw (rad) |
|------|--------|--------|-----------|
| room 1 | 0.703 | -0.069 | 1.75 |
| room 2 | -0.127 | -0.235 | 3.09 |
| room 3 | -0.198 | -1.068 | 1.83 |
| room 4 | -2.0 | 1.5 | 0.0 |
| reception | -1.0 | 0.5 | 1.57 |
| lobby | 0.5 | -2.0 | 3.14 |
| home | 0.0 | 0.0 | 0.0 |

**TTS engine** (auto-detected, degrades gracefully)

1. **gTTS** — online, British English, post-processed with `ffmpeg` (volume + 2 kHz EQ boost), played via `aplay`
2. **pico2wave + sox** — offline, pitch-shifted −300 cents, 0.95× tempo (engineered "male" voice)
3. **espeak** — last-resort fallback

**AI integration**

Requires `GEMINI_API_KEY` environment variable. Uses `google-genai` with a persistent chat session (`gemini-2.5-flash`). The system instruction keeps answers short (≤ 2 sentences) and in-character as "Varys the robot". Without a key the node still runs fully; AI queries return a graceful "unavailable" message.

---

## 4. Localization, Mapping & Navigation

| Component | Package | Topic / Role |
|-----------|---------|--------------|
| LiDAR driver | `sllidar_ros2` | `/scan` (RPLiDAR A1, `/dev/rplidar`) |
| Laser odometry | `rf2o_laser_odometry` | `/scan → /laser/odom` (scan matching) |
| SLAM | `slam_toolbox` (async) | `/scan + /laser/odom → /map` |
| Localization | AMCL (Nav2) | Particle filter, differential motion model |
| Global planner | NavFn (Nav2) | Dijkstra, 0.5 m tolerance |
| Local planner | DWB (Nav2) | max 0.20–0.26 m/s linear, 0.50 rad/s angular |
| Sensor fusion | `robot_localization` EKF | `wheel/odom` + `laser/odom` → fused odometry |

> The EKF launch line is **commented out** in `omniserv_boot.sh`; rf2o odometry is currently used directly without fusion.

**Nav2 tuning highlights** (`config/nav2_params.yaml`)
- Robot radius: 0.32 m; inflation radius: 0.55 m
- Controller frequency: 20 Hz; costmap update: 10 Hz (local) / 1 Hz (global)
- DWB critics: `RotateToGoal`, `PathAlign`, `GoalAlign`, `PathDist`, `GoalDist`, `BaseObstacle`, `Oscillation`
- Goal tolerance: 0.25 m (xy), 0.25 rad (yaw)

**Saved map** (`omniserv_map.yaml`)
- Resolution: 0.05 m/px; origin: (−9.34, −2.26, 0); occupied threshold: 0.65

---

## 5. Robot Model (URDF)

**File:** `omniserv.urdf`  Robot name: `omniserv`

```
base_link (root — 0.30 m radius cylinder)
├── base_footprint          (fixed, zero offset)
├── laser                   (fixed, z=+0.15 m, yaw=180° to correct front/back)
└── pedestal_link
    └── torso_link  (0.15 × 0.30 × 0.40 m box)
        ├── head_link   (0.10 × 0.20 × 0.15 m, screen-blue)
        │   ├── left_eye   (r=0.02 m sphere, glowing yellow)
        │   └── right_eye  (r=0.02 m sphere, glowing yellow)
        ├── left_arm   (r=0.04 m cylinder, 0.4 m long)
        └── right_arm  (r=0.04 m cylinder, 0.4 m long)
```

Key design decisions captured in URDF comments:
- `base_link` is the TF **root** so rf2o's `odom → base_link` transform works without conflicts.
- Laser is yaw-rotated **180°** to align physical sensor mounting with ROS convention.

---

## 6. Web Interface

**File:** `src/omni_base/web_interface/index.html`

A standalone browser dashboard that renders the live Nav2 global costmap using **roslibjs** + **ros2djs**.

**Setup:**
1. Install and run `rosbridge_suite` on the Pi (not started by `omniserv_boot.sh`):
   ```bash
   ros2 launch rosbridge_server rosbridge_websocket_launch.xml
   ```
2. Edit `index.html` line: `url : 'ws://raspberrypi_ip_address:9090'` → replace with the Pi's actual IP.
3. Open the HTML file in any browser on the same network.

---

## 7. How to Build & Run

### Build

```bash
cd ~/ros2_ws
colcon build
source install/setup.bash
```

### Launch the full hardware stack

```bash
./omniserv_boot.sh
```

Starts (all backgrounded, in order):
1. RPLiDAR A1 driver (`/dev/rplidar`)
2. `arduino_bridge` (`/dev/arduino`)
3. `robot_state_publisher` (URDF)
4. `rf2o_laser_odometry`
5. `slam_toolbox` (async SLAM)

### Launch Nav2

```bash
ros2 launch nav2_bringup navigation_launch.py \
  params_file:=src/omni_base/config/nav2_params.yaml
```

### Run the voice assistant

```bash
export GEMINI_API_KEY="your_key_here"
ros2 run omni_base voice_node
```

> The voice node can run with or without the API key. Without it, speech navigation and body commands still work fully; only open-ended Q&A is disabled.

### Manual body commands (testing without voice)

```bash
# Navigate to room 1 via Nav2
ros2 topic pub --once /goal_pose geometry_msgs/PoseStamped \
  "{header: {frame_id: 'map'}, pose: {position: {x: 0.703, y: -0.069}, orientation: {z: 0.812, w: 0.584}}}"

# Drive forward for 2 s
ros2 topic pub --once /robot/body/command std_msgs/String "{data: 'FORWARD'}"

# Make the robot nod
ros2 topic pub --once /robot/body/command std_msgs/String "{data: '<NOD>'}"
```

---

## 8. Key External Dependencies

**ROS 2 packages**
- `rclpy`, `nav2_bringup`, `slam_toolbox`, `robot_localization`, `robot_state_publisher`, `rosbridge_suite`

**Python libraries**
- `pyserial` — Arduino serial communication
- `SpeechRecognition` — microphone capture + Google STT
- `google-genai` — Gemini 2.5 Flash API
- `gTTS` — Google Text-to-Speech

**System tools**
- `mpg123`, `ffmpeg`, `aplay` — audio playback pipeline
- `pico2wave`, `sox` — offline TTS voice synthesis
- `espeak` — last-resort TTS fallback

**Hardware**
- Raspberry Pi (runs ROS 2 Humble)
- Arduino Mega — motor driver + servo controller (`/dev/arduino`)
- Slamtec RPLiDAR A1 (`/dev/rplidar`)
- USB microphone + speaker

---

## 9. Maintainer Notes

- **`package.xml` / `setup.py` metadata** are placeholder values ("TODO"). Fill in description, license, and maintainer email before publishing.
- **Two map files** exist: `omniserv_map.*` at workspace root and `src/omni_base/maps/my_room_map.*`. The `nav2_params.yaml` `map_server.yaml_filename` field is blank — verify which map is actually loaded at runtime.
- **`launch/rsp.launch.py`** reads the URDF from `share/omni_base/urdf/omniserv.urdf`. The boot script instead passes `~/ros2_ws/omniserv.urdf` directly; the launch file path may not exist after `colcon install` unless the URDF is added to `data_files` in `setup.py`.
- **EKF is disabled** (`# ros2 run robot_localization ekf_node ...` in `omniserv_boot.sh`). Enable it once wheel encoder odometry is wired into the Arduino.
- **Room 4, reception, and lobby** coordinates look like rough placeholders compared to the survey-accurate rooms 1–3; measure and update once the map is finalized.
- **`src/omni_base_backup/`** is dead code; consider deleting it.
