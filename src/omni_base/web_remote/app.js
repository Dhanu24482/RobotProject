/* Lumi voice commander - speech-to-text and command parsing both happen here, in the browser.
 * Only a small structured action is sent to the robot for a RECOGNISED command.
 * Audio and unrecognised speech never reach the robot.
 */
'use strict';

// ════════════════════════════════════════════════════════════
//  PARSER  (pure; mirrors the robot's voice_node vocabulary)
// ════════════════════════════════════════════════════════════
const NUMBER_WORDS = { one: '1', two: '2', three: '3', four: '4', five: '5',
                       six: '6', seven: '7', eight: '8', nine: '9' };

// Speech engines often mangle "Lumi"; any of these counts as the robot's name.
const WAKE_NAMES = ['lumi', 'lumy', 'lumie', 'lumee', 'loomi', 'loomy', 'lomi', 'lomy',
                    'lume', 'lum', 'lu', 'louie', 'lewis', 'looney', 'roomy'];
const WAKE_RE = new RegExp('^(?:hey|hi|hello)\\s+(?:' +
  WAKE_NAMES.slice().sort((a, b) => b.length - a.length).join('|') + ')\\b[\\s,;:!?]*');

// Ordered like voice_node.handle_robot_command (specific phrases before generic ones).
const RULES = [
  [/\b(?:go|move|drive) (?:forward|ahead)\b/,                 { action: 'drive', dir: 'forward' }, 'Move forward'],
  [/\b(?:go|move) (?:backward|backwards|back)\b|\breverse\b/, { action: 'drive', dir: 'back' },    'Move backward'],
  [/\b(?:turn|rotate) left\b/,                                { action: 'drive', dir: 'left' },    'Turn left'],
  [/\b(?:turn|rotate) right\b/,                               { action: 'drive', dir: 'right' },   'Turn right'],
  [/\bnod\b/,                                                 { action: 'gesture', name: 'nod' },   'Nod'],
  [/\bshake\b/,                                               { action: 'gesture', name: 'shake' }, 'Shake'],
  [/\b(?:blink|wink)\b/,                                      { action: 'gesture', name: 'blink' }, 'Blink'],
  [/\bwave left\b/,                                           { action: 'gesture', name: 'wave_left' },  'Wave (left)'],
  [/\bwave right\b/,                                          { action: 'gesture', name: 'wave_right' }, 'Wave (right)'],
  [/\bwave\b/,                                                { action: 'gesture', name: 'wave' },  'Wave'],
  [/\blook left\b/,                                           { action: 'look', dir: 'left' },      'Look left'],
  [/\blook right\b/,                                          { action: 'look', dir: 'right' },     'Look right'],
  [/\blook (?:forward|center|centre|straight)\b/,             { action: 'look', dir: 'center' },    'Look forward'],
  [/\b(?:goodbye|good bye|see you|bye)\b/,                    { action: 'gesture', name: 'goodbye' }, 'Goodbye'],
  [/\bsalute\b/,                                              { action: 'gesture', name: 'salute' }, 'Salute'],
  [/\bhands up\b/,                                            { action: 'gesture', name: 'hands_up' },   'Hands up'],
  [/\bhands down\b/,                                          { action: 'gesture', name: 'hands_down' }, 'Hands down'],
  [/\bwrist up\b/,                                            { action: 'gesture', name: 'wrist_up' },   'Wrist up'],
  [/\bwrist down\b/,                                          { action: 'gesture', name: 'wrist_down' }, 'Wrist down'],
  [/\bwrist\b/,                                               { action: 'gesture', name: 'wrist_center' }, 'Wrist centre'],
  [/\b(?:reset|center|centre)\b/,                             { action: 'gesture', name: 'reset' },   'Reset'],
];

function normalize(text) {
  let t = String(text).toLowerCase().replace(/[^\w\s]/g, ' ');
  t = t.replace(new RegExp('\\b(room|number)\\s+(' + Object.keys(NUMBER_WORDS).join('|') + ')\\b', 'g'),
                (_, a, b) => a + ' ' + NUMBER_WORDS[b]);
  return t.replace(/\s+/g, ' ').trim();
}

function matchRoom(target, rooms) {
  target = target.replace(/^(?:the|to)\s+/, '').trim();
  if (!target) return null;
  const lower = rooms.map(r => [r, r.toLowerCase()]);
  const exact = lower.find(([, l]) => l === target);
  if (exact) return exact[0];
  const fuzzy = lower.filter(([, l]) => l.includes(target) || target.includes(l))
                     .sort((a, b) => b[1].length - a[1].length);
  return fuzzy.length ? fuzzy[0][0] : null;
}

