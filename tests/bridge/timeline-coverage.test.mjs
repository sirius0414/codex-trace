/** 覆盖投影只使用合成的可靠区间，验证空档、并行归因和有界绘制都不补造时长。 */
import test from 'node:test';
import assert from 'node:assert/strict';
import { timelineCoverage, coverageMarks, coverageTotals } from '../../frontend/src/components/timeline-coverage.ts';

test('coverage partitions the full domain, retaining unknown gaps', () => {
  const result = timelineCoverage([{ start: 10, end: 30, lane: 1 }, { start: 60, end: 90, lane: 2 }], 0, 100);
  assert.deepEqual(result, {
    segments: [
      { start: 0, end: 10, mask: 0 }, { start: 10, end: 30, mask: 2 },
      { start: 30, end: 60, mask: 0 }, { start: 60, end: 90, mask: 4 }, { start: 90, end: 100, mask: 0 },
    ], recordedMs: 50, unattributedMs: 50, overlapMs: 0,
  });
  assert.deepEqual(timelineCoverage([], 0, 100), {
    segments: [{ start: 0, end: 100, mask: 0 }], recordedMs: 0, unattributedMs: 100, overlapMs: 0,
  });
});

test('points, reversed ranges, invalid timestamps, and unsupported lanes do not add coverage', () => {
  const invalid = [
    { start: 5, end: 5, lane: 0 }, { start: 10, end: 5, lane: 1 },
    { start: NaN, end: 30, lane: 2 }, { start: 10, end: Infinity, lane: 2 },
    { start: 10, end: 30, lane: -1 }, { start: 10, end: 30, lane: 3 }, { start: 10, end: 30, lane: 0.5 },
  ];
  assert.deepEqual(timelineCoverage(invalid, 0, 100), timelineCoverage([], 0, 100));
  for (const [min, max] of [[0, 0], [100, 0], [NaN, 100], [0, Infinity], [-Number.MAX_VALUE, Number.MAX_VALUE]]) {
    assert.deepEqual(timelineCoverage(invalid, min, max), { segments: [], recordedMs: 0, unattributedMs: 0, overlapMs: 0 });
    assert.deepEqual(coverageMarks([], min, max, 20), []);
    assert.deepEqual(coverageTotals([], min, max), { recordedMs: 0, unattributedMs: 0, overlapMs: 0 });
  }
});

test('parallel spans use lane unions and cross-lane overlap is counted only once', () => {
  const spans = [
    { start: 10, end: 50, lane: 2 }, { start: 30, end: 70, lane: 2 },
    { start: 20, end: 60, lane: 1 }, { start: 40, end: 80, lane: 0 },
  ];
  const before = structuredClone(spans);
  const result = timelineCoverage(spans, 0, 100);
  assert.deepEqual(result.segments, [
    { start: 0, end: 10, mask: 0 }, { start: 10, end: 20, mask: 4 },
    { start: 20, end: 40, mask: 6 }, { start: 40, end: 60, mask: 7 },
    { start: 60, end: 70, mask: 5 }, { start: 70, end: 80, mask: 1 }, { start: 80, end: 100, mask: 0 },
  ]);
  assert.equal(result.recordedMs, 70);
  assert.equal(result.unattributedMs, 30);
  assert.equal(result.overlapMs, 50);
  assert.deepEqual(coverageTotals(result.segments, 35, 65), { recordedMs: 30, unattributedMs: 0, overlapMs: 30 });
  assert.deepEqual(timelineCoverage(spans.slice(0, 2), 0, 100), {
    segments: [{ start: 0, end: 10, mask: 0 }, { start: 10, end: 70, mask: 4 }, { start: 70, end: 100, mask: 0 }],
    recordedMs: 60, unattributedMs: 40, overlapMs: 0,
  });
  assert.deepEqual(spans, before);
});

test('coincident starts and ends are atomic and identical neighboring masks merge', () => {
  const result = timelineCoverage([
    { start: 10, end: 20, lane: 1 }, { start: 0, end: 10, lane: 1 },
    { start: 20, end: 30, lane: 2 }, { start: 20, end: 30, lane: 2 },
  ], 0, 30);
  assert.deepEqual(result, {
    segments: [{ start: 0, end: 20, mask: 2 }, { start: 20, end: 30, mask: 4 }],
    recordedMs: 30, unattributedMs: 0, overlapMs: 0,
  });
});

test('coverage and drawable segments clip to the requested viewport', () => {
  const epoch = 1792713600000;
  const spans = [
    { start: epoch - 20, end: epoch + 20, lane: 0 },
    { start: epoch + 80, end: epoch + 140, lane: 2 },
    { start: epoch + 100, end: epoch + 200, lane: 1 },
  ];
  const coverage = timelineCoverage(spans, epoch, epoch + 100);
  assert.deepEqual(coverage.segments, [
    { start: epoch, end: epoch + 20, mask: 1 },
    { start: epoch + 20, end: epoch + 80, mask: 0 },
    { start: epoch + 80, end: epoch + 100, mask: 4 },
  ]);
  assert.equal(coverage.recordedMs, 40);
  assert.equal(coverage.unattributedMs, 60);
  assert.deepEqual(coverageMarks(coverage.segments, epoch + 10, epoch + 90, 10), [
    { start: epoch + 10, end: epoch + 20, masks: [1] },
    { start: epoch + 20, end: epoch + 80, masks: [0] },
    { start: epoch + 80, end: epoch + 90, masks: [4] },
  ]);
});

