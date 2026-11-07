// Does the robot web page receive the map from the LIVE rosbridge?  (needs the robot running; no browser needed)
//
//   node scripts/check_map_delivery.js [path/to/lumi-core.js] [ws://127.0.0.1:9090]
//
// Loads the page's own lumi-core.js with a tiny fake DOM and the real roslib, connects, and waits for the map to show up
// in the "Map size" field. Handy when the dashboards show "Waiting for /map": it says whether the page gets the map.
'use strict';
const fs = require('fs');
const path = require('path');
const core = path.resolve(process.argv[2] || path.join(__dirname, '..', 'src', 'omni_base', 'web_interface', 'lumi-core.js'));
const url = process.argv[3] || 'ws://127.0.0.1:9090';

const fields = {};                                              // [data-lumi="x"] -> the fake element the core writes into
const el = (sel) => fields[sel] || (fields[sel] = { textContent: '', setAttribute() {}, classList: { add() {}, remove() {}, toggle() {} }, addEventListener() {},
                                                     appendChild() {}, children: [], style: {} });
const body = { setAttribute() {}, getAttribute() { return null; }, hasAttribute() { return false; }, appendChild() {}, classList: { add() {}, remove() {}, toggle() {} } };
global.window = global;
global.document = { body, readyState: 'complete', addEventListener() {}, getElementById: () => null, querySelector: () => null,
                    querySelectorAll: (sel) => (/data-lumi="(mapsize|mapstat)"/.test(sel) ? [el(sel)] : []), createElement: () => el('x'), documentElement: {} };
global.localStorage = { getItem: (k) => (k === 'lumi.rosbridge.url' ? url : null), setItem() {} };
global.location = { protocol: 'ws:', host: '127.0.0.1', hostname: '127.0.0.1', pathname: '/x' };
global.matchMedia = () => ({ matches: false });
global.addEventListener = () => {};
require(path.join(__dirname, '..', 'src', 'omni_base', 'web_interface', 'lib', 'roslib.min.js'));
// eslint-disable-next-line no-eval
(0, eval)(fs.readFileSync(core, 'utf8'));

const t0 = Date.now(), secs = () => ((Date.now() - t0) / 1000).toFixed(1);
const size = () => (fields['[data-lumi="mapsize"]'] || {}).textContent || '';
window.LUMI.init({ canvas: 'none', autoConnect: false });
window.LUMI.connect();
const tick = setInterval(() => {
  if (/\d+ x \d+/.test(size())) { console.log(`${secs()}s  MAP RECEIVED: ${size()}   (${path.basename(path.dirname(core))}/${path.basename(core)})`); clearInterval(tick); process.exit(0); }
  if (Date.now() - t0 > 15000) { console.log(`${secs()}s  NO MAP after 15 s   (${path.basename(core)})`); process.exit(1); }
}, 250);
