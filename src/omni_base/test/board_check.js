// Runs under Node (no browser): `node board_check.js` exits non-zero on the first failed check.
// Covers web_remote/board.js: what it draws from a picture spec, and that nothing hostile can get through.
'use strict';
const assert = require('assert');
const path = require('path');
const B = require(path.join(__dirname, '..', 'web_remote', 'board.js'));

class El {
  constructor(tag) { this.tag = tag; this.attrs = {}; this.children = []; this.textContent = ''; this.style = { props: {}, setProperty(k, v) { this.props[k] = v; } }; }
  setAttribute(k, v) { this.attrs[k] = String(v); }
  appendChild(c) { this.children.push(c); return c; }
}
const doc = { createElementNS: (ns, tag) => new El(tag), createElement: (tag) => new El(tag) };
const all = (n) => [n].concat(...n.children.map(all));
const tags = (svg) => svg.children.map((c) => c.tag);

let n = 0;
function test(name, fn) { fn(); n++; }

const CIRCLE = { title: 'Circumference of a circle', seconds: 30,
  shapes: [{ kind: 'circle', x: 50, y: 52, r: 34, color: 'accent', fill: true },
           { kind: 'arc', x: 50, y: 52, r: 34, start: 200, end: 340, color: 'green' },
           { kind: 'dot', x: 50, y: 52 },
           { kind: 'arrow', x: 50, y: 52, x2: 84, y2: 52, color: 'yellow' },
           { kind: 'text', x: 68, y: 45, text: 'r', size: 'large', color: 'yellow' },
           { kind: 'text', x: 50, y: 9, text: 'C', size: 'large', color: 'green' }],
  formula: 'C = 2πr', notes: ['r is the radius', 'π ≈ 3.14159', 'r = 5 gives C ≈ 31.4'] };

test('draws the circumference picture', () => {
  const b = B.build(CIRCLE, doc);
  assert.strictEqual(b.count, 6);
  assert.deepStrictEqual(tags(b.svg), ['circle', 'path', 'circle', 'g', 'text', 'text']);
  assert.strictEqual(b.svg.attrs.viewBox, '0 0 100 100');
  assert.strictEqual(b.svg.attrs['aria-label'], 'Circumference of a circle');
  assert.strictEqual(b.formula, 'C = 2πr');
  assert.deepStrictEqual(b.notes, ['r is the radius', 'π ≈ 3.14159', 'r = 5 gives C ≈ 31.4']);
  assert.strictEqual(b.seconds, 30);
  const letters = b.svg.children.filter((c) => c.tag === 'text').map((c) => c.textContent);
  assert.deepStrictEqual(letters, ['r', 'C']);
});

test('shapes are staggered so they draw one after another', () => {
  const b = B.build(CIRCLE, doc);
  assert.deepStrictEqual(b.svg.children.map((c) => c.style.props['--i']), ['0', '1', '2', '3', '4', '5']);
});

test('an arrow is a line plus a head, dashed shapes fade instead of drawing', () => {
  const b = B.build({ shapes: [{ kind: 'arrow', x: 10, y: 10, x2: 90, y2: 10 }, { kind: 'line', x: 1, y: 1, x2: 9, y2: 9, dashed: true },
                               { kind: 'line', x: 1, y: 1, x2: 9, y2: 9 }] }, doc);
  assert.deepStrictEqual(b.svg.children[0].children.map((c) => c.tag), ['line', 'polygon']);
  assert.ok(/dash fade/.test(b.svg.children[1].attrs.class));
  assert.ok(/ draw/.test(b.svg.children[2].attrs.class));
  assert.ok(/pathLength/.test(Object.keys(b.svg.children[2].attrs).join()));
});

test('arcs: sweep, large-arc flag, and a full turn becomes a circle', () => {
  const half = B.build({ shapes: [{ kind: 'arc', x: 50, y: 50, r: 20, start: 0, end: 90 }] }, doc).svg.children[0];
  assert.strictEqual(half.tag, 'path');
  assert.ok(/^M70 50 A20 20 0 0 1 50 70$/.test(half.attrs.d), half.attrs.d);
  const big = B.build({ shapes: [{ kind: 'arc', x: 50, y: 50, r: 20, start: 0, end: 270 }] }, doc).svg.children[0];
  assert.ok(/ 0 1 1 /.test(big.attrs.d), big.attrs.d);
  const back = B.build({ shapes: [{ kind: 'arc', x: 50, y: 50, r: 20, start: 90, end: 0 }] }, doc).svg.children[0];
  assert.ok(/ 0 0 0 /.test(back.attrs.d), back.attrs.d);
  assert.strictEqual(B.build({ shapes: [{ kind: 'arc', x: 50, y: 50, r: 20, start: 0, end: 400 }] }, doc).svg.children[0].tag, 'circle');
});

