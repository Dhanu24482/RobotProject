# Environment Setup Guide

Complete step-by-step guide to set up a fresh Raspberry Pi for the OmniServ / Varys robot from scratch.

**Target platform:** Raspberry Pi 4 (or 5) · Ubuntu 22.04 LTS (64-bit) · ROS 2 Humble

---

## Table of Contents

1. [Flash Ubuntu 22.04 on the Raspberry Pi](#1-flash-ubuntu-2204-on-the-raspberry-pi)
2. [Initial System Setup](#2-initial-system-setup)
3. [Install ROS 2 Humble](#3-install-ros-2-humble)
4. [Install ROS 2 Robot Packages](#4-install-ros-2-robot-packages)
5. [Install Python Dependencies](#5-install-python-dependencies)
6. [Install Audio & TTS Tools](#6-install-audio--tts-tools)
7. [Configure Hardware (Serial Ports)](#7-configure-hardware-serial-ports)
8. [Configure Audio (Microphone & Speaker)](#8-configure-audio-microphone--speaker)
9. [Get the Gemini API Key](#9-get-the-gemini-api-key)
10. [Clone and Build the Workspace](#10-clone-and-build-the-workspace)
11. [Verify Everything Works](#11-verify-everything-works)
12. [Auto-Start on Boot (Optional)](#12-auto-start-on-boot-optional)

---

## 1. Flash Ubuntu 22.04 on the Raspberry Pi

1. Download **Raspberry Pi Imager** from [raspberrypi.com/software](https://www.raspberrypi.com/software/)
2. Insert a microSD card (32 GB+ recommended)
3. Choose OS → **Other general-purpose OS** → **Ubuntu** → **Ubuntu Server 22.04 LTS (64-bit)**
4. Click the gear icon to pre-configure:
   - Set hostname: `omniserv`
   - Enable SSH with password authentication
   - Set username/password (e.g., `user` / your password)
   - Configure Wi-Fi SSID and password
5. Flash and insert into the Pi

> **Desktop vs Server:** Ubuntu Server 22.04 is recommended — it's lighter and ROS 2 runs headlessly. If you need a GUI (RViz, etc.) install Ubuntu Desktop 22.04 instead.

---

## 2. Initial System Setup

SSH into the Pi (find its IP from your router, or connect a monitor):

```bash
ssh user@omniserv.local
```

### Update the system

```bash
sudo apt update && sudo apt upgrade -y
sudo reboot
```

### Install essential build tools

```bash
sudo apt install -y \
  build-essential \
  cmake \
  git \
  python3-pip \
  python3-dev \
  python3-setuptools \
  curl \
  wget \
  nano \
  htop \
  net-tools
```

---

## 3. Install ROS 2 Humble

Follow the official steps for Ubuntu 22.04:

### Set locale

```bash
sudo apt install locales
sudo locale-gen en_US en_US.UTF-8
sudo update-locale LC_ALL=en_US.UTF-8 LANG=en_US.UTF-8
export LANG=en_US.UTF-8
```

### Add the ROS 2 apt repository

```bash
sudo apt install software-properties-common
sudo add-apt-repository universe

sudo apt update && sudo apt install curl -y
sudo curl -sSL https://raw.githubusercontent.com/ros/rosdistro/master/ros.key \
  -o /usr/share/keyrings/ros-archive-keyring.gpg

echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/ros-archive-keyring.gpg] \
  http://packages.ros.org/ros2/ubuntu $(. /etc/os-release && echo $UBUNTU_CODENAME) main" \
  | sudo tee /etc/apt/sources.list.d/ros2.list > /dev/null

sudo apt update
```

### Install ROS 2 Humble Desktop

```bash
sudo apt install -y ros-humble-desktop
```

> On a headless server install use `ros-humble-ros-base` instead to save space, but you won't have RViz or rqt.

### Source ROS 2 in every terminal

```bash
echo "source /opt/ros/humble/setup.bash" >> ~/.bashrc
source ~/.bashrc
```

### Verify installation

```bash
ros2 --version
# Expected: ros2cli 0.18.x
```

---

## 4. Install ROS 2 Robot Packages

These are all the ROS packages used by this project:

```bash
sudo apt install -y \
  ros-humble-navigation2 \
  ros-humble-nav2-bringup \
  ros-humble-slam-toolbox \
  ros-humble-robot-localization \
  ros-humble-robot-state-publisher \
  ros-humble-rosbridge-suite \
  ros-humble-tf2-ros \
  ros-humble-tf2-tools \
  ros-humble-teleop-twist-keyboard \
  ros-humble-rqt \
  ros-humble-rviz2
```

### Install colcon build tool

```bash
sudo apt install -y python3-colcon-common-extensions
```

---

## 5. Install Python Dependencies

```bash
pip3 install \
  pyserial \
  SpeechRecognition \
  google-genai \
  gTTS \
  adafruit-circuitpython-rgb-display \
  adafruit-blinka \
  pillow
```

### Verify each one

```bash
python3 -c "import serial; print('pyserial OK')"
python3 -c "import speech_recognition; print('SpeechRecognition OK')"
python3 -c "from google import genai; print('google-genai OK')"
python3 -c "from gtts import gTTS; print('gTTS OK')"
python3 -c "import adafruit_rgb_display; print('adafruit display OK')"
python3 -c "from PIL import Image; print('Pillow OK')"
```

### PyAudio (required by SpeechRecognition for microphone access)

```bash
sudo apt install -y portaudio19-dev python3-pyaudio
pip3 install pyaudio
```

---

## 6. Install Audio & TTS Tools

### ALSA utils (aplay, arecord)

```bash
sudo apt install -y alsa-utils
```

### ffmpeg

```bash
sudo apt install -y ffmpeg
```

### mpg123 (MP3 playback for gTTS)

```bash
sudo apt install -y mpg123
```

### sox (audio effects — pitch/tempo shifting for offline TTS)

```bash
sudo apt install -y sox libsox-fmt-all
```

### pico2wave (offline TTS voice synthesis)

```bash
sudo apt install -y libttspico-utils
```

### espeak (fallback TTS)

```bash
sudo apt install -y espeak espeak-ng
```

### Verify TTS chain

```bash
# Test pico2wave → sox → aplay pipeline
pico2wave -l=en-GB -w=/tmp/test.wav "Hello, I am Varys." && aplay /tmp/test.wav

# Test espeak fallback
espeak -v en+m3 "Hello from espeak"

# Test gTTS (requires internet)
python3 -c "
from gtts import gTTS
tts = gTTS('hello world', lang='en', tld='co.uk')
tts.save('/tmp/test.mp3')
print('gTTS file saved')
"
mpg123 /tmp/test.mp3
```

---

## 7. Configure Hardware (Serial Ports)

### Add your user to the `dialout` group

This gives permanent serial port access without `sudo`:

```bash
sudo usermod -aG dialout $USER
# Log out and back in for this to take effect
```

### Set up udev rules for RPLiDAR

Creates a stable `/dev/rplidar` symlink:

```bash
cd ~/ros2_ws/src/sllidar_ros2/scripts
sudo ./create_udev_rules.sh
```

### Set up udev rule for Arduino Mega

Creates a stable `/dev/arduino` symlink:

```bash
# First, plug in the Arduino and find its device attributes
udevadm info -a -n /dev/ttyACM0 | grep -E "idVendor|idProduct|serial"

# Create the udev rule (for Arduino Mega 2560)
sudo tee /etc/udev/rules.d/99-arduino.rules > /dev/null <<'EOF'
SUBSYSTEM=="tty", ATTRS{idVendor}=="2341", ATTRS{idProduct}=="0042", SYMLINK+="arduino", MODE="0666"
EOF

# Reload udev rules
sudo udevadm control --reload-rules && sudo udevadm trigger
```

### Verify devices appear

```bash
ls -la /dev/rplidar /dev/arduino
# Both should exist and be readable after plugging in the hardware
```

### Install arduino-cli (flash the Arduino from the Pi)

The Arduino firmware lives in this repo at `firmware/omniserv_firmware/` and is
flashed over the existing USB cable directly from the Pi — no PC or Arduino IDE
needed. Install `arduino-cli` and the AVR core once:

```bash
# Install arduino-cli
curl -fsSL https://raw.githubusercontent.com/arduino/arduino-cli/master/install.sh | sh
sudo mv bin/arduino-cli /usr/local/bin/

# Install the AVR core (provides the Mega 2560 board + avrdude + Servo library)
arduino-cli core update-index
arduino-cli core install arduino:avr
```

Flash the firmware (compiles, frees the port, uploads, restarts the node):

```bash
cd ~/ros2_ws
./scripts/flash_arduino.sh
```

> A serial port has a single owner. The flash script stops `arduino_bridge`
> (which holds `/dev/arduino`) before uploading, then restarts it. If you flash
> manually, stop the node first or the upload fails with "port busy".

See [firmware/README.md](firmware/README.md) for the full pinout and serial protocol.

---

## 7b. Configure Eye Displays (Dual ILI9341 TFT)

The robot's eyes are two 2.4" ILI9341 320x240 TFT displays driven directly from the Raspberry Pi's hardware SPI0 bus by `eyes_node`.

### Enable SPI

```bash
sudo raspi-config
# Interface Options → SPI → Yes → Finish → Reboot
```

Verify the SPI device appears after reboot:

```bash
ls /dev/spidev0.*
# Expected: /dev/spidev0.0  /dev/spidev0.1
```

### Wiring (both displays share power, clock, data, D/C, and RESET)

| Display Pin | Raspberry Pi 4 Pin | GPIO |
|-------------|-------------------|------|
| VCC | Pin 1 (3.3 V) | — |
| GND | Pin 6 (GND) | — |
| SCK | Pin 23 | GPIO 11 (SCLK) |
| SDI (MOSI) | Pin 19 | GPIO 10 (MOSI) |
| D/C | Pin 18 | GPIO 24 |
| RESET | Pin 22 | GPIO 25 |
| LED | Pin 2 or 4 (5 V) | — (pulls from 5 V to prevent dimming) |
| CS (Left eye) | Pin 24 | GPIO 8 / CE0 |
| CS (Right eye) | Pin 26 | GPIO 7 / CE1 |

### Install Python display libraries

```bash
pip3 install adafruit-circuitpython-rgb-display adafruit-blinka pillow
```

### Add user to `spi` and `gpio` groups

```bash
sudo usermod -aG spi,gpio $USER
# Log out and back in for this to take effect
```

### Verify the displays initialise

```bash
ros2 run omni_base eyes_node
# Expected log line: "eyes_node: ILI9341 displays initialized."
# Both displays should show a neutral expression (blue dash on black).
```

You can test expressions manually by publishing to `/robot/emotion`:

```bash
ros2 topic pub --once /robot/emotion std_msgs/String "{data: 'happy'}"
ros2 topic pub --once /robot/emotion std_msgs/String "{data: 'surprised'}"
ros2 topic pub --once /robot/emotion std_msgs/String "{data: 'angry'}"
ros2 topic pub --once /robot/emotion std_msgs/String "{data: 'sad'}"
ros2 topic pub --once /robot/emotion std_msgs/String "{data: 'blink'}"
```

> **Dev machine (Windows/non-Pi):** `eyes_node` starts in headless mode automatically if the Adafruit libraries are unavailable — it still subscribes and logs emotions, so the ROS graph never breaks.

---

## 8. Configure Audio (Microphone & Speaker)

### List available audio devices

```bash
aplay -l     # output devices (speakers)
arecord -l   # input devices (microphones)
```

### Test microphone recording

```bash
# Record 3 seconds, then play back
arecord -d 3 -f cd /tmp/mic_test.wav && aplay /tmp/mic_test.wav
```

### Test speaker output

```bash
speaker-test -t wav -c 2
```

### Set default audio device (if needed)

If you have multiple devices (e.g., USB mic + HDMI), set the correct one as default:

```bash
# Find your card and device numbers from `aplay -l` / `arecord -l`
nano ~/.asoundrc
```

Add:

```
pcm.!default {
    type hw
    card 1        # replace with your card number
    device 0
}
ctl.!default {
    type hw
    card 1
}
```

### Set microphone volume

```bash
alsamixer    # press F6 to select sound card, use arrow keys to adjust
```

---

## 9. Get the Gemini API Key

The voice node uses Google Gemini 2.5 Flash for natural language Q&A. A free tier is available.

1. Go to [aistudio.google.com](https://aistudio.google.com)
2. Sign in with a Google account
3. Click **Get API key** → **Create API key**
4. Copy the key and add it to your environment:

```bash
# Single key:
echo 'export GEMINI_API_KEYS="your_api_key_here"' >> ~/.bashrc
# Or several comma-separated keys to enable automatic rotation on rate limits:
# echo 'export GEMINI_API_KEYS="key1,key2,key3"' >> ~/.bashrc
source ~/.bashrc
```

The voice node reads `GEMINI_API_KEYS` (preferred, comma-separated) and falls
back to the legacy single `GEMINI_API_KEY` if set.

> **Security:** keys live in environment variables **only** — never commit them
> to git or write them to a tracked file. If a key is ever exposed, rotate it.

### Verify the key works

```bash
python3 -c "
import os
from google import genai
client = genai.Client(api_key=os.environ['GEMINI_API_KEYS'].split(',')[0])
response = client.models.generate_content(model='gemini-2.5-flash', contents='Say hello in one word.')
print(response.text)
"
```

> **Note:** The robot works without this key. Navigation, movement, and servo commands all function offline. Only open-ended conversational questions require the key.

---

## 10. Clone and Build the Workspace

```bash
# Clone the repository
git clone https://github.com/Dhanu24482/RobotProject.git ~/ros2_ws
cd ~/ros2_ws

# Source ROS 2
source /opt/ros/humble/setup.bash

# Install any missing ROS dependencies declared in package.xml files
rosdep init        # only needed once per system
rosdep update
rosdep install --from-paths src --ignore-src -r -y

# Build
colcon build --symlink-install

# Source the workspace
source install/setup.bash
echo "source ~/ros2_ws/install/setup.bash" >> ~/.bashrc
```

### Verify the build

```bash
ros2 pkg list | grep omni_base
# Expected: omni_base
```

---

## 11. Verify Everything Works

Run each check in order before the first full boot.

### Check 1 — LiDAR

Plug in the RPLiDAR A1. Start the driver and check data arrives:

```bash
ros2 launch sllidar_ros2 sllidar_a1_launch.py serial_port:=/dev/rplidar &
sleep 3
ros2 topic echo /scan --once
# Should print a LaserScan message with ranges[]
```

### Check 2 — Arduino bridge

Plug in the Arduino. Start the bridge and send a test command:

```bash
ros2 run omni_base arduino_bridge &
sleep 2
ros2 topic pub --once /robot/body/command std_msgs/String "{data: 'STOP'}"
# Motors should receive <0,0> over serial (check Arduino Serial Monitor if wired up)
```

### Check 3 — Voice node (TTS only)

```bash
ros2 run omni_base voice_node
# You should hear: "Varys online. Navigation and AI systems ready."
# Press Ctrl+C to stop
```

### Check 4 — Full stack

Everything starts from a single launch file now. To build a map:

```bash
ros2 launch omni_base mapping.launch.py     # (or ./omniserv_boot.sh)
```

For autonomous navigation on the saved map:

```bash
ros2 launch omni_base navigation.launch.py use_voice:=true use_web:=true
```

Wait ~10 seconds, then confirm all topics are publishing:

```bash
ros2 topic list
# Should include: /scan, /odom, /map, /tf, /tf_static
```

---

## 12. Auto-Start on Boot (Optional)

To start the hardware stack automatically when the Pi powers on, create a `systemd` service.

```bash
sudo tee /etc/systemd/system/omniserv.service > /dev/null <<'EOF'
[Unit]
Description=OmniServ Robot Stack
After=network.target

[Service]
Type=forking
User=user
WorkingDirectory=/home/user/ros2_ws
ExecStart=/home/user/ros2_ws/omniserv_boot.sh
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable omniserv.service
sudo systemctl start omniserv.service

# Check status
sudo systemctl status omniserv.service
```

To disable auto-start:

```bash
sudo systemctl disable omniserv.service
```

---

## Summary — All Commands in Order

```bash
# 1. System
sudo apt update && sudo apt upgrade -y
sudo apt install -y build-essential cmake git python3-pip curl

# 2. ROS 2 Humble
sudo curl -sSL https://raw.githubusercontent.com/ros/rosdistro/master/ros.key \
  -o /usr/share/keyrings/ros-archive-keyring.gpg
echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/ros-archive-keyring.gpg] \
  http://packages.ros.org/ros2/ubuntu jammy main" \
  | sudo tee /etc/apt/sources.list.d/ros2.list > /dev/null
sudo apt update && sudo apt install -y ros-humble-desktop
echo "source /opt/ros/humble/setup.bash" >> ~/.bashrc

# 3. ROS robot packages
sudo apt install -y \
  ros-humble-navigation2 ros-humble-nav2-bringup \
  ros-humble-slam-toolbox ros-humble-robot-localization \
  ros-humble-robot-state-publisher ros-humble-rosbridge-suite \
  python3-colcon-common-extensions

# 4. Python
pip3 install pyserial SpeechRecognition google-genai gTTS \
  adafruit-circuitpython-rgb-display adafruit-blinka pillow
sudo apt install -y portaudio19-dev python3-pyaudio && pip3 install pyaudio

# 5. Audio tools
sudo apt install -y ffmpeg mpg123 sox libsox-fmt-all \
  alsa-utils libttspico-utils espeak espeak-ng

# 6. Hardware access
sudo usermod -aG dialout $USER
cd ~/ros2_ws/src/sllidar_ros2/scripts && sudo ./create_udev_rules.sh

# 6b. arduino-cli (flash the Mega from the Pi)
curl -fsSL https://raw.githubusercontent.com/arduino/arduino-cli/master/install.sh | sh
sudo mv bin/arduino-cli /usr/local/bin/
arduino-cli core update-index && arduino-cli core install arduino:avr

# 7. API key(s) — comma-separated list rotates to dodge rate limits
echo 'export GEMINI_API_KEYS="key1,key2,key3"' >> ~/.bashrc
source ~/.bashrc

# 8. Clone and build
git clone https://github.com/Dhanu24482/RobotProject.git ~/ros2_ws
cd ~/ros2_ws
source /opt/ros/humble/setup.bash
colcon build --symlink-install
echo "source ~/ros2_ws/install/setup.bash" >> ~/.bashrc
source ~/.bashrc
```