test('exact drawable segments remain unchanged when they fit the budget', () => {
  const { segments } = timelineCoverage([{ start: 10, end: 30, lane: 2 }], 0, 40);
  const before = structuredClone(segments);
  assert.deepEqual(coverageMarks(segments, 0, 40, 3), [
    { start: 0, end: 10, masks: [0] }, { start: 10, end: 30, masks: [4] }, { start: 30, end: 40, masks: [0] },
  ]);
  assert.deepEqual(segments, before);
});

test('nonadjacent same-bin spans keep their unknown gaps in mixed summaries', () => {
  const { segments } = timelineCoverage([{ start: 10, end: 20, lane: 2 }, { start: 80, end: 90, lane: 2 }], 0, 100);
  assert.deepEqual(coverageMarks(segments, 0, 100, 1), [{ start: 0, end: 100, masks: [0, 4] }]);
  const mixed = timelineCoverage([{ start: 0, end: 50, lane: 1 }, { start: 50, end: 100, lane: 2 }], 0, 100);
  assert.deepEqual(coverageMarks(mixed.segments, 0, 100, 1), [{ start: 0, end: 100, masks: [2, 4] }]);
  assert.equal(mixed.overlapMs, 0);
});

test('bin boundaries do not import neighboring masks that merely touch them', () => {
  const { segments } = timelineCoverage([
    { start: 0, end: 10, lane: 0 }, { start: 10, end: 20, lane: 1 }, { start: 50, end: 100, lane: 2 },
  ], 0, 100);
  assert.deepEqual(coverageMarks(segments, 0, 100, 2), [
    { start: 0, end: 50, masks: [0, 1, 2] }, { start: 50, end: 100, masks: [4] },
  ]);
});

test('a viewport wholly inside a middle gap remains unattributed', () => {
  const { segments } = timelineCoverage([
    { start: 0, end: 10, lane: 0 }, { start: 90, end: 100, lane: 2 },
  ], 0, 100);
  assert.deepEqual(coverageMarks(segments, 40, 60, 400), [{ start: 40, end: 60, masks: [0] }]);
  assert.deepEqual(coverageTotals(segments, 40, 60), { recordedMs: 0, unattributedMs: 20, overlapMs: 0 });
  assert.deepEqual(coverageTotals([], 40, 60), { recordedMs: 0, unattributedMs: 20, overlapMs: 0 });
});

test('100,000 ranges retain exact totals while rendering at most 400 summaries', () => {
  const spans = Array.from({ length: 100000 }, (_, index) => ({ start: index * 2, end: index * 2 + 1, lane: index % 3 }));
  const result = timelineCoverage(spans, 0, 200000);
  assert.equal(result.recordedMs, 100000);
  assert.equal(result.unattributedMs, 100000);
  assert.equal(result.overlapMs, 0);
  assert.equal(result.segments.length, 200000);
  const marks = coverageMarks(result.segments, 0, 200000, 10000);
  assert.equal(marks.length, 400);
  assert.equal(marks[0].start, 0);
  assert.equal(marks.at(-1).end, 200000);
  assert.ok(marks.every(mark => mark.end > mark.start && mark.masks.includes(0)));
  assert.equal(marks.reduce((sum, mark) => sum + mark.end - mark.start, 0), 200000);
  for (const bins of [0, -1, NaN, Infinity]) assert.equal(coverageMarks(result.segments, 0, 200000, bins).length, 1);
  assert.deepEqual(coverageTotals(result.segments, 0, 200000), { recordedMs: 100000, unattributedMs: 100000, overlapMs: 0 });

  // 从大分区中放大一小段，仍只展示相交区间，并精确裁剪两侧的分区边界。
  assert.deepEqual(coverageMarks(result.segments, 100003.5, 100006.5, 10), [
    { start: 100003.5, end: 100004, masks: [0] }, { start: 100004, end: 100005, masks: [2] },
    { start: 100005, end: 100006, masks: [0] }, { start: 100006, end: 100006.5, masks: [4] },
  ]);
  assert.deepEqual(coverageTotals(result.segments, 100003.5, 100006.5), { recordedMs: 1.5, unattributedMs: 1.5, overlapMs: 0 });
  assert.deepEqual(coverageMarks(result.segments, 100003.5, 100006.5, 1), [{ start: 100003.5, end: 100006.5, masks: [0, 2, 4] }]);
  assert.deepEqual(coverageMarks(result.segments, 100001.2, 100001.8, 400), [{ start: 100001.2, end: 100001.8, masks: [0] }]);
});
