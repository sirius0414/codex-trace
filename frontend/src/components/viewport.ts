/**
 * 瀑布图时间视口的纯几何运算。交互层传入时间轴范围和缩放／平移量，
 * 返回有序且受时间轴约束的范围；不修改筛选、事件数据或调用者的对象。
 */
export type Viewport = { min: number; max: number };

function orderedDomain(domain: Viewport): Viewport {
  if (!Number.isFinite(domain.min) || !Number.isFinite(domain.max)) return { min: 0, max: 1 };
  const min = Math.min(domain.min, domain.max);
  const max = Math.max(domain.min, domain.max);
  if (!Number.isFinite(max - min)) return { min: 0, max: 1 };
  if (max > min) return { min, max };

  // 空时间轴也要有可计算的宽度；绝对时间较大时，步长必须能被浮点数表示。
  const step = Math.max(1, Math.abs(min) * Number.EPSILON);
  return Number.isFinite(min + step) ? { min, max: min + step } : { min: min - step, max: min };
}

function spanFloor(domain: Viewport, minimumSpan: number): number {
  const precision = Math.max(Number.MIN_VALUE, Math.abs(domain.min) * Number.EPSILON, Math.abs(domain.max) * Number.EPSILON);
  return Math.min(domain.max - domain.min, Math.max(precision, Number.isFinite(minimumSpan) ? minimumSpan : 0));
}

/**
 * 将范围移回时间轴边界，优先保留宽度。过小范围以中心扩展到 minimumSpan，
 * 过大范围恢复整个时间轴；无效视口同样恢复全轴，反向端点会先排序。
 */
export function clampViewport(viewport: Viewport, domain: Viewport, minimumSpan: number): Viewport {
  const axis = orderedDomain(domain);
  if (!Number.isFinite(viewport.min) || !Number.isFinite(viewport.max)) return axis;
  const min = Math.min(viewport.min, viewport.max);
  const max = Math.max(viewport.min, viewport.max);
  const originalSpan = max - min;
  const axisSpan = axis.max - axis.min;
  const span = Math.min(axisSpan, Math.max(spanFloor(axis, minimumSpan), originalSpan));
  if (span >= axisSpan) return axis;

  const start = Math.min(axis.max - span, Math.max(axis.min, min - (span - originalSpan) / 2));
  return { min: start, max: start + span };
}

/**
 * factor < 1 放大，factor > 1 缩小；anchor 是视口内的 0..1 比例，缩放时
 * 保持该位置的时间不动，直到时间轴边界要求平移。无效倍数不改变当前范围。
 */
export function zoomViewport(viewport: Viewport, domain: Viewport, factor: number, anchor: number, minimumSpan: number): Viewport {
  const axis = orderedDomain(domain);
  const current = clampViewport(viewport, axis, minimumSpan);
  if (!Number.isFinite(factor) || factor <= 0) return current;
  const fraction = Number.isFinite(anchor) ? Math.max(0, Math.min(1, anchor)) : 0.5;
  const span = current.max - current.min;
  const nextSpan = Math.min(axis.max - axis.min, Math.max(spanFloor(axis, minimumSpan), span * factor));
  const fixedTime = current.min + span * fraction;
  const start = fixedTime - nextSpan * fraction;
  return clampViewport({ min: start, max: start + nextSpan }, axis, minimumSpan);
}

/** 正 delta 向较晚时间平移；触及边界时保留当前宽度，无效位移保持原范围。 */
export function panViewport(viewport: Viewport, domain: Viewport, delta: number): Viewport {
  const axis = orderedDomain(domain);
  const current = clampViewport(viewport, axis, 0);
  if (!Number.isFinite(delta)) return current;
  const axisSpan = axis.max - axis.min;
  const offset = Math.max(-axisSpan, Math.min(axisSpan, delta));
  return clampViewport({ min: current.min + offset, max: current.max + offset }, axis, 0);
}

/** 定位完整目标区间；已可见时保持窗口，否则居中并保留缩放，仅为超宽目标扩展。 */
export function revealViewport(viewport: Viewport, domain: Viewport, target: Viewport, minimumSpan = 0): Viewport {
  const current = clampViewport(viewport, domain, minimumSpan);
  const extent = clampViewport(target, domain, 0);
  if (extent.min >= current.min && extent.max <= current.max) return current;
  const span = Math.max(current.max - current.min, extent.max - extent.min);
  const center = extent.min + (extent.max - extent.min) / 2;
  return clampViewport({ min: center - span / 2, max: center + span / 2 }, domain, minimumSpan);
}
