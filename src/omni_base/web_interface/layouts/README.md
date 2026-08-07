# LUMI Control Panel Layouts

Six interchangeable front-ends for the tablet-mounted control panel. Open
[`index.html`](index.html) to browse them side by side and pick one.

All six are fully functional and behave identically — they differ only in
visual arrangement, so you can choose on looks alone.

## Why there is a shared core

Every layout loads [`../lumi-core.js`](../lumi-core.js), which owns all of the
ROS I/O, map rendering and input handling. The layout files contain no
JavaScript beyond a single `LUMI.init()` call. That keeps a bug fix or a new
feature a one-file change instead of a six-file change, and it makes the
layouts genuinely comparable.

## The layouts

| File | Name | Best for |
| --- | --- | --- |
| `kiosk.html` | Kiosk | **A public tablet.** Huge destination tiles, plain language, one big stop button. Teleop and connection settings hide behind a gear so a visitor cannot reach them. |
| `immersive.html` | Immersive | The original design, now working. Edge-to-edge map with frosted glass HUD floating on top. Most screen goes to the map. |
| `console.html` | Console | Engineers. Three columns showing modes, teleop with speed sliders, topic health, and a live log all at once. |
| `split.html` | Split | A balance. Map on the left, tabbed panel (Go / Drive / Express / Setup) on the right. |
| `dashboard.html` | Dashboard | Bright rooms. Card grid, and the only layout that follows the tablet's light/dark mode setting. |
| `minimal.html` | Minimal | Distraction-free. Pure black, a single bottom bar, everything else is map. |

## Running it

Start the robot with the web bridge enabled:

```bash
ros2 launch omni_base navigation.launch.py use_voice:=true use_web:=true
```

Then open a layout. Two options:

**Serve from the Pi** (recommended for a wall tablet — the connection URL is
then filled in automatically):

```bash
cd ~/ros2_ws/src/omni_base/web_interface
python3 -m http.server 8080
```

Browse to `http://<pi-ip>:8080/layouts/` on the tablet.

**Or open the file directly** on the tablet and type the address
`ws://<pi-ip>:9090` into the connection field once.

Either way the address is saved in the tablet's local storage and the panel
reconnects by itself on every later visit, so a wall-mounted tablet comes back
up on its own after a reboot.

### If nothing responds

Every control refuses to act while the panel is offline, so a bad address makes
the whole page look broken. A red banner along the bottom always states the
address it tried and lets you correct it inline — read that first.

The usual cause is **previewing on your own machine**. Serving the folder from
your laptop and opening `http://127.0.0.1:8080/layouts/` means the page's own
host is `127.0.0.1`, which is not the robot. The panel detects this and falls
back to the Pi's usual address (`192.168.43.221`) rather than a dead
`ws://127.0.0.1:9090`, but if the Pi is on a different IP you must type it in.
If your Pi's address has changed permanently, update `FALLBACK_HOST` near the
top of `lumi-core.js`.

Other things worth checking:

- The launch really did include `use_web:=true`, or `rosbridge_websocket` is
  running some other way. Confirm with `ros2 node list | grep rosbridge`.
- Port 9090 is reachable from the tablet: `curl -v telnet://<pi-ip>:9090`.
- The two CDN `<script>` tags loaded. Without internet on the tablet, `roslib`
  is missing and the banner says so. For a permanently offline robot, download
  `roslib.min.js` and `eventemitter2.min.js` next to `lumi-core.js` and point
  the two tags at the local copies.

### Regression test

`scripts/check_web_layouts.py` runs `lumi-core.js` in a real JS engine against a
synthetic DOM built from each layout, then simulates taps on every binding. It
catches an exception during init that would otherwise silently kill every
control on the page — the kind of failure a syntax check misses.

```bash
uv run --no-project --python 3.12 --with py-mini-racer python scripts/check_web_layouts.py
```

## Tablet behaviour

These were built for touch, which the previous interface was not:

- Every control uses pointer events, so touch, pen and mouse all work.
- The map supports one-finger drag to aim a goal, two-finger pinch to zoom and
  two-finger drag to pan.
- On the map, a **tap** sends the robot to that spot keeping its current
  heading; a **drag** additionally aims it in the direction you dragged.
- Drive buttons capture the pointer, so lifting your finger always stops the
  robot even if it slid off the button.
- Driving also stops automatically if the tab is backgrounded or loses focus.
- Page zoom and text selection are disabled so stray touches cannot disturb the
  layout.

## Destinations

The destination list is the merge of two sources:

1. `config/rooms.yaml`, mirrored as the fallback list inside `lumi-core.js`.
2. Anything saved at runtime, which arrives on the latched `/saved_locations`
   topic from `location_manager` and replaces the fallback list.

To add a destination from the panel, switch the map mode to **Save**, then tap
(or drag, to set a facing direction) on the map and give it a name. It is
published to `/save_location`, persisted to disk by `location_manager`, and
becomes usable by voice commands immediately.

If the destination list ever looks stale, it means the browser did not receive
the latched message. Recent versions of `rosbridge_server` match the
publisher's `TRANSIENT_LOCAL` durability automatically; on older versions the
panel falls back to the static `rooms.yaml` list until the next save occurs.

## Extending a layout

The core binds itself to markup by attribute, so adding a control is usually
one line of HTML with no JavaScript:

| Attribute | Effect |
| --- | --- |
| `data-lumi="px"` | Element is filled with a live value. Also gets a `data-state` of `ok`/`warn`/`err` you can style. |
| `data-drive="1,0"` | Hold to drive. The pair is a linear and angular multiplier applied to the current speed settings. |
| `data-body="NOD"` | Publishes to `/robot/body/command`. |
| `data-emotion="happy"` | Publishes to `/robot/emotion`. |
| `data-mode="goal"` | Switches map interaction mode. Gets an `act` class when selected. |
| `data-room="lobby"` | Navigates to a named destination. |
| `data-action="stop"` | One of `connect`, `disconnect`, `stop`, `cancel`, `go`, `zoom-in`, `zoom-out`, `fit`, `fullscreen`. |
| `data-lumi="rooms"` | A `<select>` populated with destinations. Add `data-autogo` to navigate on change instead of requiring a separate send button. |
| `data-lumi="room-buttons"` | A container filled with one button per destination. |

The map reads its colours from the CSS custom properties `--accent`, `--green`,
`--dim` and `--text`, so it themes itself to whichever layout it is in.

## Note on the previous interface

`../index.html` is the original single-file control panel. It still works and
is left untouched. Once you have chosen a layout, you can either point people
at it directly or copy it over `index.html`.
