# OmniServ / "Varys" — Autonomous Reception Robot

A ROS 2 (Humble) workspace for a voice-controlled, LiDAR-based autonomous service robot running on a Raspberry Pi. The robot ("Varys") navigates to named rooms, responds to spoken commands, answers questions via a Gemini LLM, and drives a physical differential-drive base plus animatronic head and arms through an Arduino Mega.

> **New to this project?** Start with [ENVIRONMENT_SETUP.md](ENVIRONMENT_SETUP.md) to install all dependencies before building.
>
> **Planned hardware upgrade (not yet installed):** ultrasonic + IR pit sensors are
> prepared in code/firmware but the physical sensors are not wired yet — see
> [docs/SENSOR_UPGRADE.md](docs/SENSOR_UPGRADE.md) for status and the install checklist.

---

## Table of Contents

1. [System Overview](#1-system-overview)
2. [Repository Layout](#2-repository-layout)
3. [The Two Core Nodes](#3-the-two-core-nodes)
   - [arduino_bridge](#31-arduino_bridge)
   - [voice_node (Varys)](#32-voice_node--varys)
4. [Localization, Mapping & Navigation](#4-localization-mapping--navigation)
5. [Robot Model (URDF)](#5-robot-model-urdf)
6. [Web Interface](#6-web-interface)
7. [Quick Start — Clone & Run](#7-quick-start--clone--run)
8. [Full Run Guide](#8-full-run-guide)
9. [Manual Testing Commands](#9-manual-testing-commands)
10. [Key External Dependencies](#10-key-external-dependencies)
11. [Maintainer Notes](#11-maintainer-notes)

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
│   ├── omni_base/               ← custom robot package
│   │   ├── omni_base/
│   │   │   ├── arduino_bridge.py
│   │   │   └── voice_node.py
│   │   ├── launch/              ← description / hardware / mapping / navigation
│   │   ├── config/
│   │   │   ├── ekf.yaml
│   │   │   ├── slam_params.yaml
│   │   │   ├── rooms.yaml       ← canonical room coordinates
│   │   │   └── nav2_params.yaml
│   │   ├── urdf/omniserv.urdf   ← robot URDF model (single source of truth)
│   │   ├── maps/                ← my_room_map.pgm / .yaml
│   │   ├── web_interface/index.html
│   │   ├── package.xml
│   │   └── setup.py
│   ├── rf2o_laser_odometry/     ← 3rd-party: laser scan-matching odometry
│   └── sllidar_ros2/            ← 3rd-party: Slamtec RPLiDAR A1 driver
├── scripts/                     ← flash_arduino.sh, save_map.sh
├── omniserv_map.pgm             ← saved occupancy-grid map (legacy duplicate)
├── omniserv_map.yaml            ← map metadata (res 0.05 m/px)
├── omniserv_boot.sh             ← mapping entrypoint (calls mapping.launch.py)
├── .gitmodules                  ← upstream sources for vendor packages
└── ENVIRONMENT_SETUP.md         ← full OS + ROS + dependency install guide
```

---

## 3. The Two Core Nodes

### 3.1 `arduino_bridge`

**File:** `src/omni_base/omni_base/arduino_bridge.py`

Translates ROS messages into serial commands for the Arduino Mega on `/dev/arduino` at 115 200 baud.

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

- `wheel_separation` = 0.35 m, `max_speed` = 1.0 m/s, `max_pwm` = 30, `min_pwm` = 20 (deadband clamp)
- Sent to Arduino as `<L,R>\n`

**`/robot/body/command` accepted strings**

| Format | Example | Action |
|--------|---------|--------|
| Direct PWM | `30,30` or `<-20,-20>` | Immediate motor command |
| Drive shortcut | `FORWARD`, `BACKWARD`, `LEFT`, `RIGHT` | Timed drive (2–2.5 s) in background thread |
| Stop | `STOP` | Sends `<0,0>` immediately |
| Servo / animation | `NOD`, `SHAKE`, `EBLINK`, `CENTER`, `WAVE:L`, `WAVE:R` | Forwarded as `<CMD>` |
| Servo pose prefix | `HP:`, `EL:`, `ER:`, `EY:`, `HL:`, `HR:`, `HANDS:` | Forwarded as `<CMD>` |

Timed drive pulses the Arduino at **10 Hz** to keep its watchdog alive. On node shutdown, `<0,0>` is always sent to stop the motors.

---

### 3.2 `voice_node` — "Varys"

**File:** `src/omni_base/omni_base/voice_node.py`

The robot's brain: continuous microphone listener → speech-to-text → command router → TTS response.

**Publishers**

| Topic | Type | Description |
|-------|------|-------------|
| `/goal_pose` | `geometry_msgs/PoseStamped` | Nav2 navigation goal |
| `/robot/body/command` | `std_msgs/String` | Arduino body/servo commands |
| `/robot/voice/command` | `std_msgs/String` | Raw recognized speech text |
| `/robot/head/pose` | `std_msgs/Float32MultiArray` | Head servo target pose |
| `/robot/hands/pose` | `std_msgs/Float32MultiArray` | Hands servo target pose |

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

**Supported voice commands**

| Category | Example phrases |
|----------|----------------|
| Navigation | "go to room 1", "navigate to reception", "take me to lobby", "go home" |
| Movement | "go forward", "move back", "turn left", "turn right", "stop" |
| Head / body | "look left", "look right", "look forward", "nod", "shake", "blink", "wink" |
| Arms | "wave", "wave left", "wave right", "hands up", "hands down" |
| Reset | "reset", "center" |
| AI question | anything else → answered by Gemini |

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

**TTS engine** (auto-detected at startup, degrades gracefully)

1. **gTTS** — online, British English, post-processed with `ffmpeg` EQ, played via `aplay`
2. **pico2wave + sox** — offline, pitch-shifted −300 cents, 0.95× tempo ("engineered male" voice)
3. **espeak** — last-resort fallback (always available)

**AI integration**

Requires the `GEMINI_API_KEY` environment variable. Uses `google-genai` with a persistent chat session (`gemini-2.5-flash`). System prompt keeps answers ≤ 2 sentences and in-character as "Varys the robot". The node starts and works fully without the key; only open-ended AI questions are degraded.

---

## 4. Localization, Mapping & Navigation

| Component | Package | Role |
|-----------|---------|------|
| LiDAR driver | `sllidar_ros2` | Publishes `/scan` from RPLiDAR A1 on `/dev/rplidar` |
| Laser odometry | `rf2o_laser_odometry` | Scan-matching → `/odom` (+ `odom`→`base_link` TF) |
| SLAM | `slam_toolbox` (async) | Builds live map from `/scan` + odometry |
| Localization | AMCL (Nav2) | Particle filter, differential motion model |
| Global planner | NavFn (Nav2) | Dijkstra, 0.5 m goal tolerance |
| Local planner | DWB (Nav2) | max 0.20–0.26 m/s linear, 0.50 rad/s angular |
| Sensor fusion | `robot_localization` EKF | Fuses `wheel/odom` + `/odom` *(disabled — see notes)* |

**Nav2 tuning highlights** (`config/nav2_params.yaml`)

- Robot radius: 0.32 m · Inflation radius: 0.55 m
- Controller: 20 Hz · Local costmap: 10 Hz · Global costmap: 1 Hz
- DWB critics: `RotateToGoal`, `PathAlign`, `GoalAlign`, `PathDist`, `GoalDist`, `BaseObstacle`, `Oscillation`
- Goal tolerance: 0.25 m (xy), 0.25 rad (yaw)

**Saved map** (`omniserv_map.yaml`)
- Resolution: 0.05 m/px · Origin: (−9.34, −2.26, 0) · Occupied threshold: 0.65

---

## 5. Robot Model (URDF)

**File:** `omniserv.urdf` · Robot name: `omniserv`

```
base_link  (root — ⌀0.60 m cylinder, h=0.15 m)
├── base_footprint   (fixed, zero offset)
├── laser            (fixed, z=+0.15 m, yaw=180° — corrects front/back)
└── pedestal_link    (⌀0.08 m, h=0.20 m)
    └── torso_link   (0.15 × 0.30 × 0.40 m box)
        ├── head_link      (0.10 × 0.20 × 0.15 m, screen-blue)
        │   ├── left_eye   (r=0.02 m, glowing yellow)
        │   └── right_eye  (r=0.02 m, glowing yellow)
        ├── left_arm       (⌀0.08 m cylinder, 0.40 m long)
        └── right_arm      (⌀0.08 m cylinder, 0.40 m long)
```

Key design decisions:
- `base_link` is the TF **root** so rf2o's `odom → base_link` transform works without conflicts.
- Laser yaw-rotated **180°** to match physical sensor mounting direction.

---

## 6. Web Interface

**File:** `src/omni_base/web_interface/index.html`

A standalone browser dashboard that renders the live Nav2 global costmap in real time using **roslibjs** + **ros2djs**. Open it from any device on the same Wi-Fi network as the Pi.

**Setup steps:**

1. Install `rosbridge_suite` (already included in the ROS Humble desktop install):
   ```bash
   sudo apt install ros-humble-rosbridge-suite
   ```

2. Start the WebSocket bridge on the Pi:
   ```bash
   source /opt/ros/humble/setup.bash
   ros2 launch rosbridge_server rosbridge_websocket_launch.xml
   ```

3. Edit `index.html` — replace `raspberrypi_ip_address` with the Pi's actual IP:
   ```js
   url : 'ws://192.168.1.XXX:9090'
   ```

4. Open `index.html` in any browser on the same network.

---

## 7. Quick Start — Clone & Run

```bash
# 1. Clone the repository
git clone https://github.com/Dhanu24482/RobotProject.git ~/ros2_ws
cd ~/ros2_ws

# 2. Source ROS 2
source /opt/ros/humble/setup.bash

# 3. Build
colcon build
source install/setup.bash

# 4. Set your Gemini API key(s) (optional — only needed for AI Q&A)
#    Comma-separated list enables automatic key rotation on rate limits.
export GEMINI_API_KEYS="key_one,key_two"

# 5a. Build a map (drive with the BT remote, then save):
ros2 launch omni_base mapping.launch.py
#    ...in another terminal once the map looks good:
./scripts/save_map.sh && colcon build --packages-select omni_base

# 5b. OR run autonomous navigation on the saved map:
ros2 launch omni_base navigation.launch.py use_voice:=true use_web:=true
```

> Everything (LiDAR, Arduino bridge, odometry, robot description, and either
> SLAM or Nav2) now starts from **one** launch file. See section 8 for details.
>
> If this is a fresh system, follow [ENVIRONMENT_SETUP.md](ENVIRONMENT_SETUP.md) first.

---

## 8. Full Run Guide

### Step 1 — Source the workspace

Add this to `~/.bashrc` so it loads automatically in every terminal:

```bash
echo "source /opt/ros/humble/setup.bash" >> ~/.bashrc
echo "source ~/ros2_ws/install/setup.bash" >> ~/.bashrc
source ~/.bashrc
```

### Step 2 — Set device permissions

The RPLiDAR and Arduino need serial port access. Run once after each reboot, or make permanent with udev rules:

```bash
# Temporary (resets on reboot)
sudo chmod 666 /dev/rplidar
sudo chmod 666 /dev/arduino

# Permanent udev rules (run once)
cd ~/ros2_ws/src/sllidar_ros2/scripts
sudo ./create_udev_rules.sh
# Then create a similar rule for Arduino:
echo 'SUBSYSTEM=="tty", ATTRS{product}=="Arduino Mega 2560", SYMLINK+="arduino"' \
  | sudo tee /etc/udev/rules.d/99-arduino.rules
sudo udevadm control --reload-rules && sudo udevadm trigger
```

### Step 3 — Build the workspace

```bash
cd ~/ros2_ws
colcon build --symlink-install
source install/setup.bash
```

`--symlink-install` means Python file edits take effect without rebuilding.

### Step 4 — Set Gemini API key(s) (optional)

Get a free key at [aistudio.google.com](https://aistudio.google.com). You can
supply several comma-separated keys; the voice node rotates to the next one
when a key hits its rate/quota limit and puts the busy key on a short cooldown.

```bash
echo 'export GEMINI_API_KEYS="key_one,key_two"' >> ~/.bashrc
source ~/.bashrc
```

> **Security:** API keys live in environment variables **only** — never commit
> them to git. If a key is ever exposed, rotate/revoke it immediately.

### Step 5 — Build a map (mapping mode)

One command brings up the LiDAR, Arduino bridge, robot description, laser
odometry, and `slam_toolbox`:

```bash
ros2 launch omni_base mapping.launch.py
```

This starts:

| # | Process | What it does |
|---|---------|-------------|
| 1 | `robot_state_publisher` | Publishes TF tree from the installed `omniserv.urdf` |
| 2 | `sllidar_ros2` | Reads RPLiDAR A1, publishes `/scan` |
| 3 | `rf2o_laser_odometry` | Scan-matches `/scan` → `/odom` (+ `odom`→`base_link` TF) |
| 4 | `arduino_bridge` | Opens `/dev/arduino`, bridges ROS ↔ motors/servos |
| 5 | `slam_toolbox` | Builds live occupancy map (provides `map`→`odom`) |

Drive the robot around with the BT remote. When the map looks complete, save it:

```bash
./scripts/save_map.sh                       # writes src/omni_base/maps/my_room_map.{pgm,yaml}
colcon build --packages-select omni_base    # installs the updated map
```

> The legacy `./omniserv_boot.sh` still works — it now simply calls
> `ros2 launch omni_base mapping.launch.py`.

### Step 6 — Autonomous navigation (navigation mode)

One command brings up the same hardware plus the full Nav2 stack on the saved map:

```bash
ros2 launch omni_base navigation.launch.py
```

Launch arguments:

| Argument | Default | Effect |
|----------|---------|--------|
| `use_voice` | `false` | Also start the voice control node |
| `use_web` | `false` | Also start the `rosbridge_websocket` for the web UI |
| `map` | installed `maps/my_room_map.yaml` | Map to navigate on |
| `params_file` | installed `config/nav2_params.yaml` | Nav2 parameters |
| `lidar_port` | `/dev/rplidar` | RPLiDAR serial port |
| `arduino_port` | `/dev/arduino` | Arduino serial port |

Typical full run (navigation + voice + web dashboard):

```bash
ros2 launch omni_base navigation.launch.py use_voice:=true use_web:=true
```

When the voice node starts you will hear: **"Varys online. Navigation and AI
systems ready."** The robot is now listening — speak a command.

### Step 7 — (Optional) Web interface

With `use_web:=true` the WebSocket bridge is already running. Open
`src/omni_base/web_interface/index.html` in a browser (with the Pi's IP filled
in) for the live map, sensor panels, and click-to-navigate.

---

## 9. Manual Testing Commands

Test individual subsystems without the voice node using `ros2 topic pub`.

### Navigation

```bash
# Go to room 1
ros2 topic pub --once /goal_pose geometry_msgs/PoseStamped \
  "{header: {frame_id: 'map'}, pose: {position: {x: 0.703, y: -0.069}, orientation: {z: 0.812, w: 0.584}}}"

# Go home (origin)
ros2 topic pub --once /goal_pose geometry_msgs/PoseStamped \
  "{header: {frame_id: 'map'}, pose: {position: {x: 0.0, y: 0.0}, orientation: {w: 1.0}}}"
```

### Drive commands

```bash
ros2 topic pub --once /robot/body/command std_msgs/String "{data: 'FORWARD'}"
ros2 topic pub --once /robot/body/command std_msgs/String "{data: 'BACKWARD'}"
ros2 topic pub --once /robot/body/command std_msgs/String "{data: 'LEFT'}"
ros2 topic pub --once /robot/body/command std_msgs/String "{data: 'RIGHT'}"
ros2 topic pub --once /robot/body/command std_msgs/String "{data: 'STOP'}"
```

### Direct PWM

```bash
# Both motors forward at PWM 50
ros2 topic pub --once /robot/body/command std_msgs/String "{data: '50,50'}"

# Spin in place
ros2 topic pub --once /robot/body/command std_msgs/String "{data: '-50,50'}"
```

### Servo / animation

```bash
ros2 topic pub --once /robot/body/command std_msgs/String "{data: '<NOD>'}"
ros2 topic pub --once /robot/body/command std_msgs/String "{data: '<SHAKE>'}"
ros2 topic pub --once /robot/body/command std_msgs/String "{data: '<WAVE:R>'}"
ros2 topic pub --once /robot/body/command std_msgs/String "{data: '<EBLINK>'}"
ros2 topic pub --once /robot/body/command std_msgs/String "{data: '<CENTER>'}"
```

### Monitor topics

```bash
ros2 topic echo /scan                        # LiDAR data
ros2 topic echo /odom                        # rf2o odometry
ros2 topic echo /robot/voice/command         # what voice_node heard
ros2 topic echo /cmd_vel                     # Nav2 velocity commands
ros2 topic list                              # all active topics
```

---

## 10. Key External Dependencies

### ROS 2 packages

| Package | Role |
|---------|------|
| `ros-humble-desktop` | Base ROS 2 + tools |
| `ros-humble-navigation2` | Nav2 full stack |
| `ros-humble-nav2-bringup` | Nav2 launch files |
| `ros-humble-slam-toolbox` | SLAM mapping |
| `ros-humble-robot-localization` | EKF sensor fusion |
| `ros-humble-robot-state-publisher` | URDF → TF |
| `ros-humble-rosbridge-suite` | WebSocket bridge for web UI |

### Python libraries

| Package | Version | Role |
|---------|---------|------|
| `pyserial` | 3.5 | Arduino serial communication |
| `SpeechRecognition` | 3.16+ | Microphone + Google STT |
| `google-genai` | 2.8+ | Gemini 2.5 Flash API |
| `gTTS` | 2.5+ | Google Text-to-Speech |

### System tools

| Tool | Package | Role |
|------|---------|------|
| `ffmpeg` | `ffmpeg` | TTS audio post-processing |
| `aplay` | `alsa-utils` | Audio playback |
| `mpg123` | `mpg123` | MP3 playback |
| `pico2wave` | `libttspico-utils` | Offline TTS synthesis |
| `sox` | `sox` | Audio pitch/tempo shifting |
| `espeak` | `espeak` | Last-resort TTS fallback |

### Hardware

| Component | Interface | Description |
|-----------|-----------|-------------|
| Raspberry Pi | — | Runs ROS 2 Humble (Ubuntu 22.04) |
| Arduino Mega 2560 | `/dev/arduino` (USB serial, 115200) | Motor driver + servo controller |
| Slamtec RPLiDAR A1 | `/dev/rplidar` (USB serial) | 360° laser scanner |
| USB microphone | ALSA default input | Voice command capture |
| USB / 3.5 mm speaker | ALSA default output | TTS audio playback |

---

## 11. Maintainer Notes

- **Packaging:** `package.xml` / `setup.py` now carry real metadata and runtime `exec_depend`s, and `setup.py` installs `launch/`, `config/`, `urdf/`, and `maps/` into the package share dir, so the launch files resolve resources without absolute `~/ros2_ws/...` paths.
- **Single source of truth for the URDF:** the model now lives at `src/omni_base/urdf/omniserv.urdf` and is read from the installed share path by `description.launch.py`. The old root-level copy was removed.
- **Two map files** exist: `omniserv_map.*` at workspace root and `src/omni_base/maps/my_room_map.*` (the one Nav2 loads via `navigation.launch.py`). The root copy is a duplicate and can be removed once confirmed unused.
- **EKF (`config/ekf.yaml`)** is provided but not launched. Enable it once wheel encoder odometry is wired into the Arduino and publishing on `/wheel/odom`.
- **Room 4, reception, and lobby** coordinates in `config/rooms.yaml` are rough estimates — measure and update them from the saved map once the environment is finalized. Keep the web UI `ROOMS` list (`web_interface/index.html`) in sync with `config/rooms.yaml`.
