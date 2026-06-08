#!/usr/bin/env python3
"""
Varys Voice Node — Nav2 Goal + Arduino Mega Sync
TTS Priority: gTTS → Engineered Local Male Fallback
"""
import rclpy
from rclpy.node import Node
from ament_index_python.packages import get_package_share_directory
from std_msgs.msg import String
from std_msgs.msg import Float32MultiArray
from geometry_msgs.msg import Twist
from geometry_msgs.msg import PoseStamped
import speech_recognition as sr
import subprocess
import threading
import tempfile
import time
import os
import math
import yaml
import json

# ════════════════════════════════════════════════════════════
#  ROOM COORDINATES
# ════════════════════════════════════════════════════════════
# Fallback used only if config/rooms.yaml cannot be loaded. The canonical
# source of truth is the installed config/rooms.yaml (see load_rooms()).
DEFAULT_ROOMS = {
    'room 1':           (0.703,  -0.069,  1.75  ),
    'room 2':           (-0.127, -0.235,  3.09  ),
    'room 3':           (-0.198, -1.068,  1.83  ),
    'room 4':           (-2.0,    1.5,    0.0   ),
    'reception':        (-1.0,    0.5,    1.57  ),
    'lobby':            ( 0.5,   -2.0,    3.14  ),
    'home':             ( 0.0,    0.0,    0.0   ),
}


def load_rooms(path, logger=None):
    """Load room coordinates from a YAML file, falling back to defaults."""
    try:
        with open(path, 'r') as f:
            data = yaml.safe_load(f) or {}
        rooms = data.get('rooms', {})
        parsed = {str(name): tuple(float(v) for v in coords)
                  for name, coords in rooms.items()}
        if parsed:
            return parsed
        if logger:
            logger.warn(f'rooms file {path} had no rooms; using defaults.')
    except Exception as e:
        if logger:
            logger.warn(f'Could not load rooms file {path}: {e}; using defaults.')
    return dict(DEFAULT_ROOMS)