test('numbers are clamped into the drawing area', () => {
  const c = B.build({ shapes: [{ kind: 'circle', x: -50, y: 900, r: 5000 }] }, doc).svg.children[0].attrs;
  assert.deepStrictEqual([c.cx, c.cy, c.r], ['0', '100', '60']);
});

test('bad numbers drop the shape, they never reach the page', () => {
  for (const bad of ['abc', null, NaN, Infinity, [], {}, true, '', '  ', undefined]) {
    const b = B.build({ shapes: [{ kind: 'circle', x: bad, y: 5, r: 5 }], formula: 'x' }, doc);
    assert.strictEqual(b.count, 0, 'accepted ' + String(bad));
  }
  assert.strictEqual(B.build({ shapes: [{ kind: 'dot', x: '12.5', y: '70' }] }, doc).count, 1);
});

test('only the known shape kinds are ever created', () => {
  const b = B.build({ shapes: [{ kind: 'script', x: 1, y: 1 }, { kind: 'foreignObject' }, { kind: 'image', href: 'http://evil' },
                               { kind: 'use', href: '#x' }, { kind: '__proto__' }, { kind: 'constructor' }, null, 5, 'circle', [],
                               { kind: 'circle', x: 5, y: 5, r: 5 }] }, doc);
  assert.strictEqual(b.count, 1);
  assert.deepStrictEqual(all(b.svg).map((e) => e.tag), ['svg', 'circle']);
});

test('no event handlers, links or styles can be smuggled in through the data', () => {
  const evil = { title: '<img src=x onerror=alert(1)>', formula: '<b>x</b>',
    shapes: [{ kind: 'circle', x: 5, y: 5, r: 5, color: 'javascript:alert(1)', onclick: 'evil()', href: 'http://x', style: 'x', class: 'y' },
             { kind: 'text', x: 5, y: 5, text: '<script>alert(1)</script>', size: '" onload="evil()' },
             { kind: 'arrow', x: 5, y: 5, x2: 60, y2: 60, color: 'red"><script>' }] };
  const b = B.build(evil, doc);
  for (const e of all(b.svg)) {
    for (const k of Object.keys(e.attrs)) {
      assert.ok(!/^on/i.test(k) && k !== 'href' && k !== 'xlink:href' && k !== 'style', 'bad attribute ' + k);
      assert.ok(!/[<>"]/.test(e.attrs[k].replace(/^.*aria-label.*$/, '')) || k === 'aria-label', 'markup in attribute ' + k + '=' + e.attrs[k]);
    }
  }
  const classes = all(b.svg).map((e) => e.attrs.class || '').join(' ');
  assert.ok(!/javascript|script|evil|onload/.test(classes), classes);
  const text = b.svg.children.find((c) => c.tag === 'text');
  assert.strictEqual(text.textContent, '<script>alert(');           // truncated, and set as plain text only
  assert.strictEqual(b.title, '<img src=x onerror=alert(1)>');     // still only ever given to textContent by show()
});

test('limits: shapes, polygon points, notes, lengths', () => {
  const many = B.build({ title: 'T'.repeat(500), formula: 'F'.repeat(500), notes: Array(9).fill('n'.repeat(500)),
    shapes: Array.from({ length: 100 }, (_, i) => ({ kind: 'dot', x: i, y: i })) }, doc);
  assert.strictEqual(many.count, 16);
  assert.strictEqual(many.title.length, 50);
  assert.strictEqual(many.formula.length, 48);
  assert.strictEqual(many.notes.length, 3);
  assert.ok(many.notes.every((t) => t.length === 70));
  const poly = B.build({ shapes: [{ kind: 'poly', points: Array.from({ length: 100 }, (_, i) => i) }] }, doc).svg.children[0];
  assert.strictEqual(poly.attrs.points.split(' ').length, 8);
  assert.strictEqual(B.build({ shapes: [{ kind: 'poly', points: [1, 2, 3, 4] }], formula: 'x' }, doc).count, 0);
});

test('how long it stays: the AI suggests, but only within sane bounds', () => {
  const cases = [[30, 30], [1, 8], [0, 8], [-5, 8], [9999, 90], ['45', 45], [undefined, 20], ['soon', 20], [NaN, 20], [true, 20], [12.6, 13]];
  for (const [given, want] of cases) assert.strictEqual(B.seconds(given), want, 'seconds(' + String(given) + ')');
});

test('garbage specs build an empty picture instead of throwing', () => {
  for (const junk of [null, undefined, 5, 'x', [], {}, { shapes: 'circle' }, { shapes: [[]] }, { notes: 'a' }]) {
    const b = B.build(junk, doc);
    assert.strictEqual(b.count, 0);
    assert.deepStrictEqual(b.notes, []);
  }
});

console.log('board.js: ' + n + ' checks passed');
