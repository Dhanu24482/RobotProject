#!/usr/bin/env python3
"""
Varys Voice Node — Nav2 Goal + Arduino Mega Sync
TTS Priority: gTTS → Engineered Local Male Fallback
"""
import rclpy
from rclpy.node import Node
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

# ════════════════════════════════════════════════════════════
#  ROOM COORDINATES
# ════════════════════════════════════════════════════════════
ROOMS = {
    'room 1':           (0.703,  -0.069,  1.75  ),
    'room 2':           (-0.127, -0.235,  3.09  ),
    'room 3':           (-0.198, -1.068,  1.83  ),
    'room 4':           (-2.0,    1.5,    0.0   ),
    'reception':        (-1.0,    0.5,    1.57  ),
    'lobby':            ( 0.5,   -2.0,    3.14  ),
    'home':             ( 0.0,    0.0,    0.0   ),
}

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
        except: pass
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
        except Exception:
            try: subprocess.run(['espeak','-v','en+m3','-s','125','-p','32','-a','200',text], capture_output=True)
            except: pass

# ════════════════════════════════════════════════════════════
#  AI CLIENT
# ════════════════════════════════════════════════════════════
class AIClient:
    def __init__(self, logger):
        self.logger = logger
        self.ready  = False
        self.client = None
        self.chat   = None
        self.setup()

    def setup(self):
        api_key = os.environ.get('GEMINI_API_KEY','')
        if not api_key: return
        try:
            from google import genai
            from google.genai import types
            self.client = genai.Client(api_key=api_key)
            config = types.GenerateContentConfig(
                system_instruction=(
                    "You are Varys, an advanced humanoid reception robot. "
                    "Keep answers SHORT — maximum 2 sentences. "
                    "Never say you are an AI. You ARE Varys the robot."
                )
            )
            self.chat = self.client.chats.create(model='gemini-2.5-flash', config=config)
            self.ready = True
        except Exception as e: self.logger.error(f'AI setup failed: {e}')

    def ask(self, question):
        if not self.ready: return "My AI module is unavailable right now."
        try:
            response = self.chat.send_message(question)
            return response.text.strip().replace('*','').replace('#','').replace('`','')
        except: return "I am sorry, I could not process that question."

# ════════════════════════════════════════════════════════════
#  VOICE NODE
# ════════════════════════════════════════════════════════════
class VoiceNode(Node):
    def __init__(self):
        super().__init__('voice_node')

        self.voice_pub   = self.create_publisher(String,            '/robot/voice/command', 10)
        self.body_pub    = self.create_publisher(String,            '/robot/body/command',  10)
        self.head_pub    = self.create_publisher(Float32MultiArray, '/robot/head/pose',     10)
        self.hands_pub   = self.create_publisher(Float32MultiArray, '/robot/hands/pose',    10)
        
        # This publishes directly to ROS2 Nav2 stack
        self.goal_pub    = self.create_publisher(PoseStamped,       '/goal_pose',           10)

        self.tts = TTSEngine(self.get_logger())
        self.ai  = AIClient(self.get_logger())

        self.recognizer = sr.Recognizer()
        self.recognizer.energy_threshold         = 300
        self.recognizer.pause_threshold          = 0.8
        self.recognizer.dynamic_energy_threshold = True
        self.listening   = True
        self.is_speaking = False

        # ADDED 'go to', 'navigate to', 'take me to' back into hardcoded list
        self.robot_commands = [
            'go to', 'navigate to', 'drive to', 'take me to', 'go home',
            'go forward', 'move forward', 'go ahead', 'go backward', 'go back', 'move back', 'reverse',
            'turn left', 'rotate left', 'turn right', 'rotate right', 'stop', 'halt', 'emergency stop',
            'look left', 'look right', 'look forward', 'look center', 'nod', 'shake', 'blink', 'wink',
            'wave', 'wave left', 'wave right', 'hands up', 'hands down', 'reset', 'center'
        ]

        try:
            with sr.Microphone() as source: self.recognizer.adjust_for_ambient_noise(source, duration=2)
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
                with sr.Microphone() as source: audio = self.recognizer.listen(source, timeout=5, phrase_time_limit=10)
                text = self.recognizer.recognize_google(audio).lower().strip()
                self.get_logger().info(f'Heard: "{text}"')
                msg = String(); msg.data = text; self.voice_pub.publish(msg)
                self.route(text)
            except sr.WaitTimeoutError: pass
            except sr.UnknownValueError: pass
            except Exception: time.sleep(1)

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
            self.speak_async('Moving forward.')
            self.body_cmd('<20,20>')

        elif any(w in text for w in ['go backward','go back','move back','reverse']):
            self.speak_async('Moving backward.')
            self.body_cmd('<-20,-20>')

        elif any(w in text for w in ['turn left','rotate left']):
            self.speak_async('Turning left.')
            self.body_cmd('<-20,20>')

        elif any(w in text for w in ['turn right','rotate right']):
            self.speak_async('Turning right.')
            self.body_cmd('<20,-20>')

        elif any(w in text for w in ['stop','halt','emergency stop']):
            self.speak_async('Stopped.')
            self.body_cmd('<0,0>')

        # ── ANIMATIONS (Arduino Expects <COMMAND>) ──
        elif 'nod'   in text: self.body_cmd('<NOD>');   self.speak_async('Yes!')
        elif 'shake' in text: self.body_cmd('<SHAKE>'); self.speak_async('No!')
        elif 'blink' in text or 'wink' in text: self.body_cmd('<EBLINK>')
        elif 'wave left'  in text: self.body_cmd('<WAVE:L>'); self.speak_async('Hello!')
        elif 'wave'       in text: self.body_cmd('<WAVE:R>'); self.speak_async('Hello there!')
        elif 'reset' in text or 'center' in text: self.body_cmd('<CENTER>'); self.speak_async('Reset.')

    def handle_room_navigation(self, text):
        triggers = ['go to','navigate to','drive to','take me to']
        room_name = text
        for trigger in triggers:
            if trigger in text:
                room_name = text.split(trigger)[-1].strip()
                break
                
        matched = None
        for name in ROOMS:
            if name in room_name or room_name in name:
                matched = name
                break

        if not matched:
            available = ', '.join(ROOMS.keys())
            self.speak_async(f'I do not know that location. Available rooms are: {available}.')
            return

        x, y, yaw = ROOMS[matched]
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
