import test from 'node:test';
import assert from 'node:assert/strict';
import { clampOverviewHeight, DEFAULT_OVERVIEW_HEIGHT, overviewHeightBounds } from '../../frontend/src/components/overview-size.ts';

test('overview reserves usable records space, relaxing minima in short hosts', () => {
  assert.deepEqual(overviewHeightBounds(700, 140), { min: 100, max: 420 });
  assert.deepEqual(overviewHeightBounds(300, 140), { min: 80, max: 80 });
  assert.deepEqual(overviewHeightBounds(100, 140), { min: 0, max: 0 });
  for (let height = 140; height < 1500; height += 13) {
    const bounds = overviewHeightBounds(height, 140);
    assert.ok(bounds.min >= 0 && bounds.min <= bounds.max);
    assert.ok(height - 140 - bounds.max >= Math.min(140, Math.ceil((height - 140) / 2)));
  }
});
test('a temporarily small viewport does not destroy the preferred height', () => {
  const preferred = 350;
  assert.equal(clampOverviewHeight(preferred, overviewHeightBounds(400, 140)), 130);
  assert.equal(clampOverviewHeight(preferred, overviewHeightBounds(900, 140)), preferred);
  assert.equal(clampOverviewHeight(-50, { min: 100, max: 500 }), 100);
  assert.equal(clampOverviewHeight(NaN, { min: 100, max: 500 }), DEFAULT_OVERVIEW_HEIGHT);
});
