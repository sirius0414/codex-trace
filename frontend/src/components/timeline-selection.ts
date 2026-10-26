/** Turn 框选的纯几何：保持端点有序、约束在时间轴内，不改变缩放视口。 */
import { clampViewport, zoomViewport, type Viewport } from './viewport.ts';

export type SelectionEdge = 'min' | 'max';

function axis(domain: Viewport) {
  return clampViewport(domain, domain, 0);
}

/** 选区最窄为 1 个轴单位；不足 1 的时间轴保留整个范围。 */
export function clampSelection(selection: Viewport, domain: Viewport): Viewport {
  const bounds = axis(domain);
  return clampViewport(selection, bounds, Math.min(1, bounds.max - bounds.min));
}

/** 单击创建约占当前视口 8% 的窄区间，边缘处平移选区以保留宽度。 */
export function selectionAt(value: number, viewport: Viewport, domain: Viewport): Viewport {
  const bounds = axis(domain), view = clampViewport(viewport, bounds, 0);
  const center = Number.isFinite(value) ? value : (view.min + view.max) / 2;
  const span = Math.max(Math.min(1, bounds.max - bounds.min), (view.max - view.min) * .08);
  return clampSelection({ min: center - span / 2, max: center + span / 2 }, bounds);
}

/** 双向拖建都保留最小跨度；越界拖动只会碰到轴边界。 */
export function selectionBetween(anchor: number, value: number, domain: Viewport): Viewport {
  return clampSelection({ min: Math.min(anchor, value), max: Math.max(anchor, value) }, domain);
}

/** 调整一个端点，固定另一端；抓取偏移在时间单位中保留，端点碰面后不互换。 */
export function resizeSelection(selection: Viewport, edge: SelectionEdge, value: number, domain: Viewport, grabOffset = 0): Viewport {
  const bounds = axis(domain), current = clampSelection(selection, bounds);
  if (!Number.isFinite(value) || !Number.isFinite(grabOffset)) return current;
  const floor = Math.min(1, bounds.max - bounds.min), target = value - grabOffset;
  return edge === 'min'
    ? { min: Math.max(bounds.min, Math.min(current.max - floor, target)), max: current.max }
    : { min: current.min, max: Math.min(bounds.max, Math.max(current.min + floor, target)) };
}

/** 命中区始终在图内；窄选区把两个命中区朝外排开，旗帜可独立锚定真实端点。 */
export function selectionHandleLayout(selection: Viewport, viewport: Viewport, width: number) {
  const available = Number.isFinite(width) ? Math.max(0, width) : 0, size = Math.min(24, available);
  const span = viewport.max - viewport.min;
  const position = (value: number) => Math.max(0, Math.min(available, (value - viewport.min) / span * available));
  const min = position(selection.min), max = position(selection.max);
  const bound = (value: number) => Math.max(0, Math.min(available - size, value));
  let left = bound(min - size / 2), right = bound(max - size / 2);
  if (right < left + size && available >= size * 2) {
    left = Math.max(0, Math.min(min - size, available - size * 2));
    right = Math.min(available - size, Math.max(max, left + size));
  }
  return { min: { position: min, left }, max: { position: max, left: right } };
}

/** 双指距离决定跨度，中点决定锚定时间；缩放和平移合成后才钳制最终窗口。 */
export function selectionPinch(viewport: Viewport, domain: Viewport, initialCenter: number, center: number, factor: number): Viewport {
  const scaled = zoomViewport(viewport, domain, factor, .5, Math.min(1, domain.max - domain.min));
  const span = scaled.max - scaled.min, anchor = viewport.min + initialCenter * (viewport.max - viewport.min);
  return clampViewport({ min: anchor - center * span, max: anchor + (1 - center) * span }, domain, Math.min(1, domain.max - domain.min));
}