# ════════════════════════════════════════════════════════════
#  TTS ENGINE
# ════════════════════════════════════════════════════════════
class TTSEngine:
    def __init__(self, logger):
        self.logger = logger
        self.engine = self._detect_best_engine()

    def _detect_best_engine(self):
        try:
            from gtts import gTTS
            result = subprocess.run(['mpg123','--version'], capture_output=True)
            if result.returncode == 0: return 'gtts'
        except Exception as e:
            self.logger.warn(f'gTTS/mpg123 unavailable, using offline male TTS: {e}')
        return 'offline_male'

    def speak(self, text):
        if self.engine == 'gtts': self._gtts_speak(text)
        else: self._engineered_male_speak(text)

    def _gtts_speak(self, text):
        try:
            from gtts import gTTS
            tts = gTTS(text=text, lang='en', tld='co.uk', slow=False)
            with tempfile.NamedTemporaryFile(suffix='.mp3', delete=False) as f:
                tmp_mp3 = f.name
            tts.save(tmp_mp3)
            tmp_wav = tmp_mp3.replace('.mp3', '.wav')
            subprocess.run(['ffmpeg','-y','-i',tmp_mp3,'-af','volume=3.0,equalizer=f=2000:width_type=o:width=2:g=3',tmp_wav], capture_output=True)
            subprocess.run(['aplay','-q',tmp_wav], stderr=subprocess.DEVNULL)
            os.unlink(tmp_mp3)
            if os.path.exists(tmp_wav): os.unlink(tmp_wav)
        except Exception as e:
            self.logger.error(f'gTTS error: {e}')
            self._engineered_male_speak(text)

    def _engineered_male_speak(self, text):
        try:
            with tempfile.NamedTemporaryFile(suffix='.wav', delete=False) as f:
                temp_wav = f.name
            subprocess.run(['pico2wave','-l=en-GB',f'-w={temp_wav}',text], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            p2 = subprocess.Popen(['sox','-t','wav',temp_wav,'-t','wav','-','vol','3.0','pitch','-300','tempo','0.95','treble','+3'], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
            p3 = subprocess.Popen(['aplay','-q'], stdin=p2.stdout, stderr=subprocess.DEVNULL)
            p2.stdout.close(); p3.wait()
            if os.path.exists(temp_wav): os.remove(temp_wav)
        except Exception as e:
            self.logger.warn(f'pico2wave/sox TTS failed, falling back to espeak: {e}')
            try:
                subprocess.run(['espeak','-v','en+m3','-s','125','-p','32','-a','200',text], capture_output=True)
            except Exception as e2:
                self.logger.error(f'espeak fallback also failed: {e2}')

# ════════════════════════════════════════════════════════════
#  AI CLIENT
# ════════════════════════════════════════════════════════════
class AIClient:
    SYSTEM_INSTRUCTION = (
        "You are Varys, an advanced humanoid reception robot. "
        "Keep answers SHORT — maximum 2 sentences. "
        "Never say you are an AI. You ARE Varys the robot."
    )
    # How long to skip a key after it hits a rate/quota limit.
    KEY_COOLDOWN_SEC = 60.0

    def __init__(self, logger):
        self.logger    = logger
        self.keys      = self._load_keys()
        self.cooldowns = {}          # key -> epoch time it becomes usable again
        self.idx       = 0
        self.client    = None
        self.chat      = None
        self.active_key = None
        self.ready     = False
        if self.keys:
            self._activate_next_key()
        else:
            self.logger.warn('No Gemini API key found (GEMINI_API_KEYS / GEMINI_API_KEY).')

    def _load_keys(self):
        # Prefer the comma-separated rotating list, fall back to the single key.
        raw = os.environ.get('GEMINI_API_KEYS', '') or os.environ.get('GEMINI_API_KEY', '')
        return [k.strip() for k in raw.split(',') if k.strip()]

    def _activate_next_key(self):
        # Find the next key that is not in cooldown and build a fresh client/chat.
        now = time.time()
        for _ in range(len(self.keys)):
            key = self.keys[self.idx]
            self.idx = (self.idx + 1) % len(self.keys)
            if self.cooldowns.get(key, 0) > now:
                continue
            if self._build_client(key):
                self.active_key = key
                self.ready = True
                return True
        self.ready = False
        return False

    def _build_client(self, key):
        try:
            from google import genai
            from google.genai import types
            self.client = genai.Client(api_key=key)
            config = types.GenerateContentConfig(system_instruction=self.SYSTEM_INSTRUCTION)
            self.chat = self.client.chats.create(model='gemini-2.5-flash', config=config)
            return True
        except Exception as e:
            self.logger.error(f'AI setup failed for a key: {e}')
            return False

    @staticmethod
    def _is_rate_limit(err):
        s = str(err).lower()
        return ('429' in s or 'resource_exhausted' in s or 'resourceexhausted' in s
                or 'quota' in s or 'rate limit' in s or 'rate_limit' in s)

    def ask(self, question):
        if not self.ready or not self.keys:
            return "My AI module is unavailable right now."
        # Try each available key once; rotate past any that are rate-limited.
        attempts = max(1, len(self.keys))
        for _ in range(attempts):
            try:
                response = self.chat.send_message(question)
                return response.text.strip().replace('*', '').replace('#', '').replace('`', '')
            except Exception as e:
                if self._is_rate_limit(e):
                    self.logger.warn('Gemini key rate-limited — rotating to next key.')
                    self.cooldowns[self.active_key] = time.time() + self.KEY_COOLDOWN_SEC
                    if not self._activate_next_key():
                        return "All my AI keys are busy right now. Please try again shortly."
                    continue
                self.logger.error(f'AI error: {e}')
                return "I am sorry, I could not process that question."
        return "All my AI keys are busy right now. Please try again shortly."

# ════════════════════════════════════════════════════════════
#  VOICE NODE
# ════════════════════════════════════════════════════════════
class VoiceNode(Node):
    def __init__(self):
        super().__init__('voice_node')

        # ── Parameters (defaults preserve previous hardcoded behavior) ──
        default_rooms = os.path.join(
            get_package_share_directory('omni_base'), 'config', 'rooms.yaml')
        self.rooms_file    = self.declare_parameter('rooms_file', default_rooms).value
        self.saved_locations_file = self.declare_parameter(
            'saved_locations_file',
            os.path.join(os.path.expanduser('~'), '.ros', 'varys_saved_locations.yaml')
        ).value
        self.drive_pwm     = self.declare_parameter('drive_pwm', 20).value
        voice_topic        = self.declare_parameter('voice_topic', '/robot/voice/command').value
        body_topic         = self.declare_parameter('body_topic',  '/robot/body/command').value
        head_topic         = self.declare_parameter('head_topic',  '/robot/head/pose').value
        hands_topic        = self.declare_parameter('hands_topic', '/robot/hands/pose').value
        goal_topic         = self.declare_parameter('goal_topic',  '/goal_pose').value

        # Room coordinates: start from canonical static rooms (config/rooms.yaml),
        # then overlay any user-saved locations persisted by the web UI / location_manager.
        self.static_rooms = load_rooms(self.rooms_file, self.get_logger())
        self.rooms = dict(self.static_rooms)

        # Best-effort load of runtime-saved locations so voice works even if
        # location_manager is not running (or hasn't published yet).
        user_locs = self._load_user_locations(self.saved_locations_file)
        if user_locs:
            self.rooms = {**self.rooms, **user_locs}
            self.get_logger().info(
                f'Loaded {len(user_locs)} user-saved location(s) from {self.saved_locations_file} '
                f'(effective rooms: {len(self.rooms)})'
            )

        # Subscribe to latched updates so new saves/deletes from the web UI are picked up live.
        self.create_subscription(String, '/saved_locations', self._on_saved_locations, 10)

        self.voice_pub   = self.create_publisher(String,            voice_topic, 10)
        self.body_pub    = self.create_publisher(String,            body_topic,  10)
        self.head_pub    = self.create_publisher(Float32MultiArray, head_topic,  10)
        self.hands_pub   = self.create_publisher(Float32MultiArray, hands_topic, 10)

        # This publishes directly to ROS2 Nav2 stack
        self.goal_pub    = self.create_publisher(PoseStamped,       goal_topic,  10)

        self.tts = TTSEngine(self.get_logger())
        self.ai  = AIClient(self.get_logger())

        self.recognizer = sr.Recognizer()
        self.recognizer.energy_threshold         = 300
        self.recognizer.pause_threshold          = 0.8
        self.recognizer.dynamic_energy_threshold = True
        self.listening   = True
        self.is_speaking = False

        # Open the microphone once and reuse it (re-opening every loop iteration
        # is slow and can leak audio device handles).
        self.microphone = sr.Microphone()

        # ADDED 'go to', 'navigate to', 'take me to' back into hardcoded list
        self.robot_commands = [
            'go to', 'navigate to', 'drive to', 'take me to', 'go home',
            'go forward', 'move forward', 'go ahead', 'go backward', 'go back', 'move back', 'reverse',
            'turn left', 'rotate left', 'turn right', 'rotate right', 'stop', 'halt', 'emergency stop',
            'look left', 'look right', 'look forward', 'look center', 'nod', 'shake', 'blink', 'wink',
            'wave', 'wave left', 'wave right', 'hands up', 'hands down', 'reset', 'center'
        ]

        try:
            with self.microphone as source: self.recognizer.adjust_for_ambient_noise(source, duration=2)
        except Exception as e: self.get_logger().error(f'Microphone error: {e}')

        time.sleep(1)
        self.speak_async('Varys online. Navigation and AI systems ready.')
        threading.Thread(target=self.listen_loop, daemon=True).start()

    def speak(self, text):
        self.is_speaking = True
        try: self.tts.speak(text)
        except Exception as e: self.get_logger().error(f'Speak error: {e}')
        finally: self.is_speaking = False

    def speak_async(self, text):
        threading.Thread(target=self.speak, args=(text,), daemon=True).start()

    def listen_loop(self):
        while self.listening:
            if self.is_speaking:
                time.sleep(0.3)
                continue
            try:
                with self.microphone as source: audio = self.recognizer.listen(source, timeout=5, phrase_time_limit=10)
                text = self.recognizer.recognize_google(audio).lower().strip()
                self.get_logger().info(f'Heard: "{text}"')
                msg = String(); msg.data = text; self.voice_pub.publish(msg)
                self.route(text)
            except sr.WaitTimeoutError: pass
            except sr.UnknownValueError: pass
            except Exception as e:
                self.get_logger().warn(f'Listen loop error: {e}')
                time.sleep(1)

    def route(self, text):
        for cmd in self.robot_commands:
            if cmd in text:
                self.handle_robot_command(text)
                return
        self.handle_ai_question(text)

    def handle_robot_command(self, text):
        # ── NAV2 ROOM NAVIGATION ──
        if any(w in text for w in ['go to', 'navigate to', 'drive to', 'take me to']):
            self.handle_room_navigation(text)
            return
            
        elif 'go home' in text:
            self.handle_room_navigation('go to home')
            return

        # ── MOVEMENT (Arduino Expects <LEFT_PWM,RIGHT_PWM>) ──
        elif any(w in text for w in ['go forward','move forward','go ahead']):
            p = self.drive_pwm
            self.speak_async('Moving forward.')
            self.body_cmd(f'<{p},{p}>')

        elif any(w in text for w in ['go backward','go back','move back','reverse']):
            p = self.drive_pwm
            self.speak_async('Moving backward.')
            self.body_cmd(f'<{-p},{-p}>')

        elif any(w in text for w in ['turn left','rotate left']):
            p = self.drive_pwm
            self.speak_async('Turning left.')
            self.body_cmd(f'<{-p},{p}>')

        elif any(w in text for w in ['turn right','rotate right']):
            p = self.drive_pwm
            self.speak_async('Turning right.')
            self.body_cmd(f'<{p},{-p}>')

        elif any(w in text for w in ['stop','halt','emergency stop']):
            self.speak_async('Stopped.')
            self.body_cmd('<0,0>')

        # ── ANIMATIONS (Arduino Expects <COMMAND>) ──
        elif 'nod'   in text: self.body_cmd('<NOD>');   self.speak_async('Yes!')
        elif 'shake' in text: self.body_cmd('<SHAKE>'); self.speak_async('No!')
        elif 'blink' in text or 'wink' in text: self.body_cmd('<EBLINK>')
        elif 'wave left'  in text: self.body_cmd('<WAVE:L>'); self.speak_async('Hello!')
        elif 'wave'       in text: self.body_cmd('<WAVE:R>'); self.speak_async('Hello there!')

        # ── HEAD LOOK (must precede 'center' so "look center" is not eaten by reset) ──
        elif 'look left'  in text:
            self.body_cmd('<LOOK:L>'); self.publish_head(30.0)
        elif 'look right' in text:
            self.body_cmd('<LOOK:R>'); self.publish_head(150.0)
        elif 'look forward' in text or 'look center' in text:
            self.body_cmd('<LOOK:C>'); self.publish_head(90.0)

        # ── HANDS ──
        elif 'hands up' in text:
            self.speak_async('Hands up.'); self.body_cmd('<HANDS:90,90>'); self.publish_hands(90.0, 90.0)
        elif 'hands down' in text:
            self.body_cmd('<HANDS:0,0>'); self.publish_hands(0.0, 0.0)

        elif 'reset' in text or 'center' in text: self.body_cmd('<CENTER>'); self.speak_async('Reset.')

    def handle_room_navigation(self, text):
        triggers = ['go to','navigate to','drive to','take me to']
        room_name = text
        for trigger in triggers:
            if trigger in text:
                room_name = text.split(trigger)[-1].strip()
                break
                
        matched = None
        for name in self.rooms:
            if name in room_name or room_name in name:
                matched = name
                break

        if not matched:
            available = ', '.join(self.rooms.keys())
            self.speak_async(f'I do not know that location. Available rooms are: {available}.')
            return

        x, y, yaw = self.rooms[matched]
        self.get_logger().info(f'Navigating to {matched} ({x}, {y})')
        self.speak_async(f'Navigating to {matched}.')

        # Publish exactly what Nav2 needs to move the robot
        goal = PoseStamped()
        goal.header.frame_id    = 'map'
        goal.header.stamp       = self.get_clock().now().to_msg()
        goal.pose.position.x    = float(x)
        goal.pose.position.y    = float(y)
        goal.pose.orientation.z = math.sin(yaw / 2.0)
        goal.pose.orientation.w = math.cos(yaw / 2.0)

        self.goal_pub.publish(goal)

    def handle_ai_question(self, text):
        self.body_cmd('<EBLINK>')
        def ask_and_speak():
            answer = self.ai.ask(text)
            self.body_cmd('<NOD>')
            self.speak(answer)
        threading.Thread(target=ask_and_speak, daemon=True).start()

    def body_cmd(self, cmd):
        msg = String(); msg.data = cmd; self.body_pub.publish(msg)

    # Publish the intended head pose [pan_deg] so other nodes / the web UI can
    # reflect head state (previously this publisher was created but never used).
    def publish_head(self, pan_deg):
        msg = Float32MultiArray(); msg.data = [float(pan_deg)]; self.head_pub.publish(msg)

    # Publish the intended hand poses [left_deg, right_deg].
    def publish_hands(self, left_deg, right_deg):
        msg = Float32MultiArray(); msg.data = [float(left_deg), float(right_deg)]; self.hands_pub.publish(msg)

    # ── Runtime-saved locations helpers ───────────────────────────────────────
    def _load_user_locations(self, path):
        """Load user-saved locations from YAML. Returns {} if missing/unreadable."""
        try:
            if os.path.exists(path):
                with open(path, 'r') as f:
                    data = yaml.safe_load(f) or {}
                locs = data.get('locations', {}) or {}
                parsed = {str(name): tuple(float(v) for v in coords)
                          for name, coords in locs.items()}
                return parsed
        except Exception as e:
            self.get_logger().warn(f'Could not load user locations {path}: {e}')
        return {}

    def _on_saved_locations(self, msg: String):
        """Handle latched /saved_locations updates from location_manager (or any publisher)."""
        try:
            payload = json.loads(msg.data)
            locs = payload.get('locations', {}) or {}
            user_locs = {str(name): tuple(float(v) for v in coords)
                         for name, coords in locs.items()}
            # Overlay user locations on top of static rooms so runtime saves win.
            self.rooms = {**self.static_rooms, **user_locs}
            self.get_logger().info(f'Updated rooms from /saved_locations: {len(self.rooms)} total')
        except Exception as e:
            self.get_logger().warn(f'Failed to parse /saved_locations: {e}')


def main(args=None):
    rclpy.init(args=args)
    node = VoiceNode()
    try: rclpy.spin(node)
    except KeyboardInterrupt: pass
    finally:
        node.listening = False
        node.body_cmd('<0,0>') # Send stop to Arduino on exit
        node.destroy_node()
        rclpy.shutdown()

if __name__ == '__main__':
    main()
