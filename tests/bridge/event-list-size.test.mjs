import test from 'node:test';
import assert from 'node:assert/strict';
import { ListSizeTracker } from '../../frontend/src/components/event-list-size.ts';

for (const first of ['scroll', 'ResizeObserver']) {
  test(`list growth clamps automatically without becoming user scroll when ${first} arrives first`, () => {
    const sizes = new ListSizeTracker();
    assert.equal(sizes.measure(300, 2000, true, 1700), null);
    // 浏览器在回调到达前已经把 scrollTop 从1700钳到1640；跟随仍定位新底部。
    assert.equal(sizes.measure(360, 2000, true, 1700), 1640);
    assert.equal(sizes.measure(360, 2000, true, 1640), null);
    // 第二个同尺寸回调不会重复定位，之后真实上滚可以按普通用户事件处理。
    assert.equal(sizes.measure(360, 2000, true, 1500), null);
  });
}

test('following remains at the bottom when the list becomes shorter', () => {
  const sizes = new ListSizeTracker();
  sizes.measure(360, 2000, true, 1640);
  assert.equal(sizes.measure(300, 2000, true, 1640), 1700);
  assert.equal(sizes.measure(280, 2000, true, 1700), 1720);
});

test('paused reading retains its anchor unless the browser maximum requires a clamp', () => {
  const sizes = new ListSizeTracker();
  sizes.measure(300, 2000, false, 800);
  assert.equal(sizes.measure(360, 2000, false, 800), 800);
  assert.equal(sizes.measure(200, 2000, false, 800), 800);
  assert.equal(sizes.measure(1300, 2000, false, 800), 700);
  // 调整尺寸不改写阅读锚点；恢复较小列表时原位置仍能还原。
  assert.equal(sizes.measure(300, 2000, false, 800), 800);
});

test('no-overflow, zero-height and changed follow state use current measurements', () => {
  const sizes = new ListSizeTracker();
  sizes.measure(300, 300, true, 0);
  assert.equal(sizes.measure(400, 300, true, 0), 0);
  assert.equal(sizes.measure(0, 300, false, 100), 100);
  assert.equal(sizes.measure(200, 300, false, 100), 100);
  assert.equal(sizes.measure(250, 300, true, 100), 50);
});
