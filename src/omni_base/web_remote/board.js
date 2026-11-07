/* Lumi voice page: the picture board.
 *
 * When Lumi's answer comes with a little picture (a circle with r, pi and C for "how do I calculate the circumference"),
 * it is drawn here, stays for the number of seconds the AI suggested, fades away, and can be dismissed at any time.
 *
 * The picture arrives as DATA (see omni_base/canvas_spec.py). It is validated AGAIN here and drawn only from a fixed
 * set of SVG shapes: no HTML, no scripts, no URLs, and every piece of text goes in through textContent.
 */
(function (root) {
  'use strict';

  var NS = 'http://www.w3.org/2000/svg';
  var COLORS = ['accent', 'green', 'yellow', 'red', 'white'];
  var SIZES = ['small', 'large'];
  var SECONDS_MIN = 8, SECONDS_MAX = 90, SECONDS_DEFAULT = 20;
  var MAX_SHAPES = 16, FADE_MS = 1300, GRACE_MS = 6000;

  // ── validation (the same rules as the robot's canvas_spec.py) ────────────────────────────────────
  function num(v, lo, hi) {
    if (typeof v !== 'number' && typeof v !== 'string') return null;       // not null, booleans, arrays...
    if (typeof v === 'string' && v.trim() === '') return null;
    v = Number(v);
    if (!isFinite(v)) return null;
    return Math.min(hi, Math.max(lo, v));
  }
  function str(v, n) {
    if (typeof v !== 'string') return '';
    return v.replace(/[\u0000-\u001f\u007f-\u009f\u2028\u2029]/g, ' ').replace(/\s+/g, ' ').trim().slice(0, n);
  }
  function seconds(v) {
    var s = num(v, SECONDS_MIN, SECONDS_MAX);
    return s === null ? SECONDS_DEFAULT : Math.round(s);
  }
  function nums(src, names, limits) {
    var out = [];
    for (var i = 0; i < names.length; i++) {
      var lim = (limits && limits[names[i]]) || [0, 100];
      var v = num(src[names[i]], lim[0], lim[1]);
      if (v === null) return null;
      out.push(v);
    }
    return out;
  }

  // ── drawing ──────────────────────────────────────────────────────────────────────────────────────
  function mk(doc, tag, attrs) {
    var e = doc.createElementNS(NS, tag);
    for (var k in attrs) if (Object.prototype.hasOwnProperty.call(attrs, k)) e.setAttribute(k, String(attrs[k]));
    return e;
  }
  function r1(v) { return Math.round(v * 100) / 100; }
  function polar(cx, cy, r, deg) { var a = deg * Math.PI / 180; return [cx + r * Math.cos(a), cy + r * Math.sin(a)]; }

  function shapeNode(doc, s, index) {
    if (!s || typeof s !== 'object') return null;
    var color = COLORS.indexOf(s.color) >= 0 ? s.color : '';
    var base = 'sh' + (color ? ' c-' + color : '');
    var line = base + (s.dashed === true ? ' dash fade' : ' draw');   // solid lines draw themselves, dashed ones fade in
    var node = null, v;

    switch (s.kind) {
      case 'circle':
        v = nums(s, ['x', 'y', 'r'], { r: [0.5, 60] });
        if (!v) return null;
        node = mk(doc, 'circle', { cx: v[0], cy: v[1], r: v[2], pathLength: 1,
                                   'class': line + (s.fill === true ? ' fill' : '') });
        break;
      case 'dot':
        v = nums(s, ['x', 'y']);
        if (!v) return null;
        node = mk(doc, 'circle', { cx: v[0], cy: v[1], r: 1.7, 'class': base + ' dot fade' });
        break;
      case 'line':
      case 'arrow':
        v = nums(s, ['x', 'y', 'x2', 'y2']);
        if (!v) return null;
        if (s.kind === 'line') {
          node = mk(doc, 'line', { x1: v[0], y1: v[1], x2: v[2], y2: v[3], pathLength: 1, 'class': line });
        } else {
          node = mk(doc, 'g', { 'class': 'arrow' });
          node.appendChild(mk(doc, 'line', { x1: v[0], y1: v[1], x2: v[2], y2: v[3], pathLength: 1, 'class': line }));
          if (Math.hypot(v[2] - v[0], v[3] - v[1]) > 4) {                  // the head
            var a = Math.atan2(v[3] - v[1], v[2] - v[0]), L = 3.6, w = 0.45;
            var pts = [v[2], v[3],
                       v[2] - L * Math.cos(a - w), v[3] - L * Math.sin(a - w),
                       v[2] - L * Math.cos(a + w), v[3] - L * Math.sin(a + w)].map(r1);
            node.appendChild(mk(doc, 'polygon', { points: pts[0] + ',' + pts[1] + ' ' + pts[2] + ',' + pts[3] + ' ' + pts[4] + ',' + pts[5],
                                                  'class': base + ' head fade' }));
          }
        }
        break;
      case 'rect':
        v = nums(s, ['x', 'y', 'w', 'h']);
        if (!v) return null;
        node = mk(doc, 'rect', { x: v[0], y: v[1], width: v[2], height: v[3], rx: 1, pathLength: 1,
                                 'class': line + (s.fill === true ? ' fill' : '') });
        break;
      case 'arc':
        v = nums(s, ['x', 'y', 'r', 'start', 'end'], { r: [0.5, 60], start: [-720, 720], end: [-720, 720] });
        if (!v) return null;
        var d = v[4] - v[3];
        if (Math.abs(d) >= 360) {
          node = mk(doc, 'circle', { cx: v[0], cy: v[1], r: v[2], pathLength: 1, 'class': line });
        } else {
          var p0 = polar(v[0], v[1], v[2], v[3]).map(r1), p1 = polar(v[0], v[1], v[2], v[4]).map(r1);
          node = mk(doc, 'path', { d: 'M' + p0[0] + ' ' + p0[1] + ' A' + v[2] + ' ' + v[2] + ' 0 ' + (Math.abs(d) > 180 ? 1 : 0) +
                                      ' ' + (d >= 0 ? 1 : 0) + ' ' + p1[0] + ' ' + p1[1], pathLength: 1, 'class': line + ' arc' });
        }
        break;
      case 'poly':
        if (!Array.isArray(s.points)) return null;
        var flat = [];
        for (var i = 0; i + 1 < Math.min(s.points.length, 16); i += 2) {
          var px = num(s.points[i], 0, 100), py = num(s.points[i + 1], 0, 100);
          if (px !== null && py !== null) flat.push(px + ',' + py);
        }
        if (flat.length < 3) return null;
        node = mk(doc, 'polygon', { points: flat.join(' '), pathLength: 1, 'class': line + (s.fill === true ? ' fill' : '') });
        break;
      case 'text':
        var text = str(s.text, 14);
        v = nums(s, ['x', 'y']);
        if (!text || !v) return null;
        node = mk(doc, 'text', { x: v[0], y: v[1], 'text-anchor': 'middle', 'dominant-baseline': 'central',
                                 'class': base + ' t fade' + (SIZES.indexOf(s.size) >= 0 ? ' ' + s.size : '') });
        node.textContent = text;                                           // plain text, never markup
        break;
      default:
        return null;
    }
    node.style.setProperty('--i', String(index));                          // staggers the draw-in
    return node;
  }

  /** Validate `spec` and build its SVG. Pure (pass any document), so it can be tested without a browser. */
  function build(spec, doc) {
    spec = spec && typeof spec === 'object' ? spec : {};
    var title = str(spec.title, 50);
    var svg = mk(doc, 'svg', { viewBox: '0 0 100 100', role: 'img', 'aria-label': title || 'Picture' });
    var count = 0;
    var raw = Array.isArray(spec.shapes) ? spec.shapes.slice(0, MAX_SHAPES * 2) : [];
    for (var i = 0; i < raw.length && count < MAX_SHAPES; i++) {
      var n = shapeNode(doc, raw[i], count);
      if (n) { svg.appendChild(n); count++; }
    }
    var notes = [];
    if (Array.isArray(spec.notes)) {
      spec.notes.slice(0, 6).forEach(function (t) { t = str(t, 70); if (t && notes.length < 3) notes.push(t); });
    }
    return { svg: svg, count: count, title: title, formula: str(spec.formula, 48), notes: notes, seconds: seconds(spec.seconds) };
  }

  // ── the board on the page: show, fade away, dismiss ──────────────────────────────────────────────
  var ui = null, fadeTimer = null, hideTimer = null;

  function reduced() {
    return !!(root.matchMedia && root.matchMedia('(prefers-reduced-motion: reduce)').matches);
  }
  function clearTimers() { clearTimeout(fadeTimer); clearTimeout(hideTimer); fadeTimer = hideTimer = null; }
  function restartBar(ms) {
    ui.bar.style.setProperty('--dur', ms + 'ms');
    ui.bar.classList.remove('run');
    void ui.bar.offsetWidth;                                               // restart the CSS animation
    ui.bar.classList.add('run');
  }
  function startFade() {
    ui.board.classList.add('fade-out');
    hideTimer = setTimeout(hide, reduced() ? 0 : FADE_MS);
  }
  function hide() {
    if (!ui) return;
    clearTimers();
    ui.board.hidden = true;
    ui.board.classList.remove('fade-out', 'held');
    root.document.body.classList.remove('has-board');
  }
  function empty(node) { while (node.firstChild) node.removeChild(node.firstChild); }

  function init() {
    var d = root.document;
    ui = { board: d.getElementById('board'), title: d.getElementById('board-title'), art: d.getElementById('board-art'),
           formula: d.getElementById('board-formula'), notes: d.getElementById('board-notes'),
           bar: d.getElementById('board-bar'), close: d.getElementById('board-x') };
    ui.close.addEventListener('click', hide);
    // Pointing at it (or touching it) holds it on screen; it gets a few more seconds after you let go.
    ui.board.addEventListener('pointerenter', function () {
      if (ui.board.hidden) return;
      clearTimers();
      ui.board.classList.remove('fade-out');
      ui.board.classList.add('held');
    });
    ui.board.addEventListener('pointerleave', function () {
      if (ui.board.hidden) return;
      ui.board.classList.remove('held');
      restartBar(GRACE_MS);
      fadeTimer = setTimeout(startFade, GRACE_MS);
    });
  }

  /** Show a picture spec. Returns false (and shows nothing) if there is nothing worth drawing. */
  function show(spec) {
    if (!ui) init();
    var b = build(spec, root.document);
    if (!b.count && !b.formula && !b.notes.length) return false;
    clearTimers();
    ui.board.classList.remove('fade-out', 'held');
    ui.title.textContent = b.title;
    ui.title.hidden = !b.title;
    empty(ui.art); ui.art.appendChild(b.svg);
    ui.formula.textContent = b.formula; ui.formula.hidden = !b.formula;
    empty(ui.notes);
    b.notes.forEach(function (t) { var li = root.document.createElement('li'); li.textContent = t; ui.notes.appendChild(li); });
    ui.notes.hidden = !b.notes.length;
    ui.board.hidden = false;
    root.document.body.classList.add('has-board');
    restartBar(b.seconds * 1000);
    fadeTimer = setTimeout(startFade, b.seconds * 1000);
    return true;
  }

  var api = { build: build, show: show, hide: hide, num: num, str: str, seconds: seconds };
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
  root.LumiBoard = api;
})(typeof window !== 'undefined' ? window : globalThis);
