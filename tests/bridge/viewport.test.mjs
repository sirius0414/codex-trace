import test from 'node:test';
import assert from 'node:assert/strict';
import { clampViewport, panViewport, zoomViewport } from '../../frontend/src/components/viewport.ts';

const domain = { min: 0, max: 1000 };

test('zoom keeps the cursor time at its original fraction', () => {
  const viewport = { min: 200, max: 600 };
  const zoomed = zoomViewport(viewport, domain, 0.5, 0.25, 10);
  assert.deepEqual(zoomed, { min: 250, max: 450 });
  assert.equal(viewport.min + 400 * 0.25, zoomed.min + 200 * 0.25);
  assert.deepEqual(zoomViewport(zoomed, domain, 2, 0.25, 10), viewport);
  assert.deepEqual(viewport, { min: 200, max: 600 });
});

test('cursor anchoring remains accurate for epoch timestamps', () => {
  const epoch = 1792713600000;
  const axis = { min: epoch, max: epoch + 100000 };
  const viewport = { min: epoch + 20000, max: epoch + 60000 };
  const zoomed = zoomViewport(viewport, axis, 0.5, 0.75, 1);
  assert.deepEqual(zoomed, { min: epoch + 35000, max: epoch + 55000 });
  assert.equal(zoomed.min + (zoomed.max - zoomed.min) * 0.75, epoch + 50000);
});

test('panning preserves width at both time boundaries', () => {
  const viewport = { min: 200, max: 600 };
  assert.deepEqual(panViewport(viewport, domain, 100), { min: 300, max: 700 });
  assert.deepEqual(panViewport(viewport, domain, -10000), { min: 0, max: 400 });
  assert.deepEqual(panViewport(viewport, domain, 10000), { min: 600, max: 1000 });
  assert.deepEqual(panViewport(domain, domain, 100), domain);
});

test('clamping shifts an out-of-bounds viewport instead of cropping it', () => {
  assert.deepEqual(clampViewport({ min: -100, max: 300 }, domain, 10), { min: 0, max: 400 });
  assert.deepEqual(clampViewport({ min: 800, max: 1200 }, domain, 10), { min: 600, max: 1000 });
});

test('zoom stops at its minimum width and zooming out restores the full axis', () => {
  const minimum = zoomViewport({ min: 200, max: 600 }, domain, 0.0001, 0.5, 50);
  assert.deepEqual(minimum, { min: 375, max: 425 });
  assert.deepEqual(zoomViewport(minimum, domain, 0.1, 0.5, 50), minimum);
  assert.deepEqual(zoomViewport(minimum, domain, 1000, 0.2, 50), domain);
  assert.deepEqual(zoomViewport(minimum, domain, Number.MAX_VALUE, 0.2, 50), domain);
  assert.deepEqual(clampViewport(minimum, domain, 2000), domain);
});

test('zoom shifts at an axis edge while retaining the requested width', () => {
  assert.deepEqual(zoomViewport({ min: 0, max: 200 }, domain, 2, 0.9, 1), { min: 0, max: 400 });
  assert.deepEqual(zoomViewport({ min: 800, max: 1000 }, domain, 2, 0.1, 1), { min: 600, max: 1000 });
});

test('a shrinking time domain preserves the viewport width when it still fits', () => {
  assert.deepEqual(clampViewport({ min: 500, max: 900 }, { min: 0, max: 700 }, 10), { min: 300, max: 700 });
  assert.deepEqual(clampViewport({ min: 500, max: 900 }, { min: 0, max: 200 }, 10), { min: 0, max: 200 });
});

test('reversed and collapsed ranges produce an ordered axis with a minimum width', () => {
  assert.deepEqual(clampViewport({ min: 600, max: 200 }, { min: 1000, max: 0 }, 10), { min: 200, max: 600 });
  assert.deepEqual(clampViewport({ min: 500, max: 500 }, domain, 40), { min: 480, max: 520 });
  assert.deepEqual(clampViewport({ min: 0, max: 0 }, domain, 40), { min: 0, max: 40 });
  assert.deepEqual(clampViewport({ min: 7, max: 7 }, { min: 7, max: 7 }, 40), { min: 7, max: 8 });
});

test('invalid gesture values recover without introducing NaN or changing valid ranges', () => {
  const viewport = { min: 200, max: 600 };
  for (const factor of [0, -1, NaN, Infinity]) assert.deepEqual(zoomViewport(viewport, domain, factor, 0.5, 10), viewport);
  for (const delta of [NaN, Infinity, -Infinity]) assert.deepEqual(panViewport(viewport, domain, delta), viewport);
  assert.deepEqual(clampViewport({ min: NaN, max: 600 }, domain, 10), domain);
  assert.deepEqual(zoomViewport(viewport, domain, 0.5, NaN, 10), { min: 300, max: 500 });
  assert.deepEqual(zoomViewport(viewport, domain, 0.5, -1, 10), { min: 200, max: 400 });
  assert.deepEqual(zoomViewport(viewport, domain, 0.5, 2, 10), { min: 400, max: 600 });
});
