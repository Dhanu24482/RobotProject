#!/usr/bin/env python3
"""
Headless smoke test for the web control panel layouts.

Runs web_interface/lumi-core.js inside a real JS engine against a synthetic DOM
built from each layout's markup, then exercises the declarative bindings the
same way a finger would. Catches the class of bug a syntax check cannot: an
exception thrown during init that silently kills every control on the page.

    uv run --python 3.12 --with py-mini-racer python scripts/check_web_layouts.py
"""

import glob
import json
import os
import sys
from html.parser import HTMLParser

from py_mini_racer import MiniRacer

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WEB = os.path.join(ROOT, "src", "omni_base", "web_interface")

VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input",
        "link", "meta", "param", "source", "track", "wbr"}


class DomBuilder(HTMLParser):
    """Turns layout markup into a nested dict tree, skipping style/script."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.root = {"tag": "#root", "attrs": {}, "children": []}
        self.stack = [self.root]
        self.skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in ("style", "script"):
            self.skip += 1
            return
        if self.skip:
            return
        node = {"tag": tag, "attrs": {k: (v if v is not None else "")
                                      for k, v in attrs}, "children": []}
        self.stack[-1]["children"].append(node)
        if tag not in VOID:
            self.stack.append(node)

    def handle_endtag(self, tag):
        if tag in ("style", "script"):
            self.skip = max(0, self.skip - 1)
            return
        if self.skip or tag in VOID:
            return
        for i in range(len(self.stack) - 1, 0, -1):
            if self.stack[i]["tag"] == tag:
                del self.stack[i:]
                break


# Minimal DOM/browser shim: only what lumi-core.js actually touches.
SHIM = r"""
var __errors = [], __log = [];
function __rec(e) { __errors.push(String(e && e.stack || e)); }

function El(tag, attrs) {
  this.tagName = (tag || 'div').toUpperCase();
  this.attrs = attrs || {};
  this.children = [];
  this.parentElement = null;
  this.listeners = {};
  this.style = {};
  this.textContent = '';
  this._html = '';
  this.value = this.attrs.value || '';
  this.clientWidth = 800; this.clientHeight = 600;
  this.width = 0; this.height = 0;
  var self = this;
  this.classList = {
    _s: (this.attrs['class'] || '').split(/\s+/).filter(Boolean),
    add: function (c) { if (this._s.indexOf(c) < 0) this._s.push(c); self.attrs['class'] = this._s.join(' '); },
    remove: function (c) { var i = this._s.indexOf(c); if (i >= 0) this._s.splice(i, 1); self.attrs['class'] = this._s.join(' '); },
    contains: function (c) { return this._s.indexOf(c) >= 0; },
    toggle: function (c, on) { on ? this.add(c) : this.remove(c); }
  };
}
El.prototype.setAttribute = function (k, v) { this.attrs[k] = String(v); };
El.prototype.getAttribute = function (k) { return k in this.attrs ? this.attrs[k] : null; };
El.prototype.hasAttribute = function (k) { return k in this.attrs; };
El.prototype.addEventListener = function (t, fn) { (this.listeners[t] = this.listeners[t] || []).push(fn); };
El.prototype.removeEventListener = function () {};
El.prototype.setPointerCapture = function () {};
El.prototype.releasePointerCapture = function () {};
El.prototype.appendChild = function (c) { c.parentElement = this; this.children.push(c); return c; };
El.prototype.removeChild = function (c) { var i = this.children.indexOf(c); if (i >= 0) this.children.splice(i, 1); };
El.prototype.getBoundingClientRect = function () { return { left: 0, top: 0, width: this.clientWidth, height: this.clientHeight }; };
El.prototype.getContext = function () { return __ctx2d; };
El.prototype.fire = function (t, ev) {
  var ls = this.listeners[t] || [];
  ev = ev || {};
  ev.preventDefault = ev.preventDefault || function () {};
  ev.target = ev.target || this;
  for (var i = 0; i < ls.length; i++) ls[i].call(this, ev);
  return ls.length;
};
Object.defineProperty(El.prototype, 'firstChild', { get: function () { return this.children[0] || null; } });
Object.defineProperty(El.prototype, 'innerHTML', {
  get: function () { return this._html; },
  set: function (v) { this._html = v; this.children = []; }
});