/* parse(text, rooms, typed) ->
 *   {type:'stop'}                      -> emergency stop
 *   {type:'action', body, label}       -> send body to /api/action
 *   {type:'ask', body, label}          -> question for the robot's LLM
 *   {type:'unknown-room', target}      -> understood "go to X" but X is not a known room
 *   {type:'ignore', reason}            -> nothing is sent
 * `typed` = true for keyboard input: unrecognised text is treated as a question
 * (a deliberate act); spoken text must start with "hey Lumi" to become one.
 */
function parse(text, rooms, typed) {
  const t = normalize(text);
  if (!t) return { type: 'ignore', reason: 'empty' };
  const wake = t.match(WAKE_RE);
  const rest = wake ? t.slice(wake[0].length).trim() : t;
  if (wake && !rest) return { type: 'ignore', reason: 'Yes? (say a command after "hey Lumi")' };

  // Safety first: a short utterance containing stop/halt always stops the robot.
  // Longer sentences ("what is a bus stop") fall through so questions still work.
  if (/\b(?:stop|halt)\b/.test(rest) && rest.split(' ').length <= 4) return { type: 'stop' };

  const go = rest.match(/\b(?:go to|navigate to|drive to|take me to)\s+(.+)$/);
  if (go) {
    const room = matchRoom(go[1], rooms);
    return room ? { type: 'action', body: { action: 'goto', room }, label: 'Go to ' + room }
                : { type: 'unknown-room', target: go[1] };
  }
  if (/\bgo home\b/.test(rest)) {
    const room = matchRoom('home', rooms);
    if (room) return { type: 'action', body: { action: 'goto', room }, label: 'Go to ' + room };
  }
  for (const [re, body, label] of RULES) {
    if (re.test(rest)) return { type: 'action', body, label };
  }
  if (typed || wake) return { type: 'ask', body: { action: 'ask', text: rest }, label: 'Ask: ' + rest };
  return { type: 'ignore', reason: 'not a command' };
}


// ════════════════════════════════════════════════════════════
//  HOLD-TO-SPEAK CONTROLLER  (pure: no DOM, testable in Node)
// ════════════════════════════════════════════════════════════
/* States: idle -> starting (asked the browser for the mic) -> listening (audio is flowing)
 *         -> finishing (released, waiting for the final transcript) -> idle.
 * hooks: lang(), state(s), interim(text), result(text, heldMs), fatal(message)
 * The engine can end by itself while the button is still held (silence timeout); we simply
 * start a new one and keep what was heard, so a long command is never cut mid-sentence. */
function createHold(SRClass, hooks, timers) {
  // (wrapped: calling the browser's setTimeout with another `this` throws "Illegal invocation")
  timers = timers || { setTimeout: function (f, ms) { return setTimeout(f, ms); },
                       clearTimeout: function (id) { clearTimeout(id); }, now: function () { return Date.now(); } };
  const FINISH_TIMEOUT_MS = 3000, MAX_RESTARTS = 30, MAX_HOLD_MS = 30000;
  let state = 'idle', held = false, rec = null, done = [], cur = '', restarts = 0, startedAt = 0, finTimer = null, holdTimer = null;

  function setState(s) { state = s; hooks.state(s); }

  // Android's engine reports growing partials as separate "final" results
  // ("go", "go to", "go to room 1"); keep only the longest of each growing run.
  function sessionText(results) {
    const parts = [];
    for (let i = 0; i < results.length; i++) {
      const t = (results[i][0].transcript || '').trim();
      if (!t) continue;
      const prev = parts.length ? parts[parts.length - 1] : '';
      if (prev && t.toLowerCase().indexOf(prev.toLowerCase()) === 0) parts[parts.length - 1] = t;
      else parts.push(t);
    }
    return parts.join(' ');
  }
  function allText() { return done.concat(cur).join(' ').replace(/\s+/g, ' ').trim(); }

  function makeRec() {
    const r = new SRClass();
    r.lang = hooks.lang(); r.continuous = true; r.interimResults = true; r.maxAlternatives = 1;
    const live = function () { if (rec === r && held && state === 'starting') setState('listening'); };
    r.onstart = live; r.onaudiostart = live;
    r.onresult = function (ev) {
      if (rec !== r) return;
      cur = sessionText(ev.results);
      hooks.interim(allText());
    };
    r.onerror = function (ev) {
      if (rec !== r) return;
      const e = ev.error;
      if (e === 'no-speech' || e === 'aborted') return;           // normal: nothing heard yet
      const msg = (e === 'not-allowed' || e === 'service-not-allowed')
        ? 'Microphone is blocked. Allow it for this site in the browser settings.'
        : e === 'audio-capture' ? 'No microphone found.'
        : e === 'network' ? 'Speech service unreachable - the browser needs internet for speech recognition.'
        : 'Speech error: ' + e;
      cancel(); hooks.fatal(msg);
    };
    r.onend = function () {
      if (rec !== r) return;
      done.push(cur); cur = '';
      if (held && restarts < MAX_RESTARTS) {                        // ended by itself mid-hold: carry on
        restarts++; rec = makeRec();
        try { rec.start(); } catch (_) { held = false; finalize(); }
      } else finalize();
    };
    return r;
  }

  function finalize() {
    if (state === 'idle') return;
    timers.clearTimeout(finTimer); timers.clearTimeout(holdTimer);
    const text = allText(), heldMs = timers.now() - startedAt;
    rec = null; held = false; done = []; cur = '';
    setState('idle'); hooks.interim('');
    hooks.result(text, heldMs);
  }
  function cancel() {
    timers.clearTimeout(finTimer); timers.clearTimeout(holdTimer);
    held = false;
    if (rec) { const r = rec; rec = null; try { r.abort(); } catch (_) { /* already stopped */ } }
    done = []; cur = '';
    if (state !== 'idle') setState('idle');
    hooks.interim('');
  }

  const api = {
    press: function () {
      if (state !== 'idle') return false;
      held = true; done = []; cur = ''; restarts = 0; startedAt = timers.now();
      rec = makeRec(); setState('starting');
      // Safety net: if the release event is ever lost (e.g. swallowed by the permission
      // prompt) the microphone must not stay open forever.
      holdTimer = timers.setTimeout(function () { api.release(); }, MAX_HOLD_MS);
      try { rec.start(); } catch (_) { cancel(); hooks.fatal('Could not start the microphone.'); return false; }
      return true;
    },
    release: function () {
      if (!held) return;
      timers.clearTimeout(holdTimer);
      held = false; setState('finishing');
      try { rec.stop(); } catch (_) { /* not started yet: onend (or the timer) still finishes */ }
      finTimer = timers.setTimeout(function () {                   // engines that never fire onend
        if (rec) { const r = rec; try { r.abort(); } catch (_) { /* ignore */ } if (cur) done.push(cur); cur = ''; }
        finalize();
      }, FINISH_TIMEOUT_MS);
    },
    cancel: cancel,
    state: function () { return state; },
  };
  return api;
}

