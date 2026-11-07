#!/usr/bin/env python3
"""
Lumi Voice Node — Nav2 Goal + Arduino Mega Sync
TTS Priority: gTTS → Engineered Local Male Fallback
"""
import rclpy
from rclpy.node import Node
from ament_index_python.packages import get_package_share_directory
from std_msgs.msg import String
from std_msgs.msg import Float32MultiArray
from geometry_msgs.msg import Twist
from geometry_msgs.msg import PoseStamped
from action_msgs.msg import GoalStatusArray
import speech_recognition as sr
from omni_base import canvas_spec
from omni_base.motion_stop import Halter
import subprocess
import threading
import tempfile
import time
import os
import math
import yaml
import json
import re
from ctypes import CFUNCTYPE, c_char_p, c_int, cdll

_alsa_handler = None  # module-level ref prevents garbage collection

def _silence_alsa_errors():
    """Install a no-op ALSA error handler so libasound stops spamming stderr."""
    try:
        global _alsa_handler
        ERROR_HANDLER = CFUNCTYPE(None, c_char_p, c_int, c_char_p, c_int, c_char_p)
        _alsa_handler = ERROR_HANDLER(lambda *a: None)
        cdll.LoadLibrary('libasound.so.2').snd_lib_error_set_handler(_alsa_handler)
    except Exception:
        pass  # best-effort; never block startup on logging cleanup

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
    """Gemini Q&A with automatic key AND model fallback.

    Free-tier quotas are counted per Google project per MODEL, so when every key is spent on the first model the same
    keys usually still work on the next one. Pairs are tried in this order: all keys on model 1, then all keys on
    model 2, ... and the best usable pair is always preferred again as soon as it is available (a daily quota resets).
    """
    SYSTEM_INSTRUCTION = (
        "You are Lumi, an advanced humanoid reception robot. Never say you are an AI. You ARE Lumi the robot.\n"
        "Always reply with ONE JSON object and nothing else: {\"say\": \"...\", \"canvas\": null}\n"
        "- say: what you speak aloud. MAX 2 SHORT sentences in plain words (it is read out loud: no markdown, no emoji).\n"
        "- canvas: null for almost every answer. Add a small picture ONLY when seeing it clearly helps the person: "
        "maths and geometry, formulas, shapes, simple diagrams or how-something-works sketches. Chit-chat, facts, "
        "directions, yes/no, opinions and anything short stay voice-only: canvas null.\n"
        "Canvas format: {\"title\": \"short title\", \"seconds\": 25, \"shapes\": [...], \"formula\": \"C = 2\u03c0r\", "
        "\"notes\": [\"short line\"]}\n"
        "- seconds: how long the picture stays on screen before it fades (8 to 90). Choose by how much there is to take in: "
        "one formula 10-15, a diagram with a couple of notes 25-40, several steps 45-90.\n"
        "- The drawing area is 100 wide by 100 high; (0,0) is the top-left; every number is 0-100 (degrees for arcs).\n"
        "- shapes (at most 14), each {\"kind\": ...}: circle {x,y,r}; line {x,y,x2,y2}; arrow {x,y,x2,y2}; rect {x,y,w,h}; "
        "arc {x,y,r,start,end} (degrees, 0 = right, clockwise); dot {x,y}; poly {points:[x1,y1,x2,y2,...]} (closed, max 8 "
        "points); text {x,y,text,size:\"small\"|\"normal\"|\"large\"} (text max 12 characters; Greek and maths symbols are fine: "
        "\u03c0 \u03b8 \u03b1 \u03b2 \u0394 \u221a \u00b2 \u00b3 \u2248 \u00d7 \u00f7 \u00b0 \u2220). Optional on any shape: "
        "\"color\": \"accent\"|\"green\"|\"yellow\"|\"red\"|\"white\", \"dashed\": true, \"fill\": true (circle, rect, poly).\n"
        "- formula: one short line (max 40 characters) shown big under the picture. notes: at most 3 short lines (max 60 "
        "characters each).\n"
        "- Keep it simple and uncluttered. Label the important parts with short text shapes placed BESIDE the lines, not on them.\n"
        "Example. User: How do I calculate the circumference of a circle?\n"
        "{\"say\": \"Multiply the radius by two and then by pi.\", \"canvas\": {\"title\": \"Circumference of a circle\", "
        "\"seconds\": 30, \"shapes\": [{\"kind\": \"circle\", \"x\": 50, \"y\": 52, \"r\": 34, \"color\": \"accent\", \"fill\": true}, "
        "{\"kind\": \"arc\", \"x\": 50, \"y\": 52, \"r\": 34, \"start\": 200, \"end\": 340, \"color\": \"green\"}, "
        "{\"kind\": \"dot\", \"x\": 50, \"y\": 52}, {\"kind\": \"arrow\", \"x\": 50, \"y\": 52, \"x2\": 84, \"y2\": 52, "
        "\"color\": \"yellow\"}, {\"kind\": \"text\", \"x\": 68, \"y\": 45, \"text\": \"r\", \"size\": \"large\", "
        "\"color\": \"yellow\"}, {\"kind\": \"text\", \"x\": 50, \"y\": 9, \"text\": \"C\", \"size\": \"large\", "
        "\"color\": \"green\"}], \"formula\": \"C = 2\u03c0r\", \"notes\": [\"r is the radius\", \"\u03c0 \u2248 3.14159\", "
        "\"r = 5 gives C \u2248 31.4\"]}}\n"
        "Example. User: What are your opening hours?\n"
        "{\"say\": \"I do not have that information, but the reception desk can help you.\", \"canvas\": null}"
    )
    # In priority order; override with GEMINI_MODELS="a,b,c" (panel: Settings > Environment variables).
    # Keep in step with config.GEMINI_DEFAULT_MODELS in the control panel.
    DEFAULT_MODELS = ('gemini-2.5-flash', 'gemini-3-flash-preview', 'gemini-3.1-flash-lite-preview')

    # How long to skip a (key, model) pair after something goes wrong (see _classify):
    KEY_COOLDOWN_SEC = 60.0          # rate-limited and Google gave no delay: first time; doubles per consecutive failure
    KEY_COOLDOWN_MAX = 3600.0        #   ...and a daily quota waits this long between looks (it resets by itself)
    KEY_REJECTED_SEC = 6 * 3600.0    # Google rejects the key (invalid / forbidden): leave it alone for hours, on every model
    MODEL_GONE_SEC = 6 * 3600.0      # the model does not exist / is not offered to us: skip it for every key
    BUSY_COOLDOWN_SEC = 15.0         # Google's MODEL is overloaded (the key is fine): back off briefly
    DAILY_PROBES = 3                 # keys from one Google project share a daily quota, so after this many "daily quota used
    MODEL_RECHECK_SEC = 300.0        #   up" answers in a row the model is paused this long (each probe costs ~1 s) and the
                                     #   next model answers at once; the probing then continues with the NEXT keys
    REQUEST_TIMEOUT_SEC = 40.0       # give up on one request after this long and treat it as "busy"
    MAX_OUTPUT_TOKENS = 1600         # room for a spoken sentence plus a small picture, never an essay
    BUSY_TRIES = 2                   #   ...and give up politely after this many, instead of cycling through everything

    def __init__(self, logger):
        self.logger      = logger
        self.keys        = self._load_keys()
        self.models      = self._load_models()
        self.cooldowns   = {}        # (key, model) -> epoch time it becomes usable again
        self.fail_streak = {}        # (key, model) -> consecutive rate-limit failures (drives the growing cooldown)
        self.model_gone  = {}        # model -> epoch time we look at it again
        self.idx         = {m: 0 for m in self.models}      # per model: where the round-robin over keys continues
        self.client      = None
        self.chat        = None
        self.active_key  = None
        self.active_model = None
        self.ready       = False
        if self.keys:
            self._activate()
        else:
            self.logger.warn('No Gemini API key found (GEMINI_API_KEYS / GEMINI_API_KEY).')

    def _load_keys(self):
        # Prefer the comma-separated rotating list, fall back to the single key.
        raw = os.environ.get('GEMINI_API_KEYS', '') or os.environ.get('GEMINI_API_KEY', '')
        return [k.strip() for k in raw.split(',') if k.strip()]

    def _load_models(self):
        raw = os.environ.get('GEMINI_MODELS', '')
        models = [m.strip() for m in raw.split(',') if m.strip()] or list(self.DEFAULT_MODELS)
        return list(dict.fromkeys(models))               # keep the order, drop duplicates

    def _usable(self, key, model, now):
        return self.cooldowns.get((key, model), 0) <= now and self.model_gone.get(model, 0) <= now

    def _activate(self):
        """Switch to the best usable (key, model) pair and build a fresh chat for it. False if there is none."""
        now = time.time()
        n = len(self.keys)
        for model in self.models:
            if self.model_gone.get(model, 0) > now:
                continue
            start = self.idx[model]
            for step in range(n):
                key = self.keys[(start + step) % n]
                if self.cooldowns.get((key, model), 0) > now:
                    continue
                if self._build_client(key, model):
                    self.idx[model] = (start + step + 1) % n
                    self.active_key, self.active_model, self.ready = key, model, True
                    return True
        self.ready = False
        return False

    def _prefer_best(self):
        """Go back to a higher-priority model as soon as one of its keys is usable again (a daily quota resets)."""
        now = time.time()
        for model in self.models:
            if model == self.active_model:
                return                                   # nothing better than what we are on
            if self.model_gone.get(model, 0) > now:
                continue
            if any(self.cooldowns.get((k, model), 0) <= now for k in self.keys):
                self._activate()
                return

    def _build_client(self, key, model):
        try:
            from google import genai
            from google.genai import types
            # A hard time limit: a stuck request must never freeze the robot's voice (it is classified as "busy").
            self.client = genai.Client(api_key=key, http_options=types.HttpOptions(timeout=int(self.REQUEST_TIMEOUT_SEC * 1000)))
            kw = {'system_instruction': self.SYSTEM_INSTRUCTION, 'response_mime_type': 'application/json',
                  'max_output_tokens': self.MAX_OUTPUT_TOKENS}
            # A spoken answer must not wait for the model to ponder: thinking is what made picture answers take 20+ s.
            if model.startswith('gemini-3'):
                kw['thinking_config'] = types.ThinkingConfig(thinking_level='low')
            else:
                kw['thinking_config'] = types.ThinkingConfig(thinking_budget=0)
            self.chat = self.client.chats.create(model=model, config=types.GenerateContentConfig(**kw))
            return True
        except Exception as e:
            self.logger.error(f'AI setup failed for {model}: {e}')
            return False

    @staticmethod
    def _classify(err):
        """What went wrong, in terms of what to do about it:
        'rate'     this key is out of quota / rate-limited on this model -> skip the pair for a while
        'rejected' Google refuses this key (invalid/forbidden)           -> skip the key for hours, on every model
        'gone'     this model is not available (404 / retired)           -> skip the model for hours, for every key
        'busy'     Google's model is overloaded / unreachable            -> brief back-off (another key will not help much)
        'other'    anything else                                         -> a real error in the question, do not rotate
        Status codes are matched as whole numbers: a retry delay such as 85401s must not read as an HTTP 401.
        """
        s = str(err).lower()
        if re.search(r'\b40[13]\b', s) or any(w in s for w in ('api key not valid', 'api_key_invalid', 'permission_denied',
                                                               'unauthenticated', 'api key expired')):
            return 'rejected'
        if re.search(r'\b429\b', s) or any(w in s for w in ('resource_exhausted', 'resourceexhausted', 'quota', 'rate limit',
                                                            'rate_limit')):
            return 'rate'
        if re.search(r'\b404\b', s) or any(w in s for w in ('not_found', 'no longer available', 'is not found')):
            return 'gone'
        if re.search(r'\b50[0234]\b', s) or any(w in s for w in ('unavailable', 'overloaded', 'high demand', 'internal',
                                                                 'deadline', 'timed out', 'timeout', 'connection', 'network')):
            return 'busy'
        return 'other'

    @staticmethod
    def _is_daily(err):
        return 'perday' in str(err).lower().replace('_', '')

    def _rate_cooldown(self, err, streak):
        """How long to leave a rate-limited pair alone. Google says: a per-minute limit comes with a retry delay (use it),
        a daily one means "wait" (an hour between looks is plenty, it resets by itself). Otherwise back off 1, 2, 4... min."""
        s = str(err).lower().replace('_', '')
        if self._is_daily(err):
            return self.KEY_COOLDOWN_MAX
        m = re.search(r'retrydelay\W+(\d+(?:\.\d+)?)s', s)
        if m:
            return min(max(float(m.group(1)) + 2.0, 5.0), 300.0)
        return min(self.KEY_COOLDOWN_MAX, self.KEY_COOLDOWN_SEC * (2 ** (streak - 1)))

    def _label(self, key, model=None):
        """A name for a key that is safe to log: its position in the list (and the model), never the key itself."""
        n = f'#{self.keys.index(key) + 1}' if key in self.keys else '?'
        return f'{n}/{model}' if model else n

    def _ask_raw(self, question):
        """Returns (text, answered): the model's raw reply, or a ready-made apology with answered=False."""
        if not self.keys:
            return "My AI module is unavailable right now.", False
        if self.ready:
            self._prefer_best()
        elif not self._activate():                       # everything was cooling down: look again, a cooldown may be over
            return "All my AI keys are busy right now. Please try again shortly.", False
        busy_msg = "My AI service is busy right now. Please try again in a moment."
        busy_seen = {}                                   # model -> how many "overloaded" answers it gave this question
        daily_seen = {}                                  # model -> how many "daily quota used up" answers this question
        # At most one try per (key, model) pair; rotate past any that cannot answer right now.
        for _ in range(max(1, len(self.keys) * len(self.models))):
            key, model = self.active_key, self.active_model
            try:
                response = self.chat.send_message(question)
                self.fail_streak[(key, model)] = 0
                return response.text or '', True
            except Exception as e:
                kind = self._classify(e)
                if kind == 'other':
                    self.logger.error(f'AI error: {e}')
                    return "I am sorry, I could not process that question.", False
                now = time.time()
                if kind == 'rate':
                    n = self.fail_streak.get((key, model), 0) + 1
                    self.fail_streak[(key, model)] = n
                    cooldown = self._rate_cooldown(e, n)
                    self.cooldowns[(key, model)] = now + cooldown
                    self.logger.warn(f'Gemini {self._label(key, model)} is rate-limited - skipping it for {cooldown:.0f}s.')
                    if self._is_daily(e):
                        daily_seen[model] = daily_seen.get(model, 0) + 1
                        if daily_seen[model] >= self.DAILY_PROBES and len(self.models) > 1:
                            self.model_gone[model] = now + self.MODEL_RECHECK_SEC
                            self.logger.warn(f'Gemini {model}: daily quota is used up on {daily_seen[model]} keys in a row - '
                                             f'using the next model, and looking at it again in {self.MODEL_RECHECK_SEC:.0f}s.')
                elif kind == 'rejected':
                    for m in self.models:
                        self.cooldowns[(key, m)] = now + self.KEY_REJECTED_SEC
                    self.logger.warn(f'Gemini key {self._label(key)} was rejected by Google - skipping it for hours. '
                                     f'Check it in the control panel (Settings > Gemini API keys).')
                elif kind == 'gone':
                    self.model_gone[model] = now + self.MODEL_GONE_SEC
                    self.logger.warn(f'Gemini model {model} is not available - using the next model instead.')
                else:
                    busy_seen[model] = busy_seen.get(model, 0) + 1
                    self.cooldowns[(key, model)] = now + self.BUSY_COOLDOWN_SEC
                    if busy_seen[model] >= self.BUSY_TRIES:
                        # The MODEL is overloaded (the keys are fine): stop asking it, go on to the next model.
                        for k in self.keys:
                            self.cooldowns[(k, model)] = max(self.cooldowns.get((k, model), 0), now + self.BUSY_COOLDOWN_SEC)
                    self.logger.warn(f'Gemini is busy ({self._label(key, model)}): {str(e)[:120]}')
                if not self._activate():
                    if kind == 'rejected':
                        return "None of my AI keys are working right now. Please check them in the control panel.", False
                    if kind == 'busy':
                        return busy_msg, False
                    return "All my AI keys are busy right now. Please try again shortly.", False
        return busy_msg, False

    def ask_rich(self, question):
        """(what to say aloud, picture spec or None). The picture is already sanitised (see canvas_spec)."""
        text, answered = self._ask_raw(question)
        if not answered:
            return text, None
        say, canvas = canvas_spec.parse_ai_json(text)
        say = say.replace('*', '').replace('#', '').replace('`', '').strip()
        return (say or "I am not sure how to answer that."), canvas

    def ask(self, question):
        return self.ask_rich(question)[0]

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
            os.path.join(os.path.expanduser('~'), '.ros', 'lumi_saved_locations.yaml')
        ).value
        # Kept for backward compatibility; drive commands now use the bridge's pulses,
        # which follow the live /robot/speed setting.
        self.drive_pwm     = self.declare_parameter('drive_pwm', 20).value
        # ── Robot name (wake-word trigger) — change here to rename the robot ──
        self.robot_name    = self.declare_parameter('robot_name', 'lumi').value.lower().strip()
        # Google STT rarely returns "lumi" cleanly — it clips or mangles the name
        # into things like "l", "lu", "lum", "lomi". All of these count as the
        # wake word; the greeting prefix keeps them from firing on stray speech.
        name_aliases       = self.declare_parameter('name_aliases', [
            'lumi', 'lumy', 'lumie', 'lumee', 'loomi', 'loomy', 'lomi', 'lomy',
            'lume', 'lum', 'lu', 'l', 'louie', 'lewis', 'looney', 'roomy',
        ]).value
        # Longest first so "lumi" wins over "lu" and the remainder is correct.
        self._wake_names   = sorted(
            {self.robot_name, *(a.lower().strip() for a in name_aliases if a.strip())},
            key=len, reverse=True,
        )
        voice_topic        = self.declare_parameter('voice_topic',   '/robot/voice/command').value
        body_topic         = self.declare_parameter('body_topic',   '/robot/body/command').value
        head_topic         = self.declare_parameter('head_topic',   '/robot/head/pose').value
        hands_topic        = self.declare_parameter('hands_topic',  '/robot/hands/pose').value
        goal_topic         = self.declare_parameter('goal_topic',   '/goal_pose').value
        emotion_topic      = self.declare_parameter('emotion_topic', '/robot/emotion').value
        # Browser-originated actions (transcribed AND parsed on the client, validated by
        # remote_node) arrive here as JSON; the robot's spoken replies are mirrored out.
        remote_topic       = self.declare_parameter('remote_topic', '/robot/voice/remote_action').value
        reply_topic        = self.declare_parameter('reply_topic',  '/robot/voice/reply').value
        # A small picture to show on the voice page next to a spoken answer (JSON, see canvas_spec). Only sent when useful.
        canvas_topic       = self.declare_parameter('canvas_topic', '/robot/voice/canvas').value
        self.enable_mic    = self.declare_parameter('enable_mic', True).value

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

        # Track navigation outcomes (SUCCEEDED / ABORTED / CANCELED).
        self._active_goal = None
        self._last_nav_status = None
        self.create_subscription(GoalStatusArray, '/navigate_to_pose/_action/status',
                                 self._on_nav_status, 10)

        self.voice_pub   = self.create_publisher(String,            voice_topic,   10)
        self.body_pub    = self.create_publisher(String,            body_topic,    10)
        self.head_pub    = self.create_publisher(Float32MultiArray, head_topic,    10)
        self.hands_pub   = self.create_publisher(Float32MultiArray, hands_topic,   10)
        self.emotion_pub = self.create_publisher(String,            emotion_topic, 10)
        self.reply_pub   = self.create_publisher(String,            reply_topic,   10)
        self.canvas_pub  = self.create_publisher(String,            canvas_topic,  10)
        # Stops motors, timed drive pulses AND any active Nav2 goal (see motion_stop.py).
        self.halter      = Halter(self, body_topic=body_topic)
        self.create_subscription(String, remote_topic, self._on_remote_action, 10)

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

        # Suppress libasound's verbose PCM-enumeration stderr before opening mic.
        _silence_alsa_errors()
        # Open the microphone once and reuse it (re-opening every loop iteration
        # is slow and can leak audio device handles). The mic is optional: if it
        # is disabled or fails to open, the node still serves remote (browser)
        # commands arriving on remote_topic.
        self.microphone = None
        if self.enable_mic:
            try:
                self.microphone = sr.Microphone()
            except Exception as e:
                self.get_logger().error(
                    f'Microphone unavailable ({e}); continuing with remote commands only.')

        # ADDED 'go to', 'navigate to', 'take me to' back into hardcoded list
        self.robot_commands = [
            'go to', 'navigate to', 'drive to', 'take me to', 'go home',
            'go forward', 'move forward', 'go ahead', 'go backward', 'go back', 'move back', 'reverse',
            'turn left', 'rotate left', 'turn right', 'rotate right', 'stop', 'halt', 'emergency stop',
            'look left', 'look right', 'look forward', 'look center', 'nod', 'shake', 'blink', 'wink',
            'wave', 'wave left', 'wave right', 'hands up', 'hands down',
            'salute', 'goodbye', 'good bye', 'bye', 'see you',
            'reset', 'center'
        ]

        if self.microphone is not None:
            try:
                with self.microphone as source: self.recognizer.adjust_for_ambient_noise(source, duration=2)
            except Exception as e: self.get_logger().error(f'Microphone error: {e}')

        time.sleep(1)
        self.set_emotion('happy')
        self.speak_async(f'{self.robot_name.capitalize()} online. Navigation and AI systems ready.')
        if self.microphone is not None:
            threading.Thread(target=self.listen_loop, daemon=True).start()

    def speak(self, text):
        self.is_speaking = True
        # Mirror everything the robot says so remote clients can show the reply.
        try:
            msg = String(); msg.data = text; self.reply_pub.publish(msg)
        except Exception: pass
        try: self.tts.speak(text)
        except Exception as e: self.get_logger().error(f'Speak error: {e}')
        finally: self.is_speaking = False

    def speak_async(self, text):
        threading.Thread(target=self.speak, args=(text,), daemon=True).start()

    def listen_loop(self):
        while self.listening:
            try:
                with self.microphone as source: audio = self.recognizer.listen(source, timeout=5, phrase_time_limit=10)
                text = self.recognizer.recognize_google(audio).lower().strip()
                # The mic stays open while the robot talks so "stop" always works (e.g.
                # right after a drive command, while "Moving forward." is still playing).
                # Anything else heard during our own speech is the speaker echoing: drop it.
                if self.is_speaking and not self._is_stop_phrase(text):
                    continue
                self.get_logger().info(f'Heard: "{text}"')
                msg = String(); msg.data = text; self.voice_pub.publish(msg)
                self.route(text)
            except sr.WaitTimeoutError: pass
            except sr.UnknownValueError: pass
            except Exception as e:
                self.get_logger().warn(f'Listen loop error: {e}')
                time.sleep(1)

    def _strip_wake_word(self, text):
        """Return the command after the wake word, or None if not addressed to the robot.

        Requires a greeting prefix followed by the robot's name or one of its
        known mis-transcriptions: "(hey|hi|hello) <name> <command>". The name
        must end on a word boundary so "hi lu" matches but "hi lucy" does not.
        Any leading punctuation or whitespace (e.g. the comma in "hi lumi, go to
        office") is stripped from the remainder.

        Returns:
            None   — the utterance is not addressed to the robot; ignore it.
            ''     — only the greeting and name were spoken; caller should acknowledge.
            str    — the command text with the wake word removed.
        """
        names = '|'.join(re.escape(n) for n in self._wake_names)
        pattern = r'^(?:hey|hi|hello)\s+(?:' + names + r')\b'
        m = re.match(pattern, text)
        if not m:
            return None
        remainder = text[m.end():].lstrip(' ,;:!?')
        return remainder

    def _on_remote_action(self, msg: String):
        """Execute an already-parsed action from remote_node (browser did the STT + parsing).

        Payload is JSON: {"type": "command", "text": "<canonical command>"} or
        {"type": "ask", "text": "<question>"}. Commands skip the wake word and the
        substring router and go straight to handle_robot_command(); questions go
        straight to the LLM so words like "stop" inside a question can't trigger motion.
        """
        try:
            data = json.loads(msg.data)
            kind, text = data['type'], str(data['text']).strip()[:200]
        except Exception as e:
            self.get_logger().warn(f'Bad remote action payload: {e}')
            return
        if not text:
            return
        self.get_logger().info(f'Remote {kind}: "{text if kind == "command" else "<question>"}"')
        if kind == 'command':
            self.handle_robot_command(text.lower())
        elif kind == 'ask':
            self.handle_ai_question(text)

    def route(self, text):
        if self._is_stop_phrase(text):
            self.emergency_stop()
            return
        command = self._strip_wake_word(text)
        if command is None:
            self.get_logger().debug(f'Ignored (no wake word): "{text}"')
            return
        if not command:
            self.set_emotion('surprised')
            self.speak_async('Yes?')
            return
        for cmd in self.robot_commands:
            if cmd in command:
                self.handle_robot_command(command)
                return
        self.handle_ai_question(command)

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
            self.set_emotion('neutral')
            self.speak_async('Moving forward.')
            self.body_cmd('FORWARD')       # bridge: 2 s pulse at the live drive speed

        elif any(w in text for w in ['go backward','go back','move back','reverse']):
            self.set_emotion('neutral')
            self.speak_async('Moving backward.')
            self.body_cmd('BACKWARD')

        elif any(w in text for w in ['turn left','rotate left']):
            self.set_emotion('neutral')
            self.speak_async('Turning left.')
            self.body_cmd('LEFT')

        elif any(w in text for w in ['turn right','rotate right']):
            self.set_emotion('neutral')
            self.speak_async('Turning right.')
            self.body_cmd('RIGHT')

        elif any(w in text for w in ['stop','halt','emergency stop']):
            self.emergency_stop()

        # ── ANIMATIONS (Arduino Expects <COMMAND>) ──
        elif 'nod'   in text:
            self.set_emotion('happy');   self.body_cmd('<NOD>');   self.speak_async('Yes!')
        elif 'shake' in text:
            self.set_emotion('angry');   self.body_cmd('<SHAKE>'); self.speak_async('No!')
        elif 'wink' in text:
            self.set_emotion('wink')       # one eye winks, then the face goes back to what it was
        elif 'blink' in text:
            self.set_emotion('blink')
        elif 'wave left'  in text:
            self.set_emotion('happy');   self.body_cmd('<WAVE:L>'); self.speak_async('Hello!')
        elif 'wave'       in text:
            self.set_emotion('happy');   self.body_cmd('<WAVE:R>'); self.speak_async('Hello there!')

        # ── HEAD LOOK (must precede 'center' so "look center" is not eaten by reset) ──
        elif 'look left'  in text:
            self.body_cmd('<LOOK:L>'); self.publish_head(30.0)
            self.speak_async('Looking left.')
        elif 'look right' in text:
            self.body_cmd('<LOOK:R>'); self.publish_head(150.0)
            self.speak_async('Looking right.')
        elif 'look forward' in text or 'look center' in text:
            self.body_cmd('<LOOK:C>'); self.publish_head(90.0)
            self.speak_async('Looking forward.')

        # ── ARMS / HANDS (6-DOF poses on Mega) ──
        # Goodbye / bye before generic greetings so "see you" is not missed.
        elif any(w in text for w in ['goodbye', 'good bye', 'see you', 'bye']):
            self.set_emotion('happy')
            self.speak_async('Goodbye! Have a nice day.')
            self.body_cmd('<GOODBYE>')  # salute → hold → home on Arduino
        elif 'salute' in text:
            self.set_emotion('happy')
            self.speak_async('Salute!')
            self.body_cmd('<SALUTE>')
            self.publish_hands(90.0, 180.0)
        # "pull up" / "pull down" are intentionally NOT handled: those poses drove the
        # shoulder roll and elbow to full travel and swung the arms into the torso.
        # Re-add only after the safe arm limits are measured (see firmware ARM_ROLL_*).
        elif 'hands up' in text:
            self.set_emotion('happy')
            self.speak_async('Hands up.')
            self.body_cmd('<HAND_UP>')
            self.publish_hands(180.0, 180.0)
        elif 'hands down' in text:
            self.set_emotion('neutral')
            self.speak_async('Hands down.')
            self.body_cmd('<HAND_DOWN>')
            self.publish_hands(0.0, 0.0)

        # ── WRISTS (hand flex only — servos on Mega pins 44/45) ──
        # Kept distinct from "hands up/down" above, which raises the whole arm.
        elif 'wrist up' in text:
            self.body_cmd('<WRIST:UP>');     self.speak_async('Wrist up.')
        elif 'wrist down' in text:
            self.body_cmd('<WRIST:DOWN>');   self.speak_async('Wrist down.')
        elif 'wrist' in text:
            self.body_cmd('<WRIST:CENTER>'); self.speak_async('Wrist centered.')

        elif 'reset' in text or 'center' in text:
            self.set_emotion('neutral'); self.body_cmd('<CENTER>'); self.speak_async('Reset.')

    _STOP_RE = re.compile(r'\b(?:stop|halt)\b')

    def _is_stop_phrase(self, text):
        """True for a short utterance containing stop/halt ("stop", "emergency stop", "please stop").

        Deliberately needs no wake word: when the robot is moving, shouting "stop" must work.
        Long sentences are excluded so questions like "what is a bus stop" still go to the LLM.
        """
        words = re.sub(r'[^\w\s]', ' ', text.lower()).split()
        return len(words) <= 4 and bool(self._STOP_RE.search(' '.join(words)))

    def emergency_stop(self):
        self._active_goal = None            # don't announce "arrived/blocked" for a goal we cancelled
        self.halter.halt()
        self.set_emotion('surprised')
        self.speak_async('Stopped.')

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
            self.set_emotion('sad')
            self.speak_async(f'I do not know that location. Available rooms are: {available}.')
            return

        x, y, yaw = self.rooms[matched]
        self.get_logger().info(f'Navigating to {matched} ({x}, {y})')
        self.set_emotion('neutral')
        self.speak_async(f'Navigating to {matched}.')

        # Remember destination so _on_nav_status can name it in the outcome message.
        self._active_goal = matched
        self._last_nav_status = None  # reset so next status transition fires

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
        self.set_emotion('surprised')
        def ask_and_speak():
            answer, canvas = self.ai.ask_rich(text)
            # Detect AI-unavailable / error responses by checking common phrases
            _error_phrases = ('unavailable', 'busy right now', 'could not process')
            if any(p in answer.lower() for p in _error_phrases):
                self.set_emotion('sad')
            else:
                self.set_emotion('happy')
            if canvas:
                self.publish_canvas(canvas)          # first, so the picture is already there while the answer is spoken
            self.speak(answer)
        threading.Thread(target=ask_and_speak, daemon=True).start()

    def publish_canvas(self, spec):
        try:
            msg = String(); msg.data = json.dumps(spec, ensure_ascii=False); self.canvas_pub.publish(msg)
        except Exception as e:
            self.get_logger().warn(f'Could not publish the picture: {e}')

    def body_cmd(self, cmd):
        msg = String(); msg.data = cmd; self.body_pub.publish(msg)

    def set_emotion(self, name):
        msg = String(); msg.data = name; self.emotion_pub.publish(msg)

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

    def _on_nav_status(self, msg: GoalStatusArray):
        """Log and speak navigation outcomes (SUCCEEDED / ABORTED / CANCELED)."""
        if not msg.status_list:
            return
        status = msg.status_list[-1].status  # status of the most-recent goal
        if status == self._last_nav_status:
            return  # only act on transitions, not repeated identical states
        self._last_nav_status = status
        target = self._active_goal or 'the target'
        if status == 4:    # SUCCEEDED
            self.get_logger().info(f'Arrived at {target}.')
            self.set_emotion('happy')
            self.speak_async(f'I have arrived at {target}.')
            self._active_goal = None
        elif status == 6:  # ABORTED — planner/controller gave up
            self.get_logger().error(
                f'Could not reach {target}: path blocked or pose unreachable.')
            self.set_emotion('sad')
            self.speak_async(f'I could not reach {target}. The path may be blocked.')
            self._active_goal = None
        elif status == 5:  # CANCELED
            self.get_logger().warn(f'Navigation to {target} was canceled.')
            self.set_emotion('neutral')
            self._active_goal = None


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