var __ctx2d = new Proxy({}, {
  get: function (t, k) {
    if (k === 'canvas') return null;
    return function () { return { data: new Array(4) }; };
  },
  set: function () { return true; }
});

function walk(el, out) {
  out.push(el);
  for (var i = 0; i < el.children.length; i++) walk(el.children[i], out);
  return out;
}

// Supports exactly the selector shapes lumi-core.js uses.
function matches(el, sel) {
  var parts = sel.match(/\[[^\]]+\]|#[\w-]+|\.[\w-]+/g) || [];
  for (var i = 0; i < parts.length; i++) {
    var p = parts[i], m;
    if (p[0] === '#') { if (el.attrs.id !== p.slice(1)) return false; }
    else if (p[0] === '.') { if (!el.classList.contains(p.slice(1))) return false; }
    else if ((m = p.match(/^\[([\w-]+)="([^"]*)"\]$/))) { if (el.attrs[m[1]] !== m[2]) return false; }
    else if ((m = p.match(/^\[([\w-]+)\]$/))) { if (!(m[1] in el.attrs)) return false; }
    else return false;
  }
  return parts.length > 0;
}

function buildTree(spec, parent) {
  var el = new El(spec.tag, Object.assign({}, spec.attrs));
  if (parent) parent.appendChild(el);
  for (var i = 0; i < spec.children.length; i++) buildTree(spec.children[i], el);
  return el;
}

var __docRoot = null, __body = null;
var document = {
  createElement: function (t) { return new El(t, {}); },
  getElementById: function (id) {
    var all = walk(__docRoot, []);
    for (var i = 0; i < all.length; i++) if (all[i].attrs.id === id) return all[i];
    return null;
  },
  querySelectorAll: function (sel) {
    return walk(__docRoot, []).filter(function (e) { return matches(e, sel); });
  },
  querySelector: function (sel) { return document.querySelectorAll(sel)[0] || null; },
  addEventListener: function () {},
  get body() { return __body; },
  hidden: false,
  fullscreenElement: null,
  documentElement: { requestFullscreen: function () { return { catch: function () {} }; } }
};
El.prototype.querySelector = function (sel) {
  var f = walk(this, []).filter(function (e) { return e !== this && matches(e, sel); }, this);
  return f[0] || null;
};

var __storage = {};
var localStorage = {
  getItem: function (k) { return k in __storage ? __storage[k] : null; },
  setItem: function (k, v) { __storage[k] = String(v); }
};
var location = { hostname: '127.0.0.1' };
var devicePixelRatio = 1;
var __timers = 0;
function setTimeout() { return ++__timers; }
function clearTimeout() {}
function setInterval() { return ++__timers; }
function clearInterval() {}
function getComputedStyle() { return { getPropertyValue: function () { return ''; } }; }
function prompt() { return 'test spot'; }
var console = { warn: function () { __log.push(Array.prototype.join.call(arguments, ' ')); },
                log: function () {}, error: function () {} };
var window = globalThis;
window.addEventListener = function () {};
window.devicePixelRatio = 1;
window.location = location;
window.localStorage = localStorage;
window.setTimeout = setTimeout; window.clearTimeout = clearTimeout;
window.setInterval = setInterval; window.clearInterval = clearInterval;
window.getComputedStyle = getComputedStyle;
window.prompt = prompt;
window.document = document;
window.console = console;
"""


def build_page(spec):
    return (
        "__docRoot = buildTree(%s, null);\n"
        "var __b = document.querySelectorAll('body')[0];\n"
        "__body = __b || __docRoot;\n" % json.dumps(spec)
    )


EXERCISE = r"""
var report = { errors: __errors, checks: [] };
function check(name, fn) {
  try { report.checks.push({ name: name, result: String(fn()) }); }
  catch (e) { report.checks.push({ name: name, result: 'THREW: ' + (e && e.message || e) }); __rec(e); }
}

check('LUMI defined', function () { return typeof LUMI; });
check('init()', function () { LUMI.init({ canvas: 'mapCanvas', autoConnect: false }); return 'completed'; });
check('room buttons rendered', function () {
  var h = document.querySelectorAll('[data-lumi="room-buttons"]');
  return h.length ? h[0].children.length + ' buttons' : 'n/a (uses select)';
});
check('room select options', function () {
  var s = document.querySelectorAll('[data-lumi="rooms"]');
  return s.length ? s[0].children.length + ' options' : 'n/a (uses buttons)';
});
check('drive buttons bound', function () {
  var d = document.querySelectorAll('[data-drive]');
  var bound = d.filter(function (e) { return (e.listeners.pointerdown || []).length > 0; });
  return bound.length + '/' + d.length;
});
check('action buttons bound', function () {
  var a = document.querySelectorAll('[data-action]');
  var bound = a.filter(function (e) { return (e.listeners.click || []).length > 0; });
  return bound.length + '/' + a.length;
});
check('mode buttons bound', function () {
  var a = document.querySelectorAll('[data-mode]');
  var bound = a.filter(function (e) { return (e.listeners.click || []).length > 0; });
  return bound.length + '/' + a.length;
});
check('emotion buttons bound', function () {
  var a = document.querySelectorAll('[data-emotion]');
  var bound = a.filter(function (e) { return (e.listeners.click || []).length > 0; });
  return bound.length + '/' + a.length;
});
check('click Connect (no ROSLIB present)', function () {
  var b = document.querySelectorAll('[data-action="connect"]')[0];
  if (!b) return 'no connect button';
  b.fire('click');
  return 'handled';
});
check('click a mode button', function () {
  var b = document.querySelectorAll('[data-mode="teleop"]')[0];
  if (!b) return 'none';
  b.fire('click');
  return 'mode class = ' + (b.classList.contains('act') ? 'act' : 'NOT SET');
});
check('press a drive button', function () {
  var b = document.querySelectorAll('[data-drive]')[0];
  if (!b) return 'none';
  b.fire('pointerdown', { pointerId: 1 });
  b.fire('pointerup', { pointerId: 1 });
  return 'handled';
});
check('offline banner injected', function () {
  var b = __body.children[__body.children.length - 1];
  return b && b.style.cssText && b.style.display === 'flex' ? 'shown' : 'MISSING';
});
check('canvas pointer goal', function () {
  var c = document.getElementById('mapCanvas');
  if (!c) return 'no canvas';
  c.fire('pointerdown', { pointerId: 1, clientX: 100, clientY: 100, button: 0 });
  c.fire('pointerup', { pointerId: 1, clientX: 140, clientY: 120, button: 0 });
  return 'handled';
});
JSON.stringify(report);
"""


def main():
    core = open(os.path.join(WEB, "lumi-core.js"), encoding="utf-8").read()
    layouts = sorted(glob.glob(os.path.join(WEB, "layouts", "*.html")))
    layouts = [p for p in layouts if os.path.basename(p) != "index.html"]

    failed = False
    for path in layouts:
        name = os.path.basename(path)
        parser = DomBuilder()
        parser.feed(open(path, encoding="utf-8").read())

        ctx = MiniRacer()
        try:
            ctx.eval(SHIM)
            ctx.eval(build_page(parser.root))
            ctx.eval(core)
            report = json.loads(ctx.eval(EXERCISE))
        except Exception as exc:
            print(f"\n=== {name} ===\n  FATAL: {exc}")
            failed = True
            continue

        print(f"\n=== {name} ===")
        for c in report["checks"]:
            bad = "THREW" in c["result"] or "NOT SET" in c["result"]
            failed = failed or bad
            print(f"  {'FAIL' if bad else 'ok  '}  {c['name']}: {c['result']}")
        for e in report["errors"]:
            failed = True
            print(f"  ERROR: {e}")

    print("\nRESULT:", "FAILURES FOUND" if failed else "all layouts initialise cleanly")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
