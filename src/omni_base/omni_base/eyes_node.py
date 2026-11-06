#!/usr/bin/env python3
"""
Eyes Node — Dual ILI9341 TFT emotion display for Lumi robot.

Drives two 2.4" ILI9341 320x240 displays connected to the Raspberry Pi's
hardware SPI0 bus. Subscribes to /robot/emotion (std_msgs/String) and animates
the matching expression on both eyes (see eye_art.py for the drawing and the
animation rules).

Expressions: neutral happy sad angry surprised  |  love (heart eyes) excited (star eyes) dizzy
             sleepy wink  |  'blink' = one blink, keeping the current expression.
Changing expression is a quick blink with a colour fade; the eyes also blink on their own every
few seconds. Only the round area visible through the face holes (180x180 centred) is ever drawn
or sent to the panels.

Hardware (shared SPI bus):
  VCC  -> Pin 1  (3.3V)          GND   -> Pin 6 (GND)
  SCK  -> Pin 23 (GPIO 11/SCLK)  MOSI  -> Pin 19 (GPIO 10/MOSI)
  D/C  -> Pin 18 (GPIO 24)       RESET -> Pin 22 (GPIO 25)
  LED  -> Pin 2 or 4 (5V)
  CS Left  -> Pin 29 (GPIO 5)    [left eye]
  CS Right -> Pin 31 (GPIO 6)    [right eye]

The chip-selects deliberately avoid the hardware CE0/CE1 pins. board.SPI()
opens /dev/spidev0.0, so the kernel drives CE0 (GPIO 8) low on every transfer
regardless of which CS the driver toggles. With a panel wired to CE0 that panel
is selected during the other panel's writes and both show the same frame.
Plain GPIOs are untouched by the kernel, so each panel is selected on its own.

Import-guarded: on non-Pi systems (e.g. Windows dev box, SPI disabled) the
node starts headless — it still subscribes and logs emotions, so the ROS graph
never breaks.
"""
import threading
import time
from collections import deque

import rclpy
from rclpy.node import Node
from std_msgs.msg import String

# ── Hardware imports (Pi-only) ────────────────────────────────────────────────
import os as _os
_os.environ.setdefault('BLINKA_RASPBERRY_PI', '1')  # needed on Ubuntu Pi (not Raspberry Pi OS)

_HW_AVAILABLE = False
_HW_IMPORT_ERROR = None
try:
    import board
    import digitalio
    import adafruit_rgb_display.ili9341 as ili9341
    from PIL import Image
    from omni_base import eye_art as art
    _HW_AVAILABLE = True
except Exception as _e:
    _HW_IMPORT_ERROR = str(_e)  # captured for logging in __init__

# ── Constants ────────────────────────────────────────────────────────────────
BAUDRATE  = 64000000
ROTATION  = 90

# Display dimensions in landscape (rotation=90)
WIDTH  = 320
HEIGHT = 240
# The round hole: a 180x180 square centred on the panel (landscape coordinates).
HOLE_X, HOLE_Y = 70, 30
# With rotation=90 the driver rotates every picture a quarter turn into the panel's native portrait
# frame, so the same square sits at native (HOLE_Y, WIDTH - HOLE_X - SIZE) - verified pixel-for-pixel
# in test/test_eye_art.py. Sending only that square is ~2.4x less SPI traffic than a full frame.
NATIVE_X, NATIVE_Y = 30, 70


