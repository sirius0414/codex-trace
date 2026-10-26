/** 单个 SVG 时间轴的连续导航；只维护可见窗口，不把缩放/平移写成列表筛选。 */
import { useEffect, useRef, useState, type KeyboardEvent, type MouseEvent, type PointerEvent, type RefObject } from 'react';
import { clampViewport, panViewport, zoomViewport, type Viewport } from './viewport';

type Options = {
  svg: RefObject<SVGSVGElement | null>;
  wheelTarget?: RefObject<HTMLElement | null>;
  domain: Viewport;
  minimumSpan: number;
  resetKey: unknown;
  enabled: boolean;
  isSelectionActive?: () => boolean;
  onInteract: () => void;
};
type Point = { x: number; y: number };
type Gesture = { viewport: Viewport; center: number; distance: number; width: number };

export function useTimelineViewport(options: Options) {
  const [window, setWindow] = useState<Viewport | null>(null);
  const [dragging, setDragging] = useState(false);
  const viewport = window ? clampViewport(window, options.domain, options.minimumSpan) : options.domain;
  // 原生 wheel 与连续 pointer 事件读取最新窗口，避免同一帧内多个增量互相覆盖。
  const current = useRef({ ...options, viewport });
  const pointers = useRef(new Map<number, Point>());
  const gesture = useRef<Gesture | null>(null);
  const moved = useRef(false);
  const suppressClick = useRef(false);
  const redraw = useRef<number | null>(null);
  const pendingFull = useRef(false);
  current.current = { ...options, viewport: redraw.current == null ? viewport : pendingFull.current ? options.domain : clampViewport(current.current.viewport, options.domain, options.minimumSpan) };

  function commit(next: Viewport) {
    const state = current.current;
    next = clampViewport(next, state.domain, state.minimumSpan);
    if (next.min === state.viewport.min && next.max === state.viewport.max) return;
    state.viewport = next;
    pendingFull.current = next.min === state.domain.min && next.max === state.domain.max;
    if (redraw.current == null) {
      // 聚合与 SVG 更新每帧最多一次；手势坐标仍在每个输入事件后立即累计。
      redraw.current = requestAnimationFrame(() => {
        redraw.current = null;
        pendingFull.current = false;
        const { viewport: next, domain } = current.current;
        setWindow(next.min === domain.min && next.max === domain.max ? null : next);
      });
      state.onInteract();
    }
  }

  function clearGesture() {
    gesture.current = null;
    const element = current.current.svg.current;
    for (const id of pointers.current.keys()) if (element?.hasPointerCapture(id)) element.releasePointerCapture(id);
    pointers.current.clear();
    setDragging(false);
  }

  useEffect(() => {
    if (redraw.current != null) cancelAnimationFrame(redraw.current);
    redraw.current = null;
    pendingFull.current = false;
    current.current.viewport = current.current.domain;
    clearGesture();
    suppressClick.current = false;
    setWindow(null);
  }, [options.resetKey]);
  useEffect(() => {
    clearGesture();
    if (!options.enabled) suppressClick.current = false;
  }, [options.enabled]);
  useEffect(() => () => { if (redraw.current != null) cancelAnimationFrame(redraw.current); }, []);

  useEffect(() => {
    // 指针层与 SVG 共用同一个容器，滚轮在标尺或指针上也继续导航视口。
    const element = options.wheelTarget?.current ?? options.svg.current;
    if (!element || !options.enabled) return;
    function wheel(event: WheelEvent) {
      const { viewport, domain, minimumSpan, svg } = current.current;
      const bounds = svg.current!.getBoundingClientRect();
      if (bounds.width <= 0 || (!event.deltaX && !event.deltaY)) return;
      // 非 passive 监听器阻止触控板捏合缩放整个宿主页；所有坐标只在图内解释。
      event.preventDefault();
      // 连续指针手势固定起始坐标；直到松手前只允许该手势修改视口或选区。
      if (pointers.current.size || current.current.isSelectionActive?.()) return;
      const unit = event.deltaMode === 1 ? 16 : event.deltaMode === 2 ? bounds.width : 1;
      const horizontal = !event.ctrlKey && !event.metaKey && (event.shiftKey || Math.abs(event.deltaX) > Math.abs(event.deltaY));
      if (horizontal) {
        const delta = event.shiftKey && !event.deltaX ? event.deltaY : event.deltaX;
        commit(panViewport(viewport, domain, delta * unit / bounds.width * (viewport.max - viewport.min)));
      } else {
        const anchor = (event.clientX - bounds.left) / bounds.width;
        const factor = Math.exp(Math.max(-1, Math.min(1, event.deltaY * unit * (event.ctrlKey ? .01 : .002))));
        commit(zoomViewport(viewport, domain, factor, anchor, minimumSpan));
      }
    }
    const listener = (event: Event) => wheel(event as WheelEvent);
    element.addEventListener('wheel', listener, { passive: false });
    return () => element.removeEventListener('wheel', listener);
  }, [options.enabled, options.svg, options.wheelTarget]);

  function rebaseGesture() {
    const points = [...pointers.current.values()];
    const bounds = current.current.svg.current!.getBoundingClientRect();
    if (!points.length || bounds.width <= 0) { gesture.current = null; return; }
    const center = points.length > 1 ? (points[0].x + points[1].x) / 2 : points[0].x;
    gesture.current = {
      viewport: current.current.viewport, center: (center - bounds.left) / bounds.width,
      distance: points.length > 1 ? Math.hypot(points[1].x - points[0].x, points[1].y - points[0].y) : 0,
      width: bounds.width,
    };
  }

  function onPointerDown(event: PointerEvent<SVGSVGElement>) {
    if (event.pointerType !== 'touch' && (event.button !== 0 || pointers.current.size)) return;
    if (!pointers.current.size) {
      moved.current = false;
      suppressClick.current = false;
      // 延迟鼠标 capture 到真正拖动，保留 SVG 标记原有的点击目标。
      if (!(event.target as Element).closest('[data-overview-key]')) event.currentTarget.focus();
    }
    pointers.current.set(event.pointerId, { x: event.clientX, y: event.clientY });
    if (pointers.current.size > 1) { moved.current = true; suppressClick.current = true; }
    rebaseGesture();
  }

  function onPointerMove(event: PointerEvent<SVGSVGElement>) {
    if (!pointers.current.has(event.pointerId) || !gesture.current) return;
    if (event.pointerType !== 'touch' && !(event.buttons & 1)) { onPointerEnd(event); return; }
    pointers.current.set(event.pointerId, { x: event.clientX, y: event.clientY });
    const points = [...pointers.current.values()], start = gesture.current;
    const bounds = event.currentTarget.getBoundingClientRect();
    const center = ((points.length > 1 ? (points[0].x + points[1].x) / 2 : points[0].x) - bounds.left) / start.width;
    if (!moved.current && Math.abs(center - start.center) * start.width < 4) return;
    moved.current = true;
    suppressClick.current = true;
    setDragging(true);
    if (!event.currentTarget.hasPointerCapture(event.pointerId)) event.currentTarget.setPointerCapture(event.pointerId);
    const { domain, minimumSpan } = current.current;
    const distance = points.length > 1 ? Math.hypot(points[1].x - points[0].x, points[1].y - points[0].y) : 0;
    const zoomed = zoomViewport(start.viewport, domain, distance > 0 && start.distance > 0 ? start.distance / distance : 1, .5, minimumSpan);
    // 同时捏合与平移时只钳制一次边界，避免先缩放后平移在边缘发生跳跃。
    const span = zoomed.max - zoomed.min;
    const anchor = start.viewport.min + start.center * (start.viewport.max - start.viewport.min);
    commit({ min: anchor - center * span, max: anchor + (1 - center) * span });
  }

  function onPointerEnd(event: PointerEvent<SVGSVGElement>) {
    if (!pointers.current.delete(event.pointerId)) return;
    if (event.currentTarget.hasPointerCapture(event.pointerId)) event.currentTarget.releasePointerCapture(event.pointerId);
    rebaseGesture();
    if (!pointers.current.size) setDragging(false);
  }

  function onKeyDown(event: KeyboardEvent<SVGSVGElement>) {
    // 标记上的 Enter/Space 仍由记录选择处理；导航快捷键在整张图内可用。
    const { viewport, domain, minimumSpan } = current.current;
    let next: Viewport;
    if (event.key === '+' || event.key === '=') next = zoomViewport(viewport, domain, .5, .5, minimumSpan);
    else if (event.key === '-' || event.key === '_') next = zoomViewport(viewport, domain, 2, .5, minimumSpan);
    else if (event.key === 'ArrowLeft' || event.key === 'ArrowRight') next = panViewport(viewport, domain, (event.key === 'ArrowLeft' ? -.1 : .1) * (viewport.max - viewport.min));
    else if (event.key === 'Home' || event.key === '0') next = domain;
    else return;
    event.preventDefault();
    commit(next);
  }

  return {
    viewport, dragging,
    zoom: (factor: number) => { const state = current.current; commit(zoomViewport(state.viewport, state.domain, factor, .5, state.minimumSpan)); },
    reset: () => commit(current.current.domain),
    show: commit,
    svgHandlers: {
      onPointerDown, onPointerMove, onPointerUp: onPointerEnd, onPointerCancel: onPointerEnd,
      onLostPointerCapture: (event: PointerEvent<SVGSVGElement>) => {
        // 触摸从标记的隐式 capture 转交 SVG 时也会冒泡一次 lost；它不表示手指已离开。
        if (event.target === event.currentTarget && !event.currentTarget.hasPointerCapture(event.pointerId)) onPointerEnd(event);
      },
      onPointerLeave: (event: PointerEvent<SVGSVGElement>) => {
        if (event.pointerType !== 'touch' && !event.currentTarget.hasPointerCapture(event.pointerId)) onPointerEnd(event);
      },
      onKeyDown,
      onClickCapture: (event: MouseEvent<SVGSVGElement>) => {
        if (suppressClick.current && event.detail !== 0) { event.preventDefault(); event.stopPropagation(); }
        suppressClick.current = false;
      },
    },
  };
}
