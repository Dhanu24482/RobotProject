/* Layout picker: remembers a chosen layout on this device so a wall tablet opens straight into it.
   Add ?choose to the address (every layout has a "Layouts" link that does) to see the picker again. */
(function () {
  'use strict';
  var KEY = 'lumi.layout';
  var LAYOUTS = ['kiosk', 'immersive', 'console', 'split', 'dashboard', 'minimal'];
  function get() { try { return localStorage.getItem(KEY) || ''; } catch (e) { return ''; } }
  function set(v) { try { if (v) localStorage.setItem(KEY, v); else localStorage.removeItem(KEY); } catch (e) { /* private mode */ } }

  var saved = get();
  if (LAYOUTS.indexOf(saved) >= 0 && !/[?&]choose\b/.test(location.search)) {
    location.replace(saved + '.html');
    return;
  }

  // fullscreen (the layouts get this from lumi-core.js; the picker does not load the core)
  function fsEl() { return document.fullscreenElement || document.webkitFullscreenElement || null; }
  function toggleFs() {
    var de = document.documentElement;
    if (fsEl()) { (document.exitFullscreen || document.webkitExitFullscreen).call(document); return; }
    var req = de.requestFullscreen || de.webkitRequestFullscreen;
    if (req) { var r = req.call(de); if (r && r.catch) r.catch(function () {}); }
  }
  var canFs = !!(document.fullscreenEnabled || document.webkitFullscreenEnabled);
  function syncFs() { document.body.setAttribute('data-fullscreen', canFs ? (fsEl() ? 'on' : 'off') : 'unsupported'); }
  document.addEventListener('fullscreenchange', syncFs);
  document.addEventListener('webkitfullscreenchange', syncFs);
  document.addEventListener('keydown', function (e) {
    if ((e.key === 'f' || e.key === 'F') && !e.ctrlKey && !e.metaKey && !e.altKey) toggleFs();
  });

  document.addEventListener('DOMContentLoaded', function () {
    syncFs();
    Array.prototype.forEach.call(document.querySelectorAll('[data-action="fullscreen"]'), function (b) { b.addEventListener('click', toggleFs); });
    var box = document.getElementById('remember');
    if (box) box.checked = LAYOUTS.indexOf(saved) >= 0;
    Array.prototype.forEach.call(document.querySelectorAll('[data-layout]'), function (a) {
      if (a.getAttribute('data-layout') === saved) a.classList.add('current');
      a.addEventListener('click', function () { set(box && box.checked ? a.getAttribute('data-layout') : ''); });
    });
  });
})();
