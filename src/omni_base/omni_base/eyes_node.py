#!/usr/bin/env python3
"""
Eyes Node — Dual ILI9341 TFT emotion display for Lumi robot.

Drives two 2.4" ILI9341 320x240 displays connected to the Raspberry Pi's
hardware SPI0 bus. Subscribes to /robot/emotion (std_msgs/String) and renders
the matching expression on both eyes simultaneously.

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
    from PIL import Image, ImageDraw
    _HW_AVAILABLE = True
except Exception as _e:
    _HW_IMPORT_ERROR = str(_e)  # captured for logging in __init__

# ── Constants (from PDF final code) ──────────────────────────────────────────
BAUDRATE  = 64000000
ROTATION  = 90
BG_COLOR  = (0, 0, 0)
EYE_COLOR = (0, 150, 255)

# Display dimensions in landscape (rotation=90)
WIDTH  = 320
HEIGHT = 240


class EyesNode(Node):
    def __init__(self):
        super().__init__('eyes_node')

        emotion_topic = self.declare_parameter('emotion_topic', '/robot/emotion').value
        self._rotation  = self.declare_parameter('rotation',  ROTATION).value
        self._baudrate  = self.declare_parameter('baudrate',  BAUDRATE).value

        self._left_eye  = None
        self._right_eye = None
        self._hw        = False
        self._last_emotion = 'neutral'

        if _HW_AVAILABLE:
            self._init_displays()
        else:
            self.get_logger().warn(
                f'eyes_node: hardware libraries unavailable — {_HW_IMPORT_ERROR}. '
                'Running headless — emotions will be logged only.'
            )

        self.create_subscription(String, emotion_topic, self._on_emotion, 10)

        # Show neutral on startup
        self._render('neutral')
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
        if name == 'blink':
            threading.Thread(target=self._blink_then_restore, daemon=True).start()
        else:
            self._last_emotion = name
            self._render(name)

    # ── Render dispatcher ─────────────────────────────────────────────────────

    def _render(self, name: str):
        if not self._hw:
            return  # headless mode — no display hardware available
        dispatch = {
            'surprised': self._draw_surprised,
            'happy':     self._draw_happy,
            'neutral':   self._draw_neutral,
            'angry':     self._draw_angry,
            'sad':       self._draw_sad,
        }
        fn = dispatch.get(name)
        if fn is None:
            self.get_logger().warn(f'eyes_node: unknown emotion "{name}", ignoring.')
            return
        fn()

    # ── Expression drawing ────────────────────────────────────────────────────
    # All geometry ported verbatim from the PDF final robot_eyes.py (pages 11-12)
    # with width=320, height=240 in landscape.

    def _new_frames(self):
        """Return two blank (BG_COLOR) images and their draw handles."""
        img_l  = Image.new('RGB', (WIDTH, HEIGHT), BG_COLOR)
        img_r  = Image.new('RGB', (WIDTH, HEIGHT), BG_COLOR)
        draw_l = ImageDraw.Draw(img_l)
        draw_r = ImageDraw.Draw(img_r)
        return img_l, img_r, draw_l, draw_r

    def _push(self, img_l, img_r):
        """Send both frames to the physical displays (no-op if headless)."""
        if not self._hw:
            return
        self._left_eye.image(img_l)
        self._right_eye.image(img_r)

    def _draw_surprised(self):
        img_l, img_r, draw_l, draw_r = self._new_frames()
        outer_box = [70, 30, 250, 210]
        inner_box = [110, 70, 210, 170]
        draw_l.ellipse(outer_box, fill=EYE_COLOR)
        draw_l.ellipse(inner_box, fill=BG_COLOR)
        draw_r.ellipse(outer_box, fill=EYE_COLOR)
        draw_r.ellipse(inner_box, fill=BG_COLOR)
        self._push(img_l, img_r)

    def _draw_happy(self):
        img_l, img_r, draw_l, draw_r = self._new_frames()
        outer_box = [70, 30, 250, 210]
        mask_box  = [60, 60, 260, 260]   # shifted down -> hides bottom half -> upper crescent
        draw_l.ellipse(outer_box, fill=EYE_COLOR)
        draw_l.ellipse(mask_box,  fill=BG_COLOR)
        draw_r.ellipse(outer_box, fill=EYE_COLOR)
        draw_r.ellipse(mask_box,  fill=BG_COLOR)
        self._push(img_l, img_r)

    def _draw_neutral(self):
        img_l, img_r, draw_l, draw_r = self._new_frames()
        dash_box = [80, 100, 240, 140]
        draw_l.rounded_rectangle(dash_box, radius=20, fill=EYE_COLOR)
        draw_r.rounded_rectangle(dash_box, radius=20, fill=EYE_COLOR)
        self._push(img_l, img_r)

    def _draw_angry(self):
        img_l, img_r, draw_l, draw_r = self._new_frames()
        outer_box = [70, 30, 250, 210]
        # Left eye: brow slashes down-right; Right eye: brow slashes down-left (mirrored)
        l_mask = [(60, 30), (260, 30), (260, 140)]
        r_mask = [(60, 30), (260, 30), (60, 140)]
        draw_l.ellipse(outer_box, fill=EYE_COLOR)
        draw_l.polygon(l_mask,    fill=BG_COLOR)
        draw_r.ellipse(outer_box, fill=EYE_COLOR)
        draw_r.polygon(r_mask,    fill=BG_COLOR)
        self._push(img_l, img_r)

    def _draw_sad(self):
        """Lower crescent: BG mask shifted upward hides top half of the ellipse."""
        img_l, img_r, draw_l, draw_r = self._new_frames()
        outer_box = [70, 30, 250, 210]
        mask_box  = [60, -20, 260, 180]   # shifted up -> hides top half -> lower crescent
        draw_l.ellipse(outer_box, fill=EYE_COLOR)
        draw_l.ellipse(mask_box,  fill=BG_COLOR)
        draw_r.ellipse(outer_box, fill=EYE_COLOR)
        draw_r.ellipse(mask_box,  fill=BG_COLOR)
        self._push(img_l, img_r)

    def _blink_then_restore(self):
        """Transient blink: briefly show closed-lid, then restore last emotion."""
        img_l, img_r, draw_l, draw_r = self._new_frames()
        # Closed-lid = thin horizontal bar (slimmer than neutral)
        lid_box = [80, 115, 240, 125]
        draw_l.rounded_rectangle(lid_box, radius=5, fill=EYE_COLOR)
        draw_r.rounded_rectangle(lid_box, radius=5, fill=EYE_COLOR)
        self._push(img_l, img_r)
        time.sleep(0.15)
        self._render(self._last_emotion)


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
