'use strict';
(function () {
  const $ = id => document.getElementById(id);

  // ── tiny DOM helper (text only - nothing from the API is ever parsed as HTML) ──
  function h(tag, props, ...kids) {
    const el = document.createElement(tag);
    for (const [k, v] of Object.entries(props || {})) {
      if (k === 'class') el.className = v;
      else if (k === 'onclick') el.addEventListener('click', v);
      else if (k === 'onchange') el.addEventListener('change', v);
      else if (v === true) el.setAttribute(k, '');
      else if (v !== false && v != null) el.setAttribute(k, v);
    }
    for (const kid of kids.flat()) if (kid != null) el.append(kid.nodeType ? kid : document.createTextNode(String(kid)));
    return el;
  }

  // ── api ──
  async function api(path, body) {
    const opts = body === undefined ? {} : {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) };
    const res = await fetch(path, { cache: 'no-store', ...opts });
    let data = {};
    try { data = await res.json(); } catch (_) { /* empty */ }
    if (!res.ok) { const e = new Error(data.error || ('HTTP ' + res.status)); e.data = data; e.status = res.status; throw e; }
    return data;
  }
  let toastTimer = null;
  function toast(msg, err) {
    const t = $('toast'); t.textContent = msg; t.className = 'toast' + (err ? ' err' : ''); t.hidden = false;
    clearTimeout(toastTimer); toastTimer = setTimeout(() => { t.hidden = true; }, err ? 6000 : 2500);
  }
  function note(id, msg, kind) {
    const el = $(id); if (!msg) { el.hidden = true; return; }
    el.textContent = msg; el.className = 'note' + (kind ? ' ' + kind : ''); el.hidden = false;
  }
  async function act(fn, okMsg) {
    try { await fn(); if (okMsg) toast(okMsg); await refresh(); }
    catch (e) { toast(e.message, true); }
  }

  let S = null;                       // latest /api/status
  const COMP_INFO = {
    eyes: ['Eye displays', 'The two animated TFT eyes (reacts to emotions).'],
    rosbridge: ['Robot web', 'The tablet dashboards at /robot/ (drive, map, goals). Uses an internal rosbridge that is only reachable through this panel.'],
    rviz_bridge: ['Remote RViz2 bridge', 'Lets RViz2 on another machine see the robot through the tunnel. Set up on the RViz2 tab.'],
  };

  // ── tabs ──
  let tab = 'overview';
  function showTab(name) {
    tab = name;
    document.querySelectorAll('#tabs button').forEach(b => b.classList.toggle('on', b.dataset.tab === name));
    document.querySelectorAll('.tab').forEach(t => { t.hidden = t.id !== 'tab-' + name; });
    if (name === 'maps') loadMaps();
    if (name === 'settings') loadEnv();
    if (name === 'logs') loadLogs();
    if (name === 'rviz') loadRviz();
    if (name === 'components') renderComponents(true);
  }
  $('tabs').addEventListener('click', ev => { const b = ev.target.closest('button[data-tab]'); if (b) showTab(b.dataset.tab); });

  // ── status polling ──
  async function refresh() {
    try { S = await api('/api/status'); } catch (e) { $('sys-pill').textContent = 'offline'; $('sys-pill').className = 'pill warn'; return; }
    renderOverview(); renderComponents(false);
    if (tab === 'settings') renderBehaviour(false);
  }
  setInterval(() => { if (!document.hidden) refresh(); }, 2000);

  const STATE_TEXT = { running: 'running', stopped: 'stopped', stopping: 'stopping…', crashed: 'restarting', failed: 'failed', blocked: 'blocked' };
  function fmtUp(sec) {
    if (sec == null) return '';
    if (sec < 90) return sec + 's'; if (sec < 5400) return Math.round(sec / 60) + ' min';
    return (sec / 3600).toFixed(1) + ' h';
  }

  function renderOverview() {
    const core = S.processes.core, s = S.settings;
    const pill = $('sys-pill');
    pill.textContent = S.stack_up ? (core.state === 'running' ? 'system on' : 'starting…') : 'system off';
    pill.className = 'pill' + (S.stack_up && core.state === 'running' ? ' up' : S.stack_up ? ' warn' : '');
    const a = S.access, b = $('banner');
    if (a.unprotected_public) {
      b.textContent = 'This panel is being reached through Cloudflare without Cloudflare Access. There is no login here - add an Access policy to the hostname, or anyone with the link controls the robot.';
      b.hidden = false;
    } else b.hidden = true;

    if (document.activeElement !== $('mode')) $('mode').value = s.mode;
    $('map-wrap').hidden = s.mode !== 'navigation';
    if (document.activeElement !== $('map-select')) fillMapSelect();
    $('btn-start').disabled = S.stack_up;
    $('btn-stop').disabled = !S.stack_up && Object.values(S.processes).every(p => p.state === 'stopped');
    $('btn-restart').disabled = !S.stack_up;

    const pending = Object.values(S.processes).filter(p => p.needs_restart).map(p => p.label);
    note('restart-note', pending.length ? 'Settings changed for: ' + pending.join(', ') + '. Restart to apply.' : '', 'warn');
    note('core-msg', core.message && core.state !== 'running' ? core.message : '', 'warn');

    const list = $('proc-list'); list.textContent = '';
    for (const [name, p] of Object.entries(S.processes)) {
      // Show every feature, so this page always agrees with the Components page:
      // green = running, grey = stopped, and switched-off features are labelled as such.
      const off = name !== 'core' && !p.enabled;
      const stateText = off && p.state === 'stopped' ? 'switched off'
        : off && p.state === 'running' ? 'running, switched off - stopping' : (STATE_TEXT[p.state] || p.state);
      list.append(h('div', { class: 'proc' + (off ? ' off' : '') },
        h('span', { class: 'dot ' + p.state }),
        h('span', { class: 'name' }, p.label),
        h('span', { class: 'state' }, stateText, p.uptime != null ? ' · ' + fmtUp(p.uptime) : ''),
        p.needs_restart ? h('span', { class: 'tag' }, 'restart needed') : null,
        p.message && p.state !== 'running' ? h('span', { class: 'pmsg' }, p.message) : null));
    }
    const hl = S.health, kv = $('health'); kv.textContent = '';
    const add = (k, v) => { if (v != null && v !== '') kv.append(h('div', {}, h('b', {}, k), v)); };
    add('Host', hl.hostname); add('Address', (hl.ips || []).join(', '));
    add('CPU temp', hl.cpu_temp_c != null ? hl.cpu_temp_c + ' °C' : null);
    add('Load', hl.load ? hl.load.join(' · ') : null);
    add('Memory used', hl.mem_used_pct != null ? hl.mem_used_pct + ' %' : null);
    add('Disk free', hl.disk_free_gb != null ? hl.disk_free_gb + ' GB' : null);
    add('Uptime', fmtUp(hl.uptime_s));
    if (a.access_user) add('Signed in (Access)', a.access_user);
  }

  let mapsCache = [];
  function fillMapSelect() {
    const sel = $('map-select'); sel.textContent = '';
    for (const m of mapsCache) sel.append(h('option', { value: m.id }, m.name + (m.source === 'saved' ? ' (saved)' : '')));
    if (S) sel.value = S.settings.map;
  }
  $('mode').addEventListener('change', () => act(() => api('/api/settings', { mode: $('mode').value })));
  $('map-select').addEventListener('change', () => act(() => api('/api/settings', { map: $('map-select').value })));
  $('btn-start').addEventListener('click', () => act(() => api('/api/stack', { action: 'start' }), 'Starting…'));
  $('btn-stop').addEventListener('click', () => act(() => api('/api/stack', { action: 'stop' }), 'Stopping…'));
  $('btn-restart').addEventListener('click', () => act(() => api('/api/stack', { action: 'restart' }), 'Restarting…'));
  $('estop').addEventListener('click', async () => {
    try { await api('/api/stop', {}); toast('STOP sent'); } catch (e) { toast('STOP failed: ' + e.message, true); }
  });

  // ── components ──
  let compSig = '';
  function procButtons(name) {
    const p = S.processes[name], on = p.state === 'running' || p.state === 'stopping';
    const locked = name !== 'core' && !p.enabled;      // switched off: only the switch can turn it on
    const tip = locked ? 'Turn the switch on first' : null;
    return h('div', { class: 'row actions' },
      h('button', { class: 'small', disabled: on || locked, title: tip, onclick: () => act(() => api('/api/component', { name, action: 'start' })) }, 'Start'),
      h('button', { class: 'small', disabled: !on, onclick: () => act(() => api('/api/component', { name, action: 'stop' })) }, 'Stop'),
      h('button', { class: 'small', disabled: !on || locked, title: tip, onclick: () => act(() => api('/api/component', { name, action: 'restart' })) }, 'Restart'),
      h('button', { class: 'small', onclick: () => { $('log-name').value = name; showTab('logs'); } }, 'Logs'));
  }
  function stateLine(name) {
    const p = S.processes[name];
    const off = name !== 'core' && !p.enabled;
    return h('p', {}, (off && p.state === 'stopped' ? 'switched off' : (STATE_TEXT[p.state] || p.state)) + (p.uptime != null ? ' · ' + fmtUp(p.uptime) : ''),
      p.message && p.state !== 'running' ? ' — ' + p.message : '', p.needs_restart ? ' — restart needed to apply changes' : '');
  }
  function toggle(checked, onchange) {
    return h('label', { class: 'switch' }, h('input', { type: 'checkbox', checked: checked, onchange }), h('span', {}));
  }
  function renderComponents(force) {
    if (!S) return;
    const sig = JSON.stringify([S.settings.components, S.stack_up, Object.entries(S.processes).map(([k, p]) => [k, p.state, p.needs_restart, p.message, p.uptime == null ? 0 : 1])]);
    if (!force && (sig === compSig || tab !== 'components')) return;
    compSig = sig;
    const box = $('comp-list'); box.textContent = '';
    const c = S.settings.components, v = c.voice;

    // Voice: one feature, one input. The mic and the browser page are never both on.
    const radio = (value, title, sub) => h('label', { class: 'check' },
      h('input', { type: 'radio', name: 'voice-input', value, checked: v.input === value,
        onchange: () => act(() => api('/api/settings', { components: { voice: { input: value } } }), 'Voice input switched') }),
      h('span', {}, h('b', {}, title), ' ', sub));
    box.append(h('div', { class: 'comp' },
      h('div', { class: 'comp-head' }, h('span', { class: 'name' }, 'Voice control (Lumi)'),
        toggle(v.enabled, ev => act(() => api('/api/component', { name: 'voice', action: ev.target.checked ? 'enable' : 'disable' })))),
      h('p', {}, 'Understands commands, drives, navigates by voice, answers questions (Gemini) and speaks.'),
      h('h2', {}, 'Voice input'),
      radio('mic', 'Robot microphone', '— listens locally. Best in a quiet place.'),
      radio('browser', 'Browser (phone or PC)', '— speech is recognised in your browser, the robot microphone is switched off. For noisy places.'),
      v.enabled && v.input === 'browser' ? h('p', {}, h('a', { href: '/voice/' }, 'Open the voice page ↗'), ' — ', STATE_TEXT[S.processes.remote.state] || S.processes.remote.state) : null,
      stateLine('voice'), procButtons('voice')));

    for (const name of ['rosbridge', 'eyes', 'rviz_bridge']) {
      const [title, desc] = COMP_INFO[name];
      box.append(h('div', { class: 'comp' },
        h('div', { class: 'comp-head' }, h('span', { class: 'name' }, title),
          toggle(c[name].enabled, ev => act(() => api('/api/component', { name, action: ev.target.checked ? 'enable' : 'disable' })))),
        h('p', {}, desc),
        name === 'rosbridge' && c.rosbridge.enabled ? h('p', {}, h('a', { href: '/robot/' }, 'Open the robot web ↗')) : null,
        name === 'rviz_bridge' ? h('p', {}, h('a', { href: '#', onclick: ev => { ev.preventDefault(); showTab('rviz'); } }, 'RViz2 setup →')) : null,
        stateLine(name), procButtons(name)));
    }
    box.append(h('div', { class: 'comp' }, h('div', { class: 'comp-head' }, h('span', { class: 'name' }, 'Robot core (' + S.settings.mode + ')')),
      h('p', {}, 'Lidar, motors/Arduino and, depending on the mode, mapping or navigation. Choose the mode and map on the Overview tab.'),
      stateLine('core'), procButtons('core')));
  }

  // ── maps ──
  async function loadMaps() {
    const d = await api('/api/maps').catch(e => { toast(e.message, true); return { maps: [] }; });
    mapsCache = d.maps; if (S) fillMapSelect();
    const grid = $('map-grid'); grid.textContent = '';
    const sel = S ? S.settings.map : d.selected;
    if (!mapsCache.length) grid.append(h('p', { class: 'muted' }, 'No maps yet. Start mapping mode and save one.'));
    for (const m of mapsCache) {
      grid.append(h('div', { class: 'mapcard' + (m.id === sel ? ' sel' : '') },
        h('img', { src: '/api/maps/preview?id=' + encodeURIComponent(m.id), alt: m.name, loading: 'lazy' }),
        h('div', { class: 'meta' }, h('b', {}, m.name), h('span', { class: 'muted' },
          (m.source === 'saved' ? 'saved on the robot' : 'built in') + (m.meters ? ' · ' + m.meters[0] + ' × ' + m.meters[1] + ' m' : ''))),
        h('div', { class: 'btns' },
          h('button', { class: 'small' + (m.id === sel ? '' : ' primary'), disabled: m.id === sel, onclick: () => useMap(m) }, m.id === sel ? 'In use' : 'Use for navigation'),
          m.source === 'saved' ? h('button', { class: 'small danger', onclick: () => delMap(m) }, 'Delete') : null)));
    }
  }
  async function useMap(m) {
    try {
      const running = S && S.settings.mode === 'navigation' && S.processes.core.state === 'running';
      const apply = running && confirm('Restart navigation now so it loads "' + m.name + '"?');
      await api('/api/maps/select', { id: m.id, apply });
      toast('Map selected'); await refresh(); loadMaps();
    } catch (e) { toast(e.message, true); }
  }
  async function delMap(m) {
    if (!confirm('Delete map "' + m.name + '"? This cannot be undone.')) return;
    try { await api('/api/maps/delete', { id: m.id }); toast('Deleted'); await refresh(); loadMaps(); } catch (e) { toast(e.message, true); }
  }
  $('save-map-form').addEventListener('submit', async ev => {
    ev.preventDefault(); note('save-map-msg', 'Saving…');
    const name = $('save-map-name').value.trim();
    try {
      await api('/api/maps/save', { name });
      note('save-map-msg', 'Saved as "' + name + '". Pick it below to navigate on it.', 'ok'); loadMaps();
    } catch (e) {
      if (e.status === 409 && confirm('A map called "' + name + '" exists. Overwrite it?')) {
        try { await api('/api/maps/save', { name, overwrite: true }); note('save-map-msg', 'Saved.', 'ok'); loadMaps(); return; } catch (e2) { e = e2; }
      }
      note('save-map-msg', e.message + (e.data && e.data.output ? '\n' + e.data.output : ''), 'err');
    }
  });
  $('pose-form').addEventListener('submit', async ev => {
    ev.preventDefault(); note('pose-msg', 'Sending…');
    try {
      await api('/api/nav/initial_pose', { x: +$('pose-x').value, y: +$('pose-y').value, yaw: +$('pose-yaw').value });
      note('pose-msg', 'Initial pose sent. Check the robot web map to confirm it localised.', 'ok');
    } catch (e) { note('pose-msg', e.message, 'err'); }
  });

  // ── Gemini key status (the panel remembers the last check of each key until it restarts) ──
  const KEYSTATE = { ok: 'Working', fallback: 'Fallback only', limited: 'Rate-limited', gone: 'Not offered', quota: 'Out of quota', busy: 'Google busy', invalid: 'Invalid', error: 'No answer' };
  const KEYSUM = { ok: 'working', fallback: 'fallback only', limited: 'rate-limited (valid)', gone: 'model not offered', quota: 'out of quota', busy: 'Google busy', invalid: 'invalid', error: 'no answer' };
  function ago(t) {
    const s = Math.max(0, Math.round(Date.now() / 1000 - t));
    return s < 60 ? 'just now' : s < 3600 ? Math.round(s / 60) + ' min ago' : Math.round(s / 3600) + ' h ago';
  }
  function keyBadge(st) {
    return h('span', { class: 'keystate ' + (st ? st.state : 'unknown'),
      title: st ? st.detail + ' (checked ' + ago(st.checked) + ')' : 'Not checked yet - press Check all keys' },
      st ? (KEYSTATE[st.state] || st.state) : 'Not checked');
  }
  const MODELWORD = { ok: 'working', limited: 'rate-limited', quota: 'out of quota', busy: 'busy', gone: 'not offered', invalid: 'invalid', error: 'no answer' };
  function keyModels(st, models) {
    const by = {};
    if (st && st.models) st.models.forEach(m => { by[m.model] = m.state; });
    return h('small', { class: 'keymodels' }, (models || []).map(m =>
      m.replace(/^gemini-/, '') + ': ' + (by[m] ? (MODELWORD[by[m]] || by[m]) : 'not tested')).join('   \u00b7   '));
  }
  function keySummary(keys) {
    const c = {}; let newest = 0;
    keys.forEach(k => { if (k.status) { c[k.status.state] = (c[k.status.state] || 0) + 1; newest = Math.max(newest, k.status.checked); } });
    const parts = Object.keys(KEYSUM).filter(x => c[x]).map(x => c[x] + ' ' + KEYSUM[x]);
    return parts.length ? parts.join(' \u00b7 ') + '  (checked ' + ago(newest) + ')' : '';
  }

  // ── settings ──
  async function loadEnv() {
    if (!S) await refresh();
    const d = await api('/api/env').catch(e => { toast(e.message, true); return null; });
    if (!d) return;
    const gl = $('gemini-list'); gl.textContent = '';
    if (!d.gemini.length) gl.append(h('p', { class: 'muted' }, 'No keys yet - voice answers will be unavailable.'));
    d.gemini.forEach((k, i) => gl.append(h('div', { class: 'item' },
      h('span', { class: 'grow' }, '#' + (i + 1) + '  ' + k.masked, keyModels(k.status, d.gemini_models)),
      keyBadge(k.status),
      h('button', { class: 'small', onclick: async ev => {
        ev.target.disabled = true; ev.target.textContent = '…';
        try { const r = await api('/api/gemini/test', { id: k.id }); toast(r.detail, !r.ok); await loadEnv(); } catch (e) { toast(e.message, true); }
        ev.target.disabled = false; ev.target.textContent = 'Test';
      } }, 'Test'),
      h('button', { class: 'small danger', onclick: () => { if (confirm('Remove this key?')) act(async () => { await api('/api/gemini/remove', { id: k.id }); loadEnv(); }, 'Key removed'); } }, 'Remove'))));
    $('gemini-summary').textContent = keySummary(d.gemini);
    const ms = d.gemini_models || [];
    $('gemini-models').textContent = 'The robot tries these models in order: ' + ms.join(', then ') + '. A check uses one request per working key ' +
      'from the free daily quota of the first model (about 20 a day per Google project); testing the fallback spends its quota too, so it is off by default.';
    if (S && S.env_imported && S.env_imported.length) note('gemini-msg', 'On first run these were imported from ~/.bashrc: ' + S.env_imported.join(', '));
    $('env-prefixes').textContent = d.prefixes.join(' ');
    const el = $('env-list'); el.textContent = '';
    for (const v of d.vars) el.append(h('div', { class: 'item' },
      h('span', { class: 'grow' }, v.name + '=' + v.value),
      h('button', { class: 'small', onclick: () => { $('env-name').value = v.name; $('env-value').value = v.secret ? '' : v.value; $('env-value').focus(); } }, 'Edit'),
      h('button', { class: 'small danger', onclick: () => { if (confirm('Remove ' + v.name + '?')) act(async () => { await api('/api/env/delete', { name: v.name }); loadEnv(); }, 'Removed'); } }, 'Remove')));
    renderLaunch(); renderBehaviour(true);
  }
  $('gemini-check-all').addEventListener('click', async ev => {
    const btn = ev.currentTarget, label = btn.textContent;
    btn.disabled = true; btn.textContent = 'Checking\u2026';
    $('gemini-summary').textContent = 'Asking Google about each key\u2026';
    try { await api('/api/gemini/test_all', { fallback: $('gemini-check-fallback').checked }); await loadEnv(); }
    catch (e) { toast(e.message, true); $('gemini-summary').textContent = ''; }
    btn.disabled = false; btn.textContent = label;
  });
  $('gemini-form').addEventListener('submit', async ev => {
    ev.preventDefault();
    try { await api('/api/gemini/add', { key: $('gemini-key').value }); $('gemini-key').value = ''; note('gemini-msg', 'Key added. Restart voice to use it.', 'ok'); loadEnv(); refresh(); }
    catch (e) { note('gemini-msg', e.message, 'err'); }
  });
  $('env-form').addEventListener('submit', async ev => {
    ev.preventDefault();
    try { await api('/api/env', { name: $('env-name').value.trim().toUpperCase(), value: $('env-value').value }); $('env-name').value = ''; $('env-value').value = ''; note('env-msg', 'Saved. Restart the affected component to apply.', 'ok'); loadEnv(); refresh(); }
    catch (e) { note('env-msg', e.message, 'err'); }
  });

  function renderLaunch() {
    const box = $('launch-fields'); box.textContent = '';
    for (const [k, meta] of Object.entries(S.launch_schema)) {
      const val = S.settings.launch[k];
      let input;
      if (meta.type === 'bool') input = h('input', { type: 'checkbox', 'data-k': k, checked: !!val });
      else if (meta.type === 'dev') input = h('input', { type: 'text', 'data-k': k, value: val, spellcheck: 'false' });
      else input = h('input', { type: 'number', 'data-k': k, value: val, step: meta.type === 'int' ? 1 : 'any', min: meta.min, max: meta.max });
      box.append(h('label', { class: meta.type === 'bool' ? 'check' : '' }, meta.type === 'bool' ? [input, meta.label] : [meta.label, input]));
    }
  }
  $('launch-form').addEventListener('submit', async ev => {
    ev.preventDefault();
    const launch = {};
    $('launch-fields').querySelectorAll('input[data-k]').forEach(i => {
      launch[i.dataset.k] = i.type === 'checkbox' ? i.checked : i.type === 'number' ? Number(i.value) : i.value.trim();
    });
    try { await api('/api/settings', { launch }); note('launch-msg', 'Saved. Restart the system to apply.', 'ok'); refresh(); }
    catch (e) { note('launch-msg', e.message, 'err'); }
  });

  function renderBehaviour(force) {
    if (!S || (!force && document.activeElement && ['opt-hosts'].includes(document.activeElement.id))) return;
    $('opt-autostart').checked = S.settings.autostart;
    $('opt-autorestart').checked = S.settings.auto_restart;
    $('opt-lan').checked = S.settings.listen_lan;
    if (force) $('opt-hosts').value = S.settings.allowed_hosts.join('\n');
  }
  $('save-behaviour').addEventListener('click', async () => {
    try {
      await api('/api/settings', {
        autostart: $('opt-autostart').checked, auto_restart: $('opt-autorestart').checked, listen_lan: $('opt-lan').checked,
        allowed_hosts: $('opt-hosts').value.split('\n').map(x => x.trim()).filter(Boolean) });
      note('behaviour-msg', 'Saved.' + (S.settings.listen_lan !== $('opt-lan').checked ? ' Restart the panel to apply the network change.' : ''), 'ok'); refresh();
    } catch (e) { note('behaviour-msg', e.message, 'err'); }
  });
  $('restart-panel').addEventListener('click', async () => {
    if (!confirm('Restart the panel? The robot system stops with it' + (S.settings.autostart ? ' and starts again afterwards.' : ' and stays off until you start it.'))) return;
    try { await api('/api/panel/restart', {}); toast('Restarting… reload in a few seconds'); } catch (e) { toast(e.message, true); }
  });

  // ── logs ──
  async function loadLogs() {
    if (!S) await refresh();
    const sel = $('log-name');
    if (!sel.options.length) for (const [name, p] of Object.entries(S.processes)) sel.append(h('option', { value: name }, p.label));
    await fetchLog();
  }
  async function fetchLog() {
    const v = $('log-view');
    try {
      const d = await api('/api/logs?name=' + encodeURIComponent($('log-name').value) + '&lines=300' + ($('log-prev').checked ? '&previous=1' : ''));
      const atBottom = v.scrollHeight - v.scrollTop - v.clientHeight < 40;
      v.textContent = d.text.replace(/\x1b\[[0-9;]*m/g, '') || '(empty)';
      if (atBottom) v.scrollTop = v.scrollHeight;
    } catch (e) { v.textContent = e.message; }
  }
  $('log-name').addEventListener('change', fetchLog);
  $('log-prev').addEventListener('change', fetchLog);
  setInterval(() => { if (tab === 'logs' && $('log-follow').checked && !document.hidden && !$('log-prev').checked) fetchLog(); }, 2000);

  // ── rviz ──
  let rviz = null;
  async function loadRviz() {
    if (!S) await refresh();
    rviz = await api('/api/rviz').catch(e => { toast(e.message, true); return null; });
    if (!rviz) return;
    const kv = $('rviz-status'); kv.textContent = '';
    const p = S.processes.rviz_bridge;
    const add = (k, v) => kv.append(h('div', {}, h('b', {}, k), v));
    add('Bridge installed', rviz.installed ? 'yes (v' + rviz.version + ')' : 'no');
    add('Bridge', STATE_TEXT[p.state] || p.state); add('Local port', '127.0.0.1:' + rviz.port);
    add('ROS domain (other machine)', String(rviz.domain));
    $('rviz-install').hidden = rviz.installed;
    const en = S.settings.components.rviz_bridge.enabled;
    $('rviz-toggle').textContent = en ? 'Disable' : 'Enable';
    if (document.activeElement !== $('rviz-host')) $('rviz-host').value = rviz.hostname;
    if (document.activeElement !== $('rviz-domain')) $('rviz-domain').value = rviz.domain;
    updateRvizLinks();
    note('rviz-msg', p.message && p.state !== 'running' ? p.message : '', 'warn');
  }
  $('rviz-install').addEventListener('click', async () => {
    note('rviz-msg', 'Downloading the bridge (about 10 MB)…'); $('rviz-install').disabled = true;
    try { const r = await api('/api/rviz/install', {}); note('rviz-msg', r.message, 'ok'); } catch (e) { note('rviz-msg', e.message, 'err'); }
    $('rviz-install').disabled = false; loadRviz();
  });
  $('rviz-toggle').addEventListener('click', () => act(async () => {
    await api('/api/component', { name: 'rviz_bridge', action: S.settings.components.rviz_bridge.enabled ? 'disable' : 'enable' }); await loadRviz();
  }));
  // The download is built on the server from the CURRENT field values, saved or not.
  function updateRvizLinks() {
    const host = $('rviz-host').value.trim() || 'zenoh.example.com';
    const dom = String(parseInt($('rviz-domain').value, 10) || 0);
    $('rviz-host-echo').textContent = host;
    $('rviz-cmd').textContent = 'bash lumi-rviz.sh' + ($('rviz-host').value.trim() ? '' : ' ' + host);
    $('rviz-dl').href = '/rviz/lumi-rviz.sh?host=' + encodeURIComponent($('rviz-host').value.trim()) + '&domain=' + encodeURIComponent(dom);
  }
  $('rviz-host').addEventListener('input', updateRvizLinks);
  $('rviz-domain').addEventListener('input', updateRvizLinks);
  $('rviz-save').addEventListener('click', () => act(async () => {
    await api('/api/settings', { rviz: { hostname: $('rviz-host').value.trim(), domain: parseInt($('rviz-domain').value, 10) || 0 } });
    await loadRviz();
  }, 'Saved. Restart the bridge to apply.'));

  // ── boot ──
  refresh().then(() => api('/api/maps').then(d => { mapsCache = d.maps; fillMapSelect(); }).catch(() => {}));
})();
