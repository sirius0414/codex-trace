/** 详情宽度只影响本地布局；拖动由分隔线捕获，取消时恢复原宽度，不修改任务阅读状态。 */
import { useLayoutEffect, useRef, useState, type KeyboardEvent, type PointerEvent, type RefObject } from 'react';

export const DEFAULT_INSPECTOR_WIDTH = 330;
const MIN_PANE_WIDTH = 280;

/** 分栏优先给列表留出可用宽度；覆盖布局保留返回列表的可见边缘。未知尺寸用默认宽度。 */
export function inspectorWidthBounds(containerWidth: number, overlay: boolean) {
  const measured = Number.isFinite(containerWidth) && containerWidth > 0;
  const max = measured ? overlay ? Math.floor(containerWidth * .9) : Math.max(0, Math.floor(containerWidth) - MIN_PANE_WIDTH) : DEFAULT_INSPECTOR_WIDTH;
  return { min: Math.min(MIN_PANE_WIDTH, max), max };
}

export function clampInspectorWidth(value: number, bounds: { min: number; max: number }) {
  return Math.round(Math.max(bounds.min, Math.min(bounds.max, Number.isFinite(value) ? value : DEFAULT_INSPECTOR_WIDTH)));
}

/** Enter 恢复期望默认宽度，不把暂时窄容器的显示上限保存成新的默认值。 */
export function inspectorKeyboardWidth(key: string, width: number, bounds: { min: number; max: number }, shift = false) {
  if (key === 'Enter') return DEFAULT_INSPECTOR_WIDTH;
  const step = shift ? 50 : 10;
  const next = key === 'ArrowLeft' ? width + step : key === 'ArrowRight' ? width - step
    : key === 'Home' ? bounds.min : key === 'End' ? bounds.max : null;
  return next === null ? null : clampInspectorWidth(next, bounds);
}

export function useInspectorResize(panel: RefObject<HTMLElement | null>, overlay: boolean, preferredWidth: number, onResize: (width: number) => void) {
  const [containerWidth, setContainerWidth] = useState(0);
  const [resizing, setResizing] = useState(false);
  const drag = useRef<{ id: number; x: number; width: number; previous: number; handle: HTMLDivElement } | null>(null);
  const resizeCallback = useRef(onResize);
  resizeCallback.current = onResize;
  useLayoutEffect(() => {
    const container = panel.current?.parentElement;
    if (!container) return;
    let previousWidth = container.clientWidth;
    const measure = () => {
      const nextWidth = container.clientWidth;
      // 容器宽度改变后原 clientX 锚点已失效；纵向分隔线仅改变高度，不应中断横向手势。
      if (nextWidth !== previousWidth) finish(true);
      previousWidth = nextWidth;
      setContainerWidth(nextWidth);
    };
    measure();
    const observer = new ResizeObserver(measure);
    observer.observe(container);
    return () => { observer.disconnect(); finish(true, false); };
  }, [panel]);
  const { min, max } = inspectorWidthBounds(containerWidth, overlay);
  const clamp = (value: number) => clampInspectorWidth(value, { min, max });
  // 容器缩小时只收窄显示宽度，保留期望值，恢复宽屏后还原原先尺寸。
  const width = clamp(preferredWidth);

  function finish(cancelled: boolean, updateState = true) {
    const active = drag.current;
    if (!active) return;
    drag.current = null;
    if (updateState) setResizing(false);
    if (cancelled) resizeCallback.current(active.previous);
    try { if (active.handle.hasPointerCapture(active.id)) active.handle.releasePointerCapture(active.id); }
    catch { /* 移除面板或浏览器取消指针时，捕获可能已经释放；仍须完成取消恢复。 */ }
  }
  // 分栏切换成覆盖布局时，原手势的坐标已失效。
  useLayoutEffect(() => { finish(true); }, [overlay]);
  useLayoutEffect(() => {
    const active = drag.current;
    if (!resizing || !active) return;
    const owner = active.handle.ownerDocument, host = owner.defaultView;
    const cancel = () => finish(true);
    const escape = (event: globalThis.KeyboardEvent) => {
      if (event.key !== 'Escape' || !drag.current) return;
      // 拖动期间 Tab 可能移走焦点；取消必须先于全局 Escape 关闭详情。
      event.preventDefault(); event.stopImmediatePropagation(); finish(true);
    };
    owner.addEventListener('keydown', escape, true);
    host?.addEventListener('blur', cancel);
    return () => { owner.removeEventListener('keydown', escape, true); host?.removeEventListener('blur', cancel); };
  }, [resizing]);

  function move(event: PointerEvent<HTMLDivElement>) {
    const active = drag.current;
    if (active?.id !== event.pointerId) return;
    resizeCallback.current(clamp(active.width + active.x - event.clientX));
  }
  function keyDown(event: KeyboardEvent<HTMLDivElement>) {
    if (drag.current) {
      if (event.key === 'Escape') { event.preventDefault(); event.stopPropagation(); finish(true); }
      return;
    }
    const next = inspectorKeyboardWidth(event.key, width, { min, max }, event.shiftKey);
    if (next == null) return;
    event.preventDefault(); event.stopPropagation(); resizeCallback.current(next);
  }
  return { width, min, max, resizing, handleProps: {
    onPointerDown(event: PointerEvent<HTMLDivElement>) {
      if (drag.current || event.button !== 0 || !event.isPrimary) return;
      event.preventDefault();
      event.currentTarget.focus({ preventScroll: true });
      try { event.currentTarget.setPointerCapture(event.pointerId); }
      catch { return; /* 未能取得捕获时不进入无法保证结束事件的拖动状态。 */ }
      drag.current = { id: event.pointerId, x: event.clientX, width, previous: preferredWidth, handle: event.currentTarget };
      setResizing(true);
    },
    onPointerMove(event: PointerEvent<HTMLDivElement>) {
      if (drag.current?.id !== event.pointerId) return;
      if (event.pointerType !== 'touch' && !(event.buttons & 1)) { finish(false); return; }
      move(event);
    },
    onPointerUp(event: PointerEvent<HTMLDivElement>) {
      if (drag.current?.id !== event.pointerId) return;
      move(event); finish(false);
    },
    onPointerCancel(event: PointerEvent<HTMLDivElement>) { if (drag.current?.id === event.pointerId) finish(true); },
    onLostPointerCapture(event: PointerEvent<HTMLDivElement>) { if (drag.current?.id === event.pointerId) finish(true); },
    onDoubleClick() { resizeCallback.current(DEFAULT_INSPECTOR_WIDTH); },
    onKeyDown: keyDown,
  } };
}
