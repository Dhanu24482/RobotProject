#!/usr/bin/env python3
"""
Chip-select isolation test for the dual ILI9341 eye displays.

Writes a DIFFERENT solid colour to each display, one at a time, so you can see
whether the CS lines actually isolate the two panels.

Expected result (CS working):
    step 1 -> LEFT turns RED,   RIGHT stays black
    step 2 -> LEFT stays RED,   RIGHT turns GREEN
    step 3 -> LEFT turns BLUE,  RIGHT stays GREEN

If BOTH panels change colour together at every step, the chip-selects are not
isolating: either the CS pins are miswired/shorted, one CS is floating, or the
kernel SPI driver is also driving CE0/CE1.

Run:  python3 scripts/test_eyes_cs.py
"""
import os
import time

os.environ.setdefault('BLINKA_RASPBERRY_PI', '1')

import board
import digitalio
import adafruit_rgb_display.ili9341 as ili9341
from PIL import Image

BAUDRATE = 64000000
WIDTH, HEIGHT = 320, 240

RED   = (255, 0, 0)
GREEN = (0, 255, 0)
BLUE  = (0, 0, 255)
BLACK = (0, 0, 0)


def solid(color):
    return Image.new('RGB', (WIDTH, HEIGHT), color)


def main():
    cs_left  = digitalio.DigitalInOut(board.CE0)
    cs_right = digitalio.DigitalInOut(board.CE1)
    dc       = digitalio.DigitalInOut(board.D24)
    reset    = digitalio.DigitalInOut(board.D25)
    spi      = board.SPI()

    print('Initializing LEFT (CE0)...')
    left = ili9341.ILI9341(spi, rotation=90, cs=cs_left, dc=dc,
                           rst=reset, baudrate=BAUDRATE)
    print('Initializing RIGHT (CE1)...')
    right = ili9341.ILI9341(spi, rotation=90, cs=cs_right, dc=dc,
                            rst=reset, baudrate=BAUDRATE)

    # Clear both so we start from a known state.
    left.image(solid(BLACK))
    right.image(solid(BLACK))
    print('Both cleared to BLACK. Watch the panels...')
    time.sleep(2)

    print('STEP 1: writing RED to LEFT only  -> RIGHT must stay BLACK')
    left.image(solid(RED))
    time.sleep(4)

    print('STEP 2: writing GREEN to RIGHT only -> LEFT must stay RED')
    right.image(solid(GREEN))
    time.sleep(4)

    print('STEP 3: writing BLUE to LEFT only -> RIGHT must stay GREEN')
    left.image(solid(BLUE))
    time.sleep(4)

    print()
    print('Result check:')
    print('  LEFT=BLUE and RIGHT=GREEN  -> chip-selects OK (problem is elsewhere)')
    print('  BOTH the same colour       -> chip-selects NOT isolating')


if __name__ == '__main__':
    main()
