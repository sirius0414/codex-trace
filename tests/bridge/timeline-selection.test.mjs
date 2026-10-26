/** 合成框选几何验证：单击宽度、反向拖建、不可交叉端点及抓取偏移。 */
import test from 'node:test';
import assert from 'node:assert/strict';
import { clampSelection, selectionAt, selectionBetween, resizeSelection, selectionHandleLayout, selectionPinch } from '../../frontend/src/components/timeline-selection.ts';

const domain = { min: 0, max: 1000 }, viewport = { min: 100, max: 300 };

test('click creates eight percent of visible span and preserves width at domain edges', () => {
  assert.deepEqual(selectionAt(200, viewport, domain), { min: 192, max: 208 });
  assert.deepEqual(selectionAt(0, viewport, domain), { min: 0, max: 16 });
  assert.deepEqual(selectionAt(1000, viewport, domain), { min: 984, max: 1000 });
});

test('drag selection is bidirectional, bounded, and at least one unit wide', () => {
  assert.deepEqual(selectionBetween(300, 100, domain), { min: 100, max: 300 });
  assert.deepEqual(selectionBetween(-100, 2000, domain), domain);
  assert.deepEqual(selectionBetween(10, 10, domain), { min: 9.5, max: 10.5 });
  const short = { min: 4, max: 4.5 };
  assert.deepEqual(selectionAt(4.2, short, short), short);
  assert.deepEqual(selectionBetween(4.1, 4.2, short), short);
});

test('endpoint dragging preserves the original grab offset and keeps the opposite endpoint', () => {
  const original = { min: 100, max: 200 };
  assert.deepEqual(resizeSelection(original, 'min', 105, domain, 5), original);
  assert.deepEqual(resizeSelection(original, 'min', 155, domain, 5), { min: 150, max: 200 });
  assert.deepEqual(resizeSelection(original, 'max', 190, domain, -10), original);
  assert.deepEqual(original, { min: 100, max: 200 });
});

test('endpoints stop before crossing and cannot leave the domain', () => {
  const original = { min: 100, max: 200 };
  assert.deepEqual(resizeSelection(original, 'min', 900, domain), { min: 199, max: 200 });
  assert.deepEqual(resizeSelection(original, 'max', 0, domain), { min: 100, max: 101 });
  assert.deepEqual(resizeSelection(original, 'min', -100, domain), { min: 0, max: 200 });
  assert.deepEqual(resizeSelection(original, 'max', 2000, domain), { min: 100, max: 1000 });
});

test('epoch-valued boundaries and invalid inputs stay finite without mutating caller ranges', () => {
  const epoch = 1792713600000, bounds = { min: epoch, max: epoch + 10000 };
  const original = { min: epoch + 500, max: epoch + 1000 };
  assert.deepEqual(resizeSelection(original, 'max', epoch + 400, bounds), { min: epoch + 500, max: epoch + 501 });
  assert.deepEqual(resizeSelection(original, 'min', NaN, bounds), original);
  assert.deepEqual(resizeSelection(original, 'min', 1, bounds, Infinity), original);
  assert.deepEqual(clampSelection({ min: Infinity, max: NaN }, bounds), bounds);
});

test('narrow and offscreen handles retain nonoverlapping 24px targets inside either viewport edge', () => {
  for (const selection of [{ min: 0, max: 1 }, { min: 999, max: 1000 }, { min: -100, max: -1 }, { min: 1001, max: 1100 }]) {
    const layout = selectionHandleLayout(selection, domain, 100);
    assert.ok(layout.min.left >= 0);
    assert.ok(layout.max.left + 24 <= 100);
    assert.ok(layout.min.left + 24 <= layout.max.left);
  }
  const right = selectionHandleLayout({ min: 999, max: 1000 }, domain, 100);
  assert.equal(right.max.position, 100);
  assert.equal(right.max.left, 76);
});

test('body pinch preserves its midpoint anchor while scaling and panning within domain bounds', () => {
  assert.deepEqual(selectionPinch(viewport, domain, .5, .5, .5), { min: 150, max: 250 });
  assert.deepEqual(selectionPinch(viewport, domain, .5, .75, .5), { min: 125, max: 225 });
  assert.deepEqual(selectionPinch(viewport, domain, .5, 20, .5), { min: 0, max: 100 });
});