class EyesNode(Node):
    def __init__(self):
        super().__init__('eyes_node')

        emotion_topic = self.declare_parameter('emotion_topic', '/robot/emotion').value
        self._rotation  = self.declare_parameter('rotation',  ROTATION).value
        self._baudrate  = self.declare_parameter('baudrate',  BAUDRATE).value
        idle_blink      = self.declare_parameter('idle_blink',  True).value     # blink on its own every few seconds
        transitions     = self.declare_parameter('transitions', True).value     # blink + colour fade when changing
        self._fps       = float(self.declare_parameter('fps', 15.0).value)      # while something is moving
        self._crop      = bool(self.declare_parameter('send_hole_only', True).value)

        self._left_eye  = None
        self._right_eye = None
        self._hw        = False
        self._lock      = threading.Lock()
        self._wake      = threading.Event()
        self._running   = True
        self._thread    = None
        self._push_ms   = deque(maxlen=60)
        self._last_err  = 0.0
        self._last_stat = 0.0

        if _HW_AVAILABLE:
            self._init_displays()
        else:
            self.get_logger().warn(
                f'eyes_node: hardware libraries unavailable — {_HW_IMPORT_ERROR}. '
                'Running headless — emotions will be logged only.'
            )

        self._anim = art.EyeAnimator(now=time.monotonic(), emotion='neutral',
                                     idle_blink=bool(idle_blink), transitions=bool(transitions)) if _HW_AVAILABLE else None

        self.create_subscription(String, emotion_topic, self._on_emotion, 10)

        if self._hw:
            self._clear()      # whole panel black once; afterwards only the hole is ever redrawn
            self._thread = threading.Thread(target=self._loop, daemon=True, name='eyes-anim')
            self._thread.start()
        self.get_logger().info(f'eyes_node ready, subscribed to {emotion_topic}')

    # ── Hardware init ─────────────────────────────────────────────────────────

    def _init_displays(self):
        try:
            cs_left  = digitalio.DigitalInOut(board.D5)    # GPIO 5, Pin 29
            cs_right = digitalio.DigitalInOut(board.D6)    # GPIO 6, Pin 31
            dc       = digitalio.DigitalInOut(board.D24)
            spi      = board.SPI()

            # RESET is shared by both panels, so it is pulsed once here and the
            # drivers are built with rst=None. Handing the same reset pin to both
            # constructors would let the second one reset the already-configured
            # first panel, leaving that eye blank.
            self._reset = digitalio.DigitalInOut(board.D25)
            self._reset.direction = digitalio.Direction.OUTPUT
            self._reset.value = True
            time.sleep(0.05)
            self._reset.value = False
            time.sleep(0.05)
            self._reset.value = True
            time.sleep(0.15)

            self._left_eye = ili9341.ILI9341(
                spi, rotation=self._rotation,
                cs=cs_left, dc=dc, rst=None, baudrate=self._baudrate
            )
            self._right_eye = ili9341.ILI9341(
                spi, rotation=self._rotation,
                cs=cs_right, dc=dc, rst=None, baudrate=self._baudrate
            )
            self._hw = True
            self.get_logger().info('eyes_node: ILI9341 displays initialized.')
        except Exception as e:
            self.get_logger().error(f'eyes_node: display init failed: {e}. Running headless.')

    # ── ROS callback ─────────────────────────────────────────────────────────

    def _on_emotion(self, msg: String):
        name = msg.data.strip().lower()
        self.get_logger().info(f'eyes_node: emotion -> {name}')
        if not self._hw:
            return  # headless mode — no display hardware available
        now = time.monotonic()
        with self._lock:
            if name == 'blink':
                self._anim.blink(now)
            elif art.canonical(name) is None:
                self.get_logger().warn(f'eyes_node: unknown emotion "{name}", ignoring.')
            else:
                self._anim.request(name, now)
        self._wake.set()

    # ── animation loop (the only thread that ever touches the SPI bus) ─────────────────────────────────

    def _loop(self):
        period = 1.0 / max(1.0, self._fps)
        while self._running:
            t0 = time.monotonic()
            with self._lock:
                frame = self._anim.frame(t0)
                busy = self._anim.busy()
                idle_for = self._anim.seconds_until_idle_blink(t0)
            if frame.dirty:
                try:
                    self._draw(frame)
                except Exception as e:                       # an SPI hiccup must not kill the eyes
                    if t0 - self._last_err > 5.0:
                        self._last_err = t0
                        self.get_logger().warn(f'eyes_node: display update failed: {e}')
            if busy:
                time.sleep(max(0.0, period - (time.monotonic() - t0)))
            else:
                self._wake.wait(timeout=min(0.5, idle_for))   # nothing moving: sleep until asked or time to blink
                self._wake.clear()

    def _draw(self, f):
        sides = ('L', 'R') if f.emotion in art.ASYMMETRIC else ('L',)
        imgs = {s: art.render(f.emotion, s, f.color, f.openness, f.t) for s in sides}
        t0 = time.monotonic()
        self._push(imgs['L'], imgs.get('R', imgs['L']))
        self._push_ms.append((time.monotonic() - t0) * 1000.0)
        if len(self._push_ms) == self._push_ms.maxlen and time.monotonic() - self._last_stat > 10.0:
            self._last_stat = time.monotonic()
            avg = sum(self._push_ms) / len(self._push_ms)
            self.get_logger().debug(f'eyes_node: panel update takes {avg:.1f} ms on average')

    def _push(self, img_l, img_r):
        """Send the two pictures to the panels (only the round hole's square when possible)."""
        if self._crop and self._rotation == 90:
            self._left_eye.image(img_l, x=NATIVE_X, y=NATIVE_Y)
            self._right_eye.image(img_r, x=NATIVE_X, y=NATIVE_Y)
            return
        for eye, img in ((self._left_eye, img_l), (self._right_eye, img_r)):
            full = Image.new('RGB', (WIDTH, HEIGHT), art.BG)
            full.paste(img, (HOLE_X, HOLE_Y))
            eye.image(full)

    def _clear(self):
        black = Image.new('RGB', (WIDTH, HEIGHT), art.BG)
        self._left_eye.image(black)
        self._right_eye.image(black)

    def destroy_node(self):
        self._running = False
        self._wake.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
        return super().destroy_node()


def main(args=None):
    rclpy.init(args=args)
    node = EyesNode()
    try:
        rclpy.spin(node)
    except KeyboardInterrupt:
        pass
    finally:
        node.destroy_node()
        rclpy.shutdown()


if __name__ == '__main__':
    main()
