/** 详情宽度只影响本地布局；拖动由分隔线捕获，取消时恢复原宽度，不修改任务阅读状态。 */
import { useLayoutEffect, useRef, useState, type KeyboardEvent, type PointerEvent, type RefObject } from 'react';

export const DEFAULT_INSPECTOR_WIDTH = 330;
const MIN_PANE_WIDTH = 280;

export function useInspectorResize(panel: RefObject<HTMLElement | null>, overlay: boolean, preferredWidth: number, onResize: (width: number) => void) {
  const [containerWidth, setContainerWidth] = useState(0);
  const [resizing, setResizing] = useState(false);
  const drag = useRef<{ id: number; x: number; width: number; previous: number; handle: HTMLDivElement } | null>(null);
  useLayoutEffect(() => {
    const container = panel.current?.parentElement;
    if (!container) return;
    const measure = () => setContainerWidth(container.clientWidth);
    measure();
    const observer = new ResizeObserver(measure);
    observer.observe(container);
    return () => observer.disconnect();
  }, [panel]);
  const available = overlay ? Math.floor(containerWidth * .9) : Math.max(0, containerWidth - MIN_PANE_WIDTH);
  const max = containerWidth ? available : DEFAULT_INSPECTOR_WIDTH;
  const min = Math.min(MIN_PANE_WIDTH, max);
  const clamp = (value: number) => Math.round(Math.max(min, Math.min(max, value)));
  // 容器缩小时只收窄显示宽度，保留期望值，恢复宽屏后还原原先尺寸。
  const width = clamp(preferredWidth);

  function finish(cancelled: boolean) {
    const active = drag.current;
    if (!active) return;
    drag.current = null;
    setResizing(false);
    if (cancelled) onResize(active.previous);
    if (active.handle.hasPointerCapture(active.id)) active.handle.releasePointerCapture(active.id);
  }
  // 分栏切换成覆盖布局时，原手势的坐标已失效。
  useLayoutEffect(() => { finish(true); }, [overlay]);

  function move(event: PointerEvent<HTMLDivElement>) {
    const active = drag.current;
    if (active?.id !== event.pointerId) return;
    onResize(clamp(active.width + active.x - event.clientX));
  }
  function keyDown(event: KeyboardEvent<HTMLDivElement>) {
    if (drag.current) {
      if (event.key === 'Escape') { event.preventDefault(); event.stopPropagation(); finish(true); }
      return;
    }
    const step = event.shiftKey ? 50 : 10;
    const next = event.key === 'ArrowLeft' ? width + step : event.key === 'ArrowRight' ? width - step
      : event.key === 'Home' ? min : event.key === 'End' ? max : event.key === 'Enter' ? DEFAULT_INSPECTOR_WIDTH : null;
    if (next == null) return;
    event.preventDefault(); event.stopPropagation(); onResize(clamp(next));
  }
  return { width, min, max, resizing, handleProps: {
    onPointerDown(event: PointerEvent<HTMLDivElement>) {
      if (drag.current || event.button !== 0 || !event.isPrimary) return;
      event.preventDefault();
      event.currentTarget.focus({ preventScroll: true });
      event.currentTarget.setPointerCapture(event.pointerId);
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
    onDoubleClick() { onResize(clamp(DEFAULT_INSPECTOR_WIDTH)); },
    onKeyDown: keyDown,
  } };
}
