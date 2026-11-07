# LUMI Control Panel Layouts

Six interchangeable front-ends for the tablet-mounted control panel. Open
[`index.html`](index.html) to browse them side by side and pick one.

All six are fully functional and behave identically — they differ only in
visual arrangement, so you can choose on looks alone.

## How it is put together

Three layers, so a change is made once:

| Layer | File | Owns |
| --- | --- | --- |
| Design system | [`../lumi-ui.css`](../lumi-ui.css) | Colours, type, buttons, cards, sliders, the drive pad, expression buttons, map stage, toast, banner, tabs, slide-over panels. Dark by default; `data-theme="auto"` follows the device, `data-theme="light"` forces light. |
| Engine | [`../lumi-core.js`](../lumi-core.js) | All ROS I/O, map rendering, input handling, and the generated control sets (expressions, body moves, map modes, drive pad, speed sliders). |
| Layout | `layouts/<name>.html` + `<name>.css` | Only *where* things go. The CSS file is arrangement only (grid areas, sizes, breakpoints). |

A page starts itself: `<body data-lumi-app="mapCanvas">` is all it needs (add
`data-autoconnect="false"` to stop it connecting on load). There is no inline
JavaScript, no inline CSS and nothing loaded from the internet: `roslib` and the
fonts live in [`../lib/`](../lib/), so the dashboards work with the robot's
internet down, and the panel serves them under a strict Content-Security-Policy
(`script-src 'self'; style-src 'self'; font-src 'self'`).

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
- `lib/roslib.min.js` loads (the page needs the `lib/` folder next to
  `lumi-core.js`). The banner says so if `ROSLIB` is missing.

### The map does not show up ("Waiting for /map")

`/map` is a *latched* topic: the map server publishes it **once** and keeps it for late joiners. rosbridge only receives that
saved copy when it subscribes in the matching way, and it picks the wrong way when something else also publishes `/map`
(for example the Zenoh bridge's own publisher while a remote RViz is connected). The page used to wait forever then.
It now asks the map server directly if no map arrives within about 2.5 s (`/map_server/map`, or `/slam_toolbox/dynamic_map`
while mapping, both `nav_msgs/GetMap`) and keeps retrying while navigation is still starting.

To check from the Pi whether the page gets the map, with the robot running:

```bash
node scripts/check_map_delivery.js        # prints "MAP RECEIVED: 69 x 78" (or "NO MAP after 15 s")
```

### Regression test

`scripts/check_web_layouts.py` runs `lumi-core.js` against a synthetic DOM built
from each layout, then simulates taps on every binding. It catches an exception
during init that would otherwise silently kill every control on the page, the
kind of failure a syntax check misses. It uses `py-mini-racer` where that has a
native build and falls back to Node.js (any machine with `node`, including the
Raspberry Pi):

```bash
python3 scripts/check_web_layouts.py
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
- Every page (all six layouts, the layout picker and the voice page) has a
  **fullscreen** button in its header, and the `F` key does the same. The icon flips
  while fullscreen is on, and the button hides itself on browsers that cannot do it
  (iPhone Safari). Browsers only enter fullscreen right after a real tap or key press.
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
| `data-emotion="happy"` | Publishes to `/robot/emotion`. Names the eyes know: `neutral happy sad angry surprised blink love excited dizzy sleepy wink` (aliases such as `heart_eyes`, `star_eyes` also work). |
| `data-mode="goal"` | Switches map interaction mode. Gets an `act` class when selected. |
| `data-room="lobby"` | Navigates to a named destination. |
| `data-action="stop"` | One of `connect`, `disconnect`, `stop`, `cancel`, `go`, `zoom-in`, `zoom-out`, `fit`, `fullscreen`. |
| `data-lumi="rooms"` | A `<select>` populated with destinations. Add `data-autogo` to navigate on change instead of requiring a separate send button. |
| `data-lumi="room-buttons"` | A container filled with one button per destination. |
| `data-lumi="motor-power"` | A `range` input that retunes the robot's PWM ceiling on `/robot/speed`. Publishes on release, and syncs itself from `/robot/speed/state`, so it shows the robot's real speed rather than the markup default. Pair with `data-lumi="motor-power-value"` and `data-lumi="motor-power-pct"` read-outs. |

### Generated control sets

The core builds these from one definition each, so adding an expression or a body
move is a one-line change in `lumi-core.js`, not an edit in every layout. Put an
empty element where you want them:

| Markup | Fills the element with |
| --- | --- |
| `<div class="emo-grid" data-lumi="emotion-buttons">` | A button per expression, drawn as the pair of eyes the robot shows (no emoji, so it looks the same on every device). Add `data-only="happy,love,wink"` for a subset. The current expression is highlighted. |
| `<div class="chip-grid" data-lumi="body-buttons">` | The body moves (nod, shake, waves, salute, wrist…). |
| `<div class="seg" data-lumi="mode-buttons">` | The four map modes (add `cols-2` for a 2×2 grid, `vertical` for a column). |
| `<div data-lumi="dpad">` | The drive pad. `data-variant="row"` gives a compact one-row pad for narrow bars. |
| `<div data-lumi="speed-controls">` | Forward / turn / robot-speed sliders (the robot-speed one is left out of the kiosk on purpose). |
| `<button data-tab="go">` + `<section data-tabpanel="go">` | Tabs; the first one is open at start. |
| `<button data-open="drawer">` + `<div class="overlay" id="drawer">` | A slide-over panel; `data-close`, a tap on the backdrop or Escape closes it. |

Note the difference between the two kinds of speed control. `lin-speed` and
`ang-speed` only shape the `Twist` that *this page* sends, so they affect the
on-screen drive pad and nothing else. `motor-power` changes the PWM ceiling on the
robot, which also governs Nav2 goals, the voice shortcuts and the Bluetooth handset —
so it is deliberately absent from the kiosk layout, where the public can reach it.

The map reads its colours from the CSS custom properties `--accent`, `--green`,
`--dim`, `--text` and `--map-unknown` / `--map-wall` / `--map-free`, so it themes
itself to whichever layout it is in (including the light theme).

## The old single-page interface

`../index.html` used to be a 700-line single-file dashboard with its own copy of
the logic and a hard-coded address. It is now a small redirect to this folder:
everything it did is in the layouts (the **Console** layout is the closest match).

## The layout picker

[`index.html`](index.html) shows a schematic of each layout. Tick *Open the
layout I pick automatically on this device* and a wall tablet opens straight into
its layout from then on; every layout has a **Layouts** link (`index.html?choose`)
to come back.