if (typeof module !== 'undefined') module.exports = { parse, normalize, createHold };

// ════════════════════════════════════════════════════════════
//  UI  (browser only)
// ════════════════════════════════════════════════════════════
if (typeof document !== 'undefined') (function () {
  const $ = id => document.getElementById(id);
  const SR = window.SpeechRecognition || window.webkitSpeechRecognition;
  let rooms = [], lastReply = null;

  // ── toasts ──
  function toast(kind, text, ms) {
    const box = $('toasts');
    const el = document.createElement('div');
    el.className = 'toast ' + kind; el.textContent = text;
    box.appendChild(el);
    while (box.children.length > 4) box.removeChild(box.firstChild);
    setTimeout(function () { el.classList.add('out'); setTimeout(function () { el.remove(); }, 300); }, ms || 3200);
  }

  // ── api ──
  async function post(path, body) {
    const res = await fetch(path, { method: 'POST', headers: { 'Content-Type': 'application/json' },
                                    body: JSON.stringify(body || {}), cache: 'no-store' });
    let data = {}; try { data = await res.json(); } catch (_) { /* empty */ }
    if (!res.ok) throw new Error(data.error || ('HTTP ' + res.status));
    return data;
  }
  async function get(path) {
    const res = await fetch(path, { cache: 'no-store' });
    if (!res.ok) throw new Error('HTTP ' + res.status);
    return res.json();
  }

  async function sendStop() {
    try { await post('api/stop'); toast('cmd', 'Stop'); }
    catch (e) { toast('err', 'STOP failed: ' + e.message + ' - try again!', 6000); }
  }
  async function sendAction(body, label, kind) {
    try { await post('api/action', body); toast(kind || 'cmd', label); }
    catch (e) { toast('err', label + ' - ' + e.message, 5000); }
  }

  // ── what to do with a finished utterance ──
  function onResult(text, heldMs) {
    if (!text) { toast('info', heldMs < 400 ? 'Keep holding while you speak' : 'Did not catch anything', 2200); return; }
    const r = parse(text, rooms, false);
    if (r.type === 'stop') return sendStop();
    if (r.type === 'action') return sendAction(r.body, r.label);
    if (r.type === 'ask') return sendAction(r.body, r.label, 'ask');
    if (r.type === 'unknown-room') return toast('warn', 'No room called \u201c' + r.target + '\u201d', 4000);
    toast('info', 'Heard \u201c' + text + '\u201d - not a command', 3500);
  }

  // ── the big button ──
  const LABELS = { idle: 'Hold to speak', starting: 'Starting\u2026', listening: 'Listening\u2026', finishing: 'Sending\u2026' };
  const talk = $('talk');
  const hold = createHold(SR, {
    lang: function () { return $('lang').value; },
    state: function (s) {
      talk.dataset.state = s; $('label').textContent = LABELS[s];
      talk.setAttribute('aria-pressed', s === 'starting' || s === 'listening' ? 'true' : 'false');
    },
    interim: function (t) { $('caption').textContent = t ? '\u201c' + t + '\u201d' : ''; },
    result: onResult,
    fatal: function (m) { toast('err', m, 7000); },
  });

  if (!SR || !window.isSecureContext) {
    talk.disabled = true;
    $('caption').textContent = !SR ? 'This browser has no speech recognition. Use Chrome, Edge or Safari.'
      : 'The microphone needs a secure (https) address.';
  } else {
    // pointer: capture so releasing outside the button still counts as a release
    talk.addEventListener('pointerdown', function (e) {
      if (e.button != null && e.button > 0) return;
      e.preventDefault();
      try { talk.setPointerCapture(e.pointerId); } catch (_) { /* old browsers */ }
      hold.press();
    });
    ['pointerup', 'pointercancel', 'lostpointercapture'].forEach(function (ev) { talk.addEventListener(ev, function (e) { e.preventDefault(); hold.release(); }); });
    talk.addEventListener('contextmenu', function (e) { e.preventDefault(); });   // long-press menu on touch

    // spacebar does the same thing as the button
    const isTyping = function (t) { return t && (t.tagName === 'SELECT' || t.tagName === 'INPUT' || t.tagName === 'TEXTAREA'); };
    document.addEventListener('keydown', function (e) {
      if (e.code === 'Escape') { sendStop(); return; }
      if (e.code !== 'Space' || isTyping(e.target)) return;
      e.preventDefault();                                          // no page scroll / no button click
      if (!e.repeat) hold.press();
    });
    document.addEventListener('keyup', function (e) {
      if (e.code !== 'Space' || isTyping(e.target)) return;
      e.preventDefault(); hold.release();
    });
    // losing focus mid-hold must not leave the microphone open or send half a sentence
    window.addEventListener('blur', function () { hold.cancel(); });
    document.addEventListener('visibilitychange', function () { if (document.hidden) hold.cancel(); });
  }

  $('stop').addEventListener('click', sendStop);
  $('lang').value = localStorage.getItem('lumi_lang') || 'en-US';
  if (!$('lang').value) $('lang').value = 'en-US';
  $('lang').addEventListener('change', function () { localStorage.setItem('lumi_lang', $('lang').value); $('lang').blur(); });

  // ── room names (needed to understand "go to ...") and the robot's spoken replies ──
  async function loadRooms() { try { rooms = (await get('api/rooms')).rooms || []; } catch (_) { /* keep old */ } }
  async function pollReplies() {
    if (document.hidden) return;
    try {
      const d = await get('api/replies' + (lastReply !== null ? '?after=' + lastReply : ''));
      if (lastReply !== null) d.replies.forEach(function (r) { toast('bot', 'Lumi: ' + r.text, 4500); });
      lastReply = d.last;
    } catch (_) { /* transient */ }
  }
  // ── fullscreen (a wall tablet or a phone should be able to hide the browser chrome) ──────────────────
  const fsEl = () => document.fullscreenElement || document.webkitFullscreenElement || null;
  const canFs = !!(document.fullscreenEnabled || document.webkitFullscreenEnabled);
  const syncFs = () => { document.body.setAttribute('data-fullscreen', canFs ? (fsEl() ? 'on' : 'off') : 'unsupported'); };
  function toggleFs() {
    const de = document.documentElement;
    if (fsEl()) { (document.exitFullscreen || document.webkitExitFullscreen).call(document); return; }
    const req = de.requestFullscreen || de.webkitRequestFullscreen;
    if (!req) return;
    const r = req.call(de);
    if (r && r.catch) r.catch(function () { toast('warn', 'The browser blocked fullscreen', 2500); });
  }
  syncFs();
  document.addEventListener('fullscreenchange', syncFs);
  document.addEventListener('webkitfullscreenchange', syncFs);
  $('fs').addEventListener('click', toggleFs);
  document.addEventListener('keydown', function (e) {
    if ((e.key === 'f' || e.key === 'F') && !e.ctrlKey && !e.metaKey && !e.altKey && !e.repeat &&
        !/^(INPUT|SELECT|TEXTAREA)$/.test(e.target.tagName)) toggleFs();
  });

  loadRooms(); pollReplies();
  setInterval(loadRooms, 60000);
  setInterval(pollReplies, 1500);
})();
