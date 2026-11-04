import test from 'node:test';
import assert from 'node:assert/strict';
import { DEFAULT_INSPECTOR_WIDTH, clampInspectorWidth, inspectorKeyboardWidth, inspectorWidthBounds } from '../../frontend/src/components/useInspectorResize.ts';

test('inspector bounds preserve list width in split layout and a visible edge in overlay layout', () => {
  assert.deepEqual(inspectorWidthBounds(1200, false), { min: 280, max: 920 });
  assert.deepEqual(inspectorWidthBounds(500, false), { min: 220, max: 220 });
  assert.deepEqual(inspectorWidthBounds(390, true), { min: 280, max: 351 });
  assert.deepEqual(inspectorWidthBounds(240, true), { min: 216, max: 216 });
  assert.deepEqual(inspectorWidthBounds(200, false), { min: 0, max: 0 });
  for (const size of [0, -1, NaN, Infinity]) {
    assert.deepEqual(inspectorWidthBounds(size, true), { min: 280, max: DEFAULT_INSPECTOR_WIDTH });
  }
});

test('temporary container shrink only clamps display and restores the preferred width when space returns', () => {
  const preference = 640;
  assert.equal(clampInspectorWidth(preference, inspectorWidthBounds(1100, false)), 640);
  assert.equal(clampInspectorWidth(preference, inspectorWidthBounds(700, false)), 420);
  assert.equal(clampInspectorWidth(preference, inspectorWidthBounds(390, true)), 351);
  assert.equal(clampInspectorWidth(preference, inspectorWidthBounds(1100, false)), 640);
  assert.equal(preference, 640);
  assert.equal(clampInspectorWidth(NaN, inspectorWidthBounds(1100, false)), DEFAULT_INSPECTOR_WIDTH);
});

test('separator keyboard resize uses visible bounds, larger shift steps and a restorable default', () => {
  const bounds = inspectorWidthBounds(1200, false);
  assert.equal(inspectorKeyboardWidth('ArrowLeft', 330, bounds), 340);
  assert.equal(inspectorKeyboardWidth('ArrowRight', 330, bounds, true), 280);
  assert.equal(inspectorKeyboardWidth('ArrowLeft', 900, bounds, true), 920);
  assert.equal(inspectorKeyboardWidth('Home', 400, bounds), 280);
  assert.equal(inspectorKeyboardWidth('End', 400, bounds), 920);
  assert.equal(inspectorKeyboardWidth('Tab', 400, bounds), null);
  const narrow = inspectorWidthBounds(240, true);
  const reset = inspectorKeyboardWidth('Enter', 216, narrow);
  assert.equal(reset, DEFAULT_INSPECTOR_WIDTH);
  assert.equal(clampInspectorWidth(reset, narrow), 216);
  assert.equal(clampInspectorWidth(reset, bounds), DEFAULT_INSPECTOR_WIDTH);
});
