/*  LUMI control panel — shared core.
 *
 *  Layout files supply only markup and CSS; everything below owns the ROS I/O,
 *  map rendering and input handling so the six layouts cannot drift apart.
 *
 *  Layouts wire themselves up declaratively — no per-layout JavaScript:
 *
 *    Display   <span data-lumi="px">          filled with a live value
 *    Drive     <button data-drive="1,0">      hold to move (lin,ang multipliers)
 *    Body      <button data-body="NOD">       publishes /robot/body/command
 *    Emotion   <button data-emotion="happy">  publishes /robot/emotion
 *    Mode      <button data-mode="goal">      switches map interaction mode
 *    Room      <button data-room="lobby">     navigates to a known room
 *    Action    <button data-action="connect"> connect/disconnect/stop/cancel/zoom/fit
 *    Rooms UI  <select data-lumi="rooms">     auto-populated with destinations
 *              <div data-lumi="room-buttons"> auto-filled with one button per room
 *    Speed     <input data-lumi="motor-power"> drag to retune the robot's PWM
 *                                             ceiling (Nav2, voice and Bluetooth)
 *
 *  Every data-lumi field is optional: a layout renders only what it wants and
 *  missing fields are skipped silently.
 */
(function (global) {
'use strict';

// ── Configuration ────────────────────────────────────────────────────────────

var STORAGE_URL = 'lumi.rosbridge.url';

// Fallback destinations. Anything saved at runtime arrives via /saved_locations
// and is merged on top of these.
var STATIC_ROOMS = {
  'room 1':    { x:  0.703, y: -0.069, yaw: 1.75 },
  'room 2':    { x: -0.127, y: -0.235, yaw: 3.09 },
  'room 3':    { x: -0.198, y: -1.068, yaw: 1.83 },
  'room 4':    { x: -2.0,   y:  1.5,   yaw: 0.0  },
  'reception': { x: -1.0,   y:  0.5,   yaw: 1.57 },
  'lobby':     { x:  0.5,   y: -2.0,   yaw: 3.14 },
  'home':      { x:  0.0,   y:  0.0,   yaw: 0.0  }
};

var HINTS = {
  goal:   'Drag on the map to set a navigation goal and its facing direction',
  add:    'Tap (or drag for direction) to save a new named location',
  teleop: 'Drag the map to pan. Use the D-pad or arrow keys to drive',
  pose:   'Drag to tell AMCL where the robot actually is'
};

// ── State ────────────────────────────────────────────────────────────────────

var ros = null, connected = false;
var rooms = Object.assign({}, STATIC_ROOMS);
var mapData = null, mapInfo = null;
var robotPose = { x: 0, y: 0, yaw: 0 };
var scale = 2, offsetX = 0, offsetY = 0;
var goalArrow = null, dragOrigin = null;
var moveInterval = null;
var mode = 'goal';
var canvas = null, ctx = null;
var pubs = {};

// ── Small DOM helpers ────────────────────────────────────────────────────────

function all(sel)  { return Array.prototype.slice.call(document.querySelectorAll(sel)); }
function fields(n) { return all('[data-lumi="' + n + '"]'); }

/** Write a value into every element tagged with this field name.
 *  `state` ('ok' | 'warn' | 'err') is exposed as a data-state attribute so each
 *  layout can colour it however it likes. */
function setField(name, value, state) {
  fields(name).forEach(function (el) {
    el.textContent = value;
    if (state !== undefined) el.setAttribute('data-state', state || '');
  });
}

function setBody(attr, value) { document.body.setAttribute(attr, value); }

function themeColor(varName, fallback) {
  var v = getComputedStyle(document.documentElement).getPropertyValue(varName).trim();
  return v || fallback;
}

// ── Logging and toasts ───────────────────────────────────────────────────────

function log(msg, type) {
  var stamp = new Date().toLocaleTimeString([], { hour12: false });
  fields('log').forEach(function (box) {
    var line = document.createElement('div');
    line.className = 'le ' + (type || 'i');
    line.setAttribute('data-state', type || 'i');
    line.textContent = stamp + '  ' + msg;
    box.appendChild(line);
    box.scrollTop = box.scrollHeight;
    while (box.children.length > 80) box.removeChild(box.firstChild);
  });
  if (type === 'e') console.warn('[LUMI]', msg);
}

var toastTimer = null;
function toast(msg) {
  var els = fields('toast');
  if (!els.length) { log(msg, 'i'); return; }
  els.forEach(function (el) {
    var span = el.querySelector('[data-lumi="toast-msg"]') || el;
    span.textContent = msg;
    el.classList.add('show');
  });
  clearTimeout(toastTimer);
  toastTimer = setTimeout(function () {
    els.forEach(function (el) { el.classList.remove('show'); });
  }, 2800);
}

// ── Status banner ────────────────────────────────────────────────────────────
// Injected by the core instead of being copied into six layouts. Without it a
// disconnected panel just looks broken: every control silently refuses and
// nothing on screen explains why. It also surfaces uncaught script errors, so
// a future mistake can never fail invisibly again.

var banner = null, bannerText = null, bannerInput = null;

function buildBanner() {
  if (banner) return;
  banner = document.createElement('div');
  banner.style.cssText =
    'position:fixed;left:50%;bottom:14px;transform:translateX(-50%);z-index:2147483647;' +
    'max-width:min(720px,94vw);padding:13px 18px;border-radius:13px;' +
    'background:#7f1d1d;color:#fff;border:1px solid #ef4444;' +
    "font:500 13px/1.45 Inter,system-ui,sans-serif;box-shadow:0 10px 30px rgba(0,0,0,.5);" +
    'display:none;gap:11px;align-items:center;flex-wrap:wrap;';

  bannerText = document.createElement('span');
  banner.appendChild(bannerText);

  bannerInput = document.createElement('input');
  bannerInput.type = 'text';
  bannerInput.spellcheck = false;
  bannerInput.style.cssText =
    'background:rgba(0,0,0,.35);border:1px solid rgba(255,255,255,.35);color:#fff;' +
    'padding:7px 11px;border-radius:8px;font:inherit;width:210px;outline:none;';
  bannerInput.addEventListener('keydown', function (e) {
    if (e.key === 'Enter') retryFromBanner();
  });
  banner.appendChild(bannerInput);

  var go = document.createElement('button');
  go.textContent = 'Connect';
  go.style.cssText =
    'background:#fff;color:#7f1d1d;border:none;padding:8px 16px;border-radius:8px;' +
    'font:600 13px Inter,system-ui,sans-serif;cursor:pointer;';
  go.addEventListener('click', retryFromBanner);
  banner.appendChild(go);

  document.body.appendChild(banner);
}

function retryFromBanner() {
  var url = bannerInput.value.trim();
  if (!url) return;
  var input = document.querySelector('[data-lumi="url"]');
  if (input) input.value = url;          // keep the layout's own field in step
  connect();
}

function showBanner(msg, url) {
  buildBanner();
  bannerText.textContent = msg;
  if (url !== undefined) bannerInput.value = url;
  bannerInput.style.display = url === null ? 'none' : '';
  banner.style.display = 'flex';
}

function hideBanner() { if (banner) banner.style.display = 'none'; }

global.addEventListener('error', function (e) {
  showBanner('Script error: ' + (e.message || 'unknown') + ' — see the browser console.', null);
});

// ── ROS connection ───────────────────────────────────────────────────────────

function savedUrl() {
  try { return localStorage.getItem(STORAGE_URL) || ''; } catch (e) { return ''; }
}

// Served from the Pi, the page's own host is the robot. Previewed from a laptop
// on localhost or opened from a file:// bookmark it is definitely not, and
// ws://192.168.43.221:9090 is a guaranteed-dead guess — fall back to the Pi's usual
// address so the field starts somewhere useful.
var FALLBACK_HOST = '192.168.43.221';

function defaultUrl() {
  var host = location.hostname;
  var localPreview = !host || host === 'localhost' || host === '192.168.43.221' || host === '[::1]';
  return 'ws://192.168.43.221:9090';
}

function currentUrl() {
  var input = document.querySelector('[data-lumi="url"]');
  var val = input ? input.value.trim() : '';
  return val || savedUrl() || defaultUrl();
}

function connect() {
  if (typeof ROSLIB === 'undefined') {
    toast('roslib failed to load — check the tablet\'s internet connection');
    log('ROSLIB missing. The CDN scripts did not load.', 'e');
    return;
  }
  var url = currentUrl();
  var input = document.querySelector('[data-lumi="url"]');
  if (input) input.value = url;

  log('Connecting to ' + url + ' ...', 'i');
  setField('conn', 'Connecting…', 'warn');
  showBanner('Connecting to ' + url + ' …', url);

  var everConnected = false;
  ros = new ROSLIB.Ros({ url: url });

  ros.on('connection', function () {
    connected = true; everConnected = true;
    // Only remember an address that actually worked, so a failed guess does not
    // become the pre-filled default forever.
    try { localStorage.setItem(STORAGE_URL, url); } catch (e) { /* private mode */ }
    setBody('data-connected', 'true');
    setField('conn', 'Online', 'ok');
    setField('rosurl', url.replace('ws://', '').replace(':9090', ''), 'ok');
    hideBanner();
    log('Connected to rosbridge.', 'o');
    toast('Connected');
    setupTopics();
  });

  ros.on('error', function () {
    setField('conn', 'Error', 'err');
    log('rosbridge connection error at ' + url, 'e');
  });

  ros.on('close', function () {
    connected = false;
    setBody('data-connected', 'false');
    setField('conn', 'Offline', 'err');
    setField('rosurl', 'offline', 'err');
    ['scan', 'mapstat', 'amcl', 'body'].forEach(function (f) { setField(f, '—', ''); });
    showBanner(
      everConnected
        ? 'Lost the connection to ' + url + '. Check the robot is still running.'
        : 'Could not reach the robot at ' + url + '. Check the address, and that '
          + 'the launch used use_web:=true.',
      url
    );
    render();   // repaint the map placeholder with the offline message
    log('Disconnected from rosbridge.', 'w');
  });
}

function disconnect() { if (ros) ros.close(); }

function setupTopics() {
  pubs.cmdVel  = new ROSLIB.Topic({ ros: ros, name: '/cmd_vel',            messageType: 'geometry_msgs/Twist' });
  pubs.goal    = new ROSLIB.Topic({ ros: ros, name: '/goal_pose',          messageType: 'geometry_msgs/PoseStamped' });
  pubs.initial = new ROSLIB.Topic({ ros: ros, name: '/initialpose',        messageType: 'geometry_msgs/PoseWithCovarianceStamped' });
  pubs.body    = new ROSLIB.Topic({ ros: ros, name: '/robot/body/command', messageType: 'std_msgs/String' });
  pubs.emotion = new ROSLIB.Topic({ ros: ros, name: '/robot/emotion',      messageType: 'std_msgs/String' });
  pubs.save    = new ROSLIB.Topic({ ros: ros, name: '/save_location',      messageType: 'std_msgs/String' });
  pubs.speed   = new ROSLIB.Topic({ ros: ros, name: '/robot/speed',        messageType: 'std_msgs/Int32' });
  setField('body', 'ok', 'ok');

  // The bridge owns the real speed and republishes it every couple of seconds, so
  // the slider shows what the robot is actually doing — including the configured
  // default on first connect, and any change made from another tablet.
  new ROSLIB.Topic({ ros: ros, name: '/robot/speed/state', messageType: 'std_msgs/Int32' })
    .subscribe(function (msg) { showSpeed(msg.data); });

  new ROSLIB.Topic({ ros: ros, name: '/map', messageType: 'nav_msgs/OccupancyGrid', throttle_rate: 3000 })
    .subscribe(function (msg) {
      mapData = msg.data; mapInfo = msg.info;
      setField('mapstat', 'ok', 'ok');
      setField('mapsize', msg.info.width + ' x ' + msg.info.height);
      fitView(); render();
    });

  new ROSLIB.Topic({ ros: ros, name: '/amcl_pose', messageType: 'geometry_msgs/PoseWithCovarianceStamped', throttle_rate: 400 })
    .subscribe(function (msg) {
      var p = msg.pose.pose;
      robotPose = { x: p.position.x, y: p.position.y, yaw: quatToYaw(p.orientation) };
      setField('px', robotPose.x.toFixed(2));
      setField('py', robotPose.y.toFixed(2));
      setField('pyaw', (robotPose.yaw * 180 / Math.PI).toFixed(0) + '\u00B0');
      setField('amcl', 'ok', 'ok');
      render();
    });

  new ROSLIB.Topic({ ros: ros, name: '/scan', messageType: 'sensor_msgs/LaserScan', throttle_rate: 2000 })
    .subscribe(function () { setField('scan', 'ok', 'ok'); });

  new ROSLIB.Topic({ ros: ros, name: '/robot/voice/command', messageType: 'std_msgs/String' })
    .subscribe(function (msg) { setField('voice', msg.data); log('Heard: "' + msg.data + '"', 'i'); });

  new ROSLIB.Topic({ ros: ros, name: '/robot/emotion', messageType: 'std_msgs/String' })
    .subscribe(function (msg) { setField('emotion', msg.data); setBody('data-emotion', msg.data); });

  // Runtime-saved points from location_manager (latched JSON).
  new ROSLIB.Topic({ ros: ros, name: '/saved_locations', messageType: 'std_msgs/String' })
    .subscribe(function (msg) {
      try {
        var payload = JSON.parse(msg.data);
        var locs = payload.locations || {};
        rooms = Object.assign({}, STATIC_ROOMS);
        Object.keys(locs).forEach(function (k) {
          var c = locs[k];
          rooms[k] = { x: +c[0], y: +c[1], yaw: +c[2] };
        });
        populateRooms();
        render();
        log('Saved locations synced (' + Object.keys(rooms).length + ' destinations).', 'i');
      } catch (e) { log('Could not parse /saved_locations.', 'w'); }
    });

  new ROSLIB.Topic({ ros: ros, name: '/navigate_to_pose/_action/status', messageType: 'action_msgs/GoalStatusArray' })
    .subscribe(function (msg) {
      if (!msg.status_list || !msg.status_list.length) return;
      var s = msg.status_list[msg.status_list.length - 1].status;
      if (s === 4) {
        setField('navstate', 'Arrived', 'ok');
        setBody('data-nav', 'idle');
        goalArrow = null; render();
        log('Goal reached.', 'o'); toast('Arrived');
      } else if (s === 6) {
        setField('navstate', 'Failed', 'err');
        setBody('data-nav', 'idle');
        goalArrow = null; render();
        log('Goal failed — path blocked or unreachable.', 'e');
        toast('Could not reach the destination');
      } else if (s === 5) {
        setField('navstate', 'Cancelled', 'warn');
        setBody('data-nav', 'idle');
      }
    });
}

// ── Geometry ─────────────────────────────────────────────────────────────────

function quatToYaw(q) { return Math.atan2(2 * (q.w * q.z + q.x * q.y), 1 - 2 * (q.y * q.y + q.z * q.z)); }
function yawToQuat(y) { return { x: 0, y: 0, z: Math.sin(y / 2), w: Math.cos(y / 2) }; }

function worldToCanvas(wx, wy) {
  if (!mapInfo) return { x: 0, y: 0 };
  return {
    x: offsetX + ((wx - mapInfo.origin.position.x) / mapInfo.resolution) * scale,
    y: offsetY + (mapInfo.height - (wy - mapInfo.origin.position.y) / mapInfo.resolution) * scale
  };
}

function canvasToWorld(cx, cy) {
  if (!mapInfo) return { x: 0, y: 0 };
  return {
    x: mapInfo.origin.position.x + ((cx - offsetX) / scale) * mapInfo.resolution,
    y: mapInfo.origin.position.y + (mapInfo.height - (cy - offsetY) / scale) * mapInfo.resolution
  };
}

function eventToWorld(e) {
  var r = canvas.getBoundingClientRect();
  return canvasToWorld(e.clientX - r.left, e.clientY - r.top);
}

// ── Map rendering ────────────────────────────────────────────────────────────

function sizeCanvas() {
  if (!canvas) return;
  var host = canvas.parentElement;
  var dpr = global.devicePixelRatio || 1;
  var w = host.clientWidth, h = host.clientHeight;
  canvas.width = w * dpr; canvas.height = h * dpr;
  canvas.style.width = w + 'px'; canvas.style.height = h + 'px';
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  return { w: w, h: h };
}

function fitView() {
  if (!mapInfo || !canvas) return;
  var s = sizeCanvas();
  scale = Math.min(s.w / mapInfo.width, s.h / mapInfo.height) * 0.9;
  offsetX = (s.w - mapInfo.width * scale) / 2;
  offsetY = (s.h - mapInfo.height * scale) / 2;
  setField('zoom', scale.toFixed(1) + 'x');
}

function render() {
  if (!canvas) return;
  var s = sizeCanvas();
  ctx.clearRect(0, 0, s.w, s.h);

  var accent = themeColor('--accent', '#38bdf8');
  var green  = themeColor('--green', '#10b981');

  if (!mapData || !mapInfo) {
    ctx.fillStyle = themeColor('--dim', '#94a3b8');
    ctx.font = '14px Inter, system-ui, sans-serif';
    ctx.textAlign = 'center';
    ctx.fillText(connected ? 'Waiting for /map …' : 'Not connected to the robot',
                 s.w / 2, s.h / 2);
    return;
  }

  var W = mapInfo.width, H = mapInfo.height;
  var img = ctx.createImageData(W, H);
  for (var i = 0; i < mapData.length; i++) {
    var v = mapData[i], r, g, b;
    if (v === -1)      { r = 17; g = 24;  b = 39;  }   // unknown
    else if (v >= 65)  { r = 2;  g = 6;   b = 23;  }   // occupied
    else               { r = 226; g = 232; b = 240; }  // free
    img.data[i * 4] = r; img.data[i * 4 + 1] = g; img.data[i * 4 + 2] = b; img.data[i * 4 + 3] = 255;
  }
  var off = document.createElement('canvas');
  off.width = W; off.height = H;
  off.getContext('2d').putImageData(img, 0, 0);

  ctx.save();
  ctx.translate(offsetX, offsetY);
  ctx.scale(scale, scale);
  ctx.translate(0, H);
  ctx.scale(1, -1);
  ctx.imageSmoothingEnabled = false;
  ctx.drawImage(off, 0, 0);
  ctx.restore();

  // Saved destinations
  Object.keys(rooms).forEach(function (key) {
    var p = worldToCanvas(rooms[key].x, rooms[key].y);
    ctx.beginPath(); ctx.arc(p.x, p.y, 5, 0, Math.PI * 2);
    ctx.fillStyle = green; ctx.fill();
    ctx.fillStyle = themeColor('--text', '#f8fafc');
    ctx.font = '11px Inter, system-ui, sans-serif';
    ctx.textAlign = 'center';
    ctx.fillText(key, p.x, p.y - 10);
  });

  if (goalArrow) drawArrow(goalArrow.x, goalArrow.y, goalArrow.angle, accent, 26);

  var rp = worldToCanvas(robotPose.x, robotPose.y);
  drawRobot(rp.x, rp.y, -robotPose.yaw, green);
}

function drawRobot(cx, cy, yaw, color) {
  ctx.save(); ctx.translate(cx, cy); ctx.rotate(yaw);
  ctx.beginPath(); ctx.arc(0, 0, 13, 0, Math.PI * 2);
  ctx.fillStyle = 'rgba(16,185,129,0.25)'; ctx.fill();
  ctx.strokeStyle = color; ctx.lineWidth = 2.5; ctx.stroke();
  ctx.beginPath(); ctx.moveTo(0, 0); ctx.lineTo(18, 0);
  ctx.strokeStyle = color; ctx.lineWidth = 3; ctx.stroke();
  ctx.restore();
}

function drawArrow(cx, cy, angle, color, sz) {
  ctx.save(); ctx.translate(cx, cy); ctx.rotate(angle);
  ctx.beginPath(); ctx.arc(0, 0, sz * 0.55, 0, Math.PI * 2);
  ctx.strokeStyle = color; ctx.lineWidth = 2;
  ctx.setLineDash([3, 3]); ctx.stroke(); ctx.setLineDash([]);
  ctx.beginPath(); ctx.moveTo(0, 0); ctx.lineTo(sz, 0);
  ctx.strokeStyle = color; ctx.lineWidth = 3; ctx.stroke();
  ctx.beginPath(); ctx.moveTo(sz, 0); ctx.lineTo(sz - 9, -6); ctx.lineTo(sz - 9, 6);
  ctx.closePath(); ctx.fillStyle = color; ctx.fill();
  ctx.restore();
}

// ── Canvas input: pointer events cover mouse, pen and touch alike ────────────

var pointers = new Map();
var panPrev = null, pinchPrev = null;

function pinchInfo() {
  var pts = Array.from(pointers.values());
  var dx = pts[0].x - pts[1].x, dy = pts[0].y - pts[1].y;
  return { dist: Math.hypot(dx, dy), cx: (pts[0].x + pts[1].x) / 2, cy: (pts[0].y + pts[1].y) / 2 };
}

function bindCanvas() {
  canvas.style.touchAction = 'none';

  canvas.addEventListener('pointerdown', function (e) {
    canvas.setPointerCapture(e.pointerId);
    pointers.set(e.pointerId, { x: e.clientX, y: e.clientY });

    if (pointers.size === 2) {           // second finger — switch to pan/zoom
      dragOrigin = null; goalArrow = null;
      pinchPrev = pinchInfo();
      return;
    }
    // Middle/right mouse button, or teleop mode, pans instead of placing a goal.
    if (e.button === 1 || e.button === 2 || mode === 'teleop') {
      panPrev = { x: e.clientX, y: e.clientY };
    } else {
      dragOrigin = eventToWorld(e);
    }
  });

  canvas.addEventListener('pointermove', function (e) {
    if (pointers.has(e.pointerId)) pointers.set(e.pointerId, { x: e.clientX, y: e.clientY });

    var w = eventToWorld(e);
    setField('cursor', w.x.toFixed(2) + ', ' + w.y.toFixed(2));

    if (pointers.size === 2 && pinchPrev) {
      var now = pinchInfo();
      var f = now.dist / (pinchPrev.dist || 1);
      var r = canvas.getBoundingClientRect();
      var mx = now.cx - r.left, my = now.cy - r.top;
      offsetX = mx - (mx - offsetX) * f;
      offsetY = my - (my - offsetY) * f;
      offsetX += now.cx - pinchPrev.cx;
      offsetY += now.cy - pinchPrev.cy;
      scale *= f;
      pinchPrev = now;
      setField('zoom', scale.toFixed(1) + 'x');
      render();
      return;
    }

    if (panPrev) {
      offsetX += e.clientX - panPrev.x;
      offsetY += e.clientY - panPrev.y;
      panPrev = { x: e.clientX, y: e.clientY };
      render();
      return;
    }

    if (dragOrigin) {
      var a = Math.atan2(-(w.y - dragOrigin.y), w.x - dragOrigin.x);
      var cp = worldToCanvas(dragOrigin.x, dragOrigin.y);
      goalArrow = { x: cp.x, y: cp.y, angle: -a };
      render();
    }
  });

  function endPointer(e) {
    pointers.delete(e.pointerId);
    if (pointers.size < 2) pinchPrev = null;

    if (panPrev) { panPrev = null; return; }
    if (!dragOrigin) return;

    var w = eventToWorld(e);
    var dist = Math.hypot(w.x - dragOrigin.x, w.y - dragOrigin.y);
    // A tap keeps the robot's current heading; a drag aims it.
    var yaw = dist > 0.12 ? Math.atan2(-(w.y - dragOrigin.y), w.x - dragOrigin.x) : robotPose.yaw;

    if (mode === 'goal')      sendGoal(dragOrigin.x, dragOrigin.y, yaw);
    else if (mode === 'pose') sendInitialPose(dragOrigin.x, dragOrigin.y, yaw);
    else if (mode === 'add')  promptSave(dragOrigin.x, dragOrigin.y, yaw);

    dragOrigin = null;
    if (mode !== 'goal') { goalArrow = null; render(); }
  }

  canvas.addEventListener('pointerup', endPointer);
  canvas.addEventListener('pointercancel', function (e) {
    pointers.delete(e.pointerId); dragOrigin = null; panPrev = null; pinchPrev = null;
  });
  canvas.addEventListener('contextmenu', function (e) { e.preventDefault(); });

  canvas.addEventListener('wheel', function (e) {
    var f = e.deltaY < 0 ? 1.15 : 0.87;
    var r = canvas.getBoundingClientRect();
    var mx = e.clientX - r.left, my = e.clientY - r.top;
    offsetX = mx - (mx - offsetX) * f;
    offsetY = my - (my - offsetY) * f;
    scale *= f;
    setField('zoom', scale.toFixed(1) + 'x');
    render();
    e.preventDefault();
  }, { passive: false });
}

// ── Navigation actions ───────────────────────────────────────────────────────

function requireConnection() {
  if (!connected) { toast('Connect to the robot first'); return false; }
  return true;
}

function sendGoal(x, y, yaw) {
  if (!requireConnection()) return;
  pubs.goal.publish(new ROSLIB.Message({
    header: { frame_id: 'map' },
    pose: { position: { x: x, y: y, z: 0 }, orientation: yawToQuat(yaw) }
  }));
  var p = worldToCanvas(x, y);
  goalArrow = { x: p.x, y: p.y, angle: -yaw };
  setField('gx', x.toFixed(2));
  setField('gy', y.toFixed(2));
  setField('navstate', 'Navigating', 'warn');
  setBody('data-nav', 'active');
  log('Goal sent to (' + x.toFixed(2) + ', ' + y.toFixed(2) + ').', 'o');
  toast('On the way');
  render();
}

function sendInitialPose(x, y, yaw) {
  if (!requireConnection()) return;
  var cov = new Array(36).fill(0);
  cov[0] = 0.25; cov[7] = 0.25; cov[35] = 0.0685;
  pubs.initial.publish(new ROSLIB.Message({
    header: { frame_id: 'map' },
    pose: { pose: { position: { x: x, y: y, z: 0 }, orientation: yawToQuat(yaw) }, covariance: cov }
  }));
  log('Initial pose set to (' + x.toFixed(2) + ', ' + y.toFixed(2) + ').', 'o');
  toast('Pose updated');
}

function cancelNav() {
  stopMove();
  setField('navstate', 'Cancelled', 'warn');
  setBody('data-nav', 'idle');
  goalArrow = null; render();
  log('Navigation cancelled.', 'w');
  toast('Cancelled');
}

function goToRoom(key) {
  var d = rooms[key];
  if (!d) { toast('Unknown destination: ' + key); return; }
  sendGoal(d.x, d.y, d.yaw);
}

function goSelectedRoom() {
  var sel = document.querySelector('[data-lumi="rooms"]');
  if (!sel || !sel.value) { toast('Pick a destination first'); return; }
  goToRoom(sel.value);
}

function promptSave(x, y, yaw) {
  var name = prompt('Name this location (' + x.toFixed(2) + ', ' + y.toFixed(2) + '):');
  if (!name || !name.trim()) { goalArrow = null; render(); return; }
  var id = name.trim().toLowerCase();
  rooms[id] = { x: x, y: y, yaw: yaw };
  if (pubs.save) {
    pubs.save.publish(new ROSLIB.Message({ data: JSON.stringify({ name: id, x: x, y: y, yaw: yaw }) }));
  }
  populateRooms();
  log('Saved "' + id + '" — voice navigation can now use it.', 'o');
  toast('Saved "' + id + '"');
  setMode('goal');
}

// ── Teleop ───────────────────────────────────────────────────────────────────

function speeds() {
  var lin = document.querySelector('[data-lumi="lin-speed"]');
  var ang = document.querySelector('[data-lumi="ang-speed"]');
  return { lin: lin ? parseFloat(lin.value) : 0.20, ang: ang ? parseFloat(ang.value) : 0.50 };
}

function startMove(linMul, angMul) {
  if (!connected) { toast('Connect to the robot first'); return; }
  var s = speeds();
  var lv = linMul * s.lin, av = angMul * s.ang;
  if (moveInterval) clearInterval(moveInterval);
  var push = function () {
    pubs.cmdVel.publish(new ROSLIB.Message({
      linear: { x: lv, y: 0, z: 0 }, angular: { x: 0, y: 0, z: av }
    }));
    setField('vel', lv.toFixed(2) + ' / ' + av.toFixed(2));
  };
  push();
  moveInterval = setInterval(push, 100);   // keeps the Arduino watchdog fed
  setBody('data-driving', 'true');
}

function stopMove() {
  if (moveInterval) { clearInterval(moveInterval); moveInterval = null; }
  if (pubs.cmdVel) {
    pubs.cmdVel.publish(new ROSLIB.Message({
      linear: { x: 0, y: 0, z: 0 }, angular: { x: 0, y: 0, z: 0 }
    }));
  }
  setField('vel', '0.00 / 0.00');
  setBody('data-driving', 'false');
}

function bodyCmd(cmd) {
  if (!requireConnection()) return;
  pubs.body.publish(new ROSLIB.Message({ data: cmd }));
  log('Body command: ' + cmd, 'i');
}

// ── Global drive speed ───────────────────────────────────────────────────────
// Distinct from the lin-speed/ang-speed sliders, which only shape the Twist this
// page sends. This sets the PWM ceiling on the robot itself, so it also governs
// Nav2 goals, the voice shortcuts and the Bluetooth handset.

var PWM_MAX = 255;

function speedReadout(pwm) {
  setField('motor-power-value', String(pwm));
  setField('motor-power-pct', Math.round((pwm / PWM_MAX) * 100) + '%');
}

// Applies a value that came from the robot. Assigning .value does not fire input
// or change events, so this cannot bounce back out as a new command.
function showSpeed(pwm) {
  var el = document.querySelector('[data-lumi="motor-power"]');
  if (el) el.value = pwm;
  speedReadout(pwm);
}

function setSpeed(pwm) {
  if (!requireConnection()) return;
  var v = Math.round(parseFloat(pwm));
  pubs.speed.publish(new ROSLIB.Message({ data: v }));
  log('Drive speed -> ' + v + ' PWM', 'i');
  toast('Speed ' + v);
}

function setEmotion(name) {
  if (!requireConnection()) return;
  pubs.emotion.publish(new ROSLIB.Message({ data: name }));
  log('Emotion: ' + name, 'i');
}

// ── Modes, rooms, zoom ───────────────────────────────────────────────────────

// Deliberately not persisted: a public tablet should always come back up in
// plain "send a goal" mode rather than resuming Save or Init Pose.
function setMode(m) {
  mode = m;
  dragOrigin = null;
  all('[data-mode]').forEach(function (el) {
    el.classList.toggle('act', el.getAttribute('data-mode') === m);
  });
  // Distinct attribute name: reusing data-mode here would make <body> match the
  // [data-mode] selector above and pick up the active-button styling.
  setBody('data-map-mode', m);
  setField('mode', m);
  setField('hint', HINTS[m] || '');
  if (m !== 'goal' && m !== 'add') { goalArrow = null; render(); }
}

function populateRooms() {
  var names = Object.keys(rooms).sort();

  all('[data-lumi="rooms"]').forEach(function (sel) {
    var keep = sel.value;
    sel.innerHTML = '<option value="" disabled selected>Choose a destination…</option>';
    names.forEach(function (n) {
      var o = document.createElement('option');
      o.value = n; o.textContent = n.replace(/\b\w/g, function (c) { return c.toUpperCase(); });
      sel.appendChild(o);
    });
    if (keep && rooms[keep]) sel.value = keep;
  });

  all('[data-lumi="room-buttons"]').forEach(function (host) {
    host.innerHTML = '';
    names.forEach(function (n) {
      var b = document.createElement('button');
      b.className = 'room-btn';
      b.setAttribute('data-room', n);
      b.textContent = n.replace(/\b\w/g, function (c) { return c.toUpperCase(); });
      b.addEventListener('click', function () { goToRoom(n); });
      host.appendChild(b);
    });
  });

  setField('roomcount', names.length);
}

function zoom(f) {
  var s = { w: canvas.clientWidth, h: canvas.clientHeight };
  var cx = s.w / 2, cy = s.h / 2;
  offsetX = cx - (cx - offsetX) * f;
  offsetY = cy - (cy - offsetY) * f;
  scale *= f;
  setField('zoom', scale.toFixed(1) + 'x');
  render();
}

function resetView() { fitView(); render(); }

// ── Declarative wiring ───────────────────────────────────────────────────────

var ACTIONS = {
  connect: connect,
  disconnect: disconnect,
  stop: stopMove,
  cancel: cancelNav,
  go: goSelectedRoom,
  'zoom-in': function () { zoom(1.25); },
  'zoom-out': function () { zoom(0.8); },
  fit: resetView,
  fullscreen: function () {
    if (document.fullscreenElement) document.exitFullscreen();
    else document.documentElement.requestFullscreen().catch(function () {});
  }
};

function bindControls() {
  // Hold-to-drive buttons. Pointer events give us mouse and touch in one path;
  // capturing the pointer means the release still lands here even if the finger
  // has slid off the button, so the robot cannot be left driving.
  all('[data-drive]').forEach(function (el) {
    var parts = el.getAttribute('data-drive').split(',');
    var lin = parseFloat(parts[0]) || 0, ang = parseFloat(parts[1]) || 0;
    el.style.touchAction = 'none';
    el.addEventListener('pointerdown', function (e) {
      e.preventDefault();
      el.setPointerCapture(e.pointerId);
      el.classList.add('held');
      if (lin === 0 && ang === 0) stopMove(); else startMove(lin, ang);
    });
    ['pointerup', 'pointercancel', 'pointerleave'].forEach(function (evt) {
      el.addEventListener(evt, function () {
        if (!el.classList.contains('held')) return;
        el.classList.remove('held');
        stopMove();
      });
    });
  });

  all('[data-body]').forEach(function (el) {
    el.addEventListener('click', function () { bodyCmd(el.getAttribute('data-body')); });
  });
  all('[data-emotion]').forEach(function (el) {
    el.addEventListener('click', function () { setEmotion(el.getAttribute('data-emotion')); });
  });
  all('[data-mode]').forEach(function (el) {
    el.addEventListener('click', function () { setMode(el.getAttribute('data-mode')); });
  });
  all('[data-room]').forEach(function (el) {
    el.addEventListener('click', function () { goToRoom(el.getAttribute('data-room')); });
  });
  all('[data-action]').forEach(function (el) {
    var fn = ACTIONS[el.getAttribute('data-action')];
    if (fn) el.addEventListener('click', fn);
  });

  // Live-updating speed slider read-outs
  [['lin-speed', 'lin-value'], ['ang-speed', 'ang-value']].forEach(function (pair) {
    var input = document.querySelector('[data-lumi="' + pair[0] + '"]');
    if (!input) return;
    var show = function () { setField(pair[1], parseFloat(input.value).toFixed(2)); };
    input.addEventListener('input', show);
    show();
  });

  // Global drive speed. The read-out tracks the thumb, but the robot is only told
  // on release — a drag would otherwise fire dozens of serial writes on the way.
  var power = document.querySelector('[data-lumi="motor-power"]');
  if (power) {
    power.addEventListener('input',  function () { speedReadout(power.value); });
    power.addEventListener('change', function () { setSpeed(power.value); });
    speedReadout(power.value);
  }

  var urlInput = document.querySelector('[data-lumi="url"]');
  if (urlInput) {
    urlInput.value = savedUrl() || defaultUrl();
    urlInput.addEventListener('keydown', function (e) { if (e.key === 'Enter') connect(); });
  }

  // Layouts without a separate "Send" button opt into navigating straight off
  // the dropdown by adding data-autogo.
  all('[data-lumi="rooms"][data-autogo]').forEach(function (sel) {
    sel.addEventListener('change', function () { if (sel.value) goToRoom(sel.value); });
  });
}

function bindKeyboard() {
  var keys = {
    ArrowUp: [1, 0], ArrowDown: [-1, 0], ArrowLeft: [0, 1], ArrowRight: [0, -1],
    w: [1, 0], s: [-1, 0], a: [0, 1], d: [0, -1]
  };
  document.addEventListener('keydown', function (e) {
    if (/^(INPUT|SELECT|TEXTAREA)$/.test(e.target.tagName)) return;
    if (e.key === ' ') { stopMove(); e.preventDefault(); return; }
    var k = keys[e.key];
    if (k && !moveInterval) startMove(k[0], k[1]);
  });
  document.addEventListener('keyup', function (e) {
    if (keys[e.key]) stopMove();
  });
  // A tablet losing focus mid-drive must not leave the robot rolling.
  global.addEventListener('blur', stopMove);
  document.addEventListener('visibilitychange', function () { if (document.hidden) stopMove(); });
}

// ── Init ─────────────────────────────────────────────────────────────────────

function init(opts) {
  opts = opts || {};
  canvas = document.getElementById(opts.canvas || 'mapCanvas');
  if (canvas) { ctx = canvas.getContext('2d'); bindCanvas(); }

  bindControls();
  bindKeyboard();
  populateRooms();

  setBody('data-connected', 'false');
  setField('conn', 'Offline', 'err');
  setField('navstate', 'Idle', '');
  setField('vel', '0.00 / 0.00');
  setMode('goal');

  global.addEventListener('resize', function () { if (mapData) fitView(); render(); });
  render();
  log('Control panel ready.', 'i');

  // A wall tablet should come back on its own after a reboot, so always try —
  // the saved address if there is one, otherwise the best guess. A failure is
  // not silent: the banner appears with the address it tried.
  if (opts.autoConnect === false) {
    showBanner('Not connected to the robot.', currentUrl());
  } else {
    setTimeout(connect, 400);
  }
}

// ── Public API ───────────────────────────────────────────────────────────────

global.LUMI = {
  init: init,
  connect: connect,
  disconnect: disconnect,
  sendGoal: sendGoal,
  sendInitialPose: sendInitialPose,
  cancelNav: cancelNav,
  goToRoom: goToRoom,
  goSelectedRoom: goSelectedRoom,
  startMove: startMove,
  stopMove: stopMove,
  bodyCmd: bodyCmd,
  setSpeed: setSpeed,
  setEmotion: setEmotion,
  setMode: setMode,
  zoom: zoom,
  resetView: resetView,
  render: render,
  log: log,
  toast: toast,
  get rooms() { return rooms; },
  get connected() { return connected; }
};

})(window);
