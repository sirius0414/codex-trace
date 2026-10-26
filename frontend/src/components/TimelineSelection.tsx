/** 独立时间选区覆盖层：尺／端点负责单指框选，框选模式的图主体同时允许双指导航。 */
import { useEffect, useRef, useState, type KeyboardEvent, type PointerEvent } from 'react';
import { usePreferences } from '../preferences';
import type { Viewport } from './viewport';
import { clampSelection, resizeSelection, selectionAt, selectionBetween, selectionHandleLayout, selectionPinch, type SelectionEdge } from './timeline-selection';

type Props = {
  domain: Viewport; viewport: Viewport; selection: Viewport | null; selecting: boolean;
  onChange: (range: Viewport | null, restoring?: boolean) => void; formatValue: (value: number) => string;
  onNavigate?: (viewport: Viewport) => void;
  onGestureChange?: (active: boolean) => void;
  onCommit?: (range: Viewport) => void;
  mappingKey?: string;
};
type Gesture = {
  pointerId: number; edge: SelectionEdge | null; anchor: number; offset: number;
  startX: number; moved: boolean; original: Viewport | null;
  source: 'ruler' | 'thumb' | 'body'; point: { x: number; y: number }; previewed: boolean;
  touch: boolean;
};
type Pinch = { points: Map<number, { x: number; y: number }>; viewport: Viewport; center: number; distance: number; width: number };
type KeyboardGesture = { keys: Set<string>; original: Viewport | null; previewed: boolean; forceCommit: boolean };

export default function TimelineSelection(props: Props) {
  const { t } = usePreferences();
  const root = useRef<HTMLDivElement>(null);
  const gesture = useRef<Gesture | null>(null);
  const pinch = useRef<Pinch | null>(null);
  const keyboard = useRef<KeyboardGesture | null>(null);
  const pending = useRef<Viewport | null>(null);
  const pendingNavigation = useRef<Viewport | null>(null);
  const frame = useRef<number | null>(null);
  const interacting = useRef(false);
  const [width, setWidth] = useState(0);
  const selection = props.selection ? clampSelection(props.selection, props.domain) : null;
  const received = useRef(selection);
  received.current = selection;
  // 父级尚未渲染上一帧回调时，后续输入仍使用刚刚提交的逻辑选区。
  const latest = useRef({ ...props, selection });
  latest.current = { ...props, selection: pending.current ?? selection };

  function gestureChanged(active: boolean) {
    if (interacting.current === active) return;
    interacting.current = active;
    latest.current.onGestureChange?.(active);
  }
  function flush() {
    if (frame.current != null) cancelAnimationFrame(frame.current);
    frame.current = null;
    const next = pending.current;
    pending.current = null;
    if (next) {
      if (gesture.current) gesture.current.previewed = true;
      if (keyboard.current) keyboard.current.previewed = true;
      latest.current.selection = next; latest.current.onChange(next);
    }
    const navigation = pendingNavigation.current;
    pendingNavigation.current = null;
    if (navigation) latest.current.onNavigate?.(navigation);
  }
  function commit(range: Viewport) {
    const next = clampSelection(range, latest.current.domain), previous = latest.current.selection;
    if (previous?.min === next.min && previous.max === next.max) return;
    latest.current.selection = next;
    pending.current = next;
    if (frame.current == null) frame.current = requestAnimationFrame(flush);
  }
  function clearGesture(restore = false) {
    const active = gesture.current, keyed = keyboard.current;
    const ids = pinch.current ? [...pinch.current.points.keys()] : active ? [active.pointerId] : [];
    gesture.current = null;
    pinch.current = null;
    keyboard.current = null;
    if (frame.current != null) cancelAnimationFrame(frame.current);
    frame.current = null;
    pending.current = null;
    pendingNavigation.current = null;
    // 用户取消需撤销已提交的预览；外部映射变化只清理输入，不能覆盖新状态。
    const original = active ? active.original : keyed?.original ?? null;
    latest.current.selection = restore && (active || keyed) ? original : received.current;
    gestureChanged(false);
    for (const id of ids) if (root.current?.hasPointerCapture(id)) root.current.releasePointerCapture(id);
    if (restore && (active?.previewed || keyed?.previewed)) latest.current.onChange(original, true);
  }
  useEffect(() => {
    const element = root.current;
    if (!element) return;
    const observer = new ResizeObserver(entries => setWidth(entries[0].contentRect.width));
    observer.observe(element);
    // 尺／端点专用单指；图主体可把第二指交给本组件的双指导航，其他指针不透传背景。
    const ignoreSecondPointer = (event: globalThis.PointerEvent) => {
      const active = gesture.current, navigation = pinch.current;
      const canPinch = active?.source === 'body' && active.touch && event.pointerType === 'touch' && latest.current.onNavigate
        && event.target instanceof Element && event.target.closest('.cx-turn-selection-hit');
      if ((navigation && !navigation.points.has(event.pointerId) || active && event.pointerId !== active.pointerId && !canPinch)
        && element.parentElement?.contains(event.target as Node)) {
        event.preventDefault(); event.stopImmediatePropagation();
      }
    };
    document.addEventListener('pointerdown', ignoreSecondPointer, true);
    const loseWindowFocus = () => clearGesture(true);
    window.addEventListener('blur', loseWindowFocus);
    return () => { observer.disconnect(); document.removeEventListener('pointerdown', ignoreSecondPointer, true); window.removeEventListener('blur', loseWindowFocus); clearGesture(); };
  }, []);
  useEffect(() => clearGesture(), [props.selecting]);
  // 缩放／滚动改变坐标映射时结束旧手势，避免用旧抓取位置套进新视口发生跳变。
  useEffect(() => { if (!pinch.current) clearGesture(); }, [props.viewport.min, props.viewport.max]);
  useEffect(() => clearGesture(), [props.domain.min, props.domain.max]);
  useEffect(() => clearGesture(), [props.mappingKey]);

  function valueAt(clientX: number) {
    const bounds = root.current!.getBoundingClientRect(), { viewport } = latest.current;
    return bounds.width > 0 ? viewport.min + (clientX - bounds.left) / bounds.width * (viewport.max - viewport.min) : viewport.min;
  }
  function start(event: PointerEvent<HTMLDivElement>) {
    event.stopPropagation();
    if (pinch.current || event.button !== 0) return;
    // 按键尚未释放时先撤销该次预览，不能让两种输入共享一次提交。
    if (keyboard.current) { event.preventDefault(); clearGesture(true); return; }
    const element = event.target as HTMLElement;
    const active = gesture.current;
    if (active) {
      if (active.source !== 'body' || !active.touch || event.pointerType !== 'touch' || !latest.current.onNavigate
        || !element.closest('.cx-turn-selection-hit')) return;
      event.preventDefault();
      const bounds = root.current!.getBoundingClientRect(), first = active.point, second = { x: event.clientX, y: event.clientY };
      if (bounds.width <= 0) return;
      if (frame.current != null) cancelAnimationFrame(frame.current);
      frame.current = null; pending.current = null;
      gesture.current = null;
      // 单指预览不是最终框选；第二指进入时恢复首次按下前的选择，再接管缩放／平移。
      latest.current.selection = active.original;
      if (active.previewed) latest.current.onChange(active.original, true);
      pinch.current = { points: new Map([[active.pointerId, first], [event.pointerId, second]]), viewport: latest.current.viewport,
        center: ((first.x + second.x) / 2 - bounds.left) / bounds.width,
        distance: Math.hypot(second.x - first.x, second.y - first.y), width: bounds.width };
      root.current!.setPointerCapture(event.pointerId);
      return;
    }
    const edge = element.closest<HTMLElement>('[data-selection-edge]')?.dataset.selectionEdge as SelectionEdge | undefined;
    if (!edge && !element.closest('.cx-turn-ruler') && !latest.current.selecting) return;
    event.preventDefault();
    const anchor = valueAt(event.clientX), current = latest.current.selection;
    gesture.current = { pointerId: event.pointerId, edge: edge ?? null, anchor,
      offset: edge && current ? anchor - current[edge] : 0, startX: event.clientX, moved: false, original: current,
      source: edge ? 'thumb' : element.closest('.cx-turn-ruler') ? 'ruler' : 'body',
      point: { x: event.clientX, y: event.clientY }, previewed: false, touch: event.pointerType === 'touch' };
    gestureChanged(true);
    root.current!.setPointerCapture(event.pointerId);
    (element.closest<HTMLElement>('[role="slider"],.cx-turn-ruler') ?? root.current)?.focus({ preventScroll: true });
  }
  function navigate(event: PointerEvent<HTMLDivElement>) {
    const active = pinch.current;
    if (!active?.points.has(event.pointerId)) return;
    active.points.set(event.pointerId, { x: event.clientX, y: event.clientY });
    const [first, second] = [...active.points.values()], bounds = root.current!.getBoundingClientRect();
    const distance = Math.hypot(second.x - first.x, second.y - first.y);
    const center = ((first.x + second.x) / 2 - bounds.left) / active.width;
    pendingNavigation.current = selectionPinch(active.viewport, latest.current.domain, active.center, center,
      active.distance > 0 && distance > 0 ? active.distance / distance : 1);
    if (frame.current == null) frame.current = requestAnimationFrame(flush);
  }
  function move(event: PointerEvent<HTMLDivElement>) {
    if (pinch.current?.points.has(event.pointerId)) { event.preventDefault(); event.stopPropagation(); navigate(event); return; }
    const active = gesture.current;
    if (!active || active.pointerId !== event.pointerId) return;
    event.preventDefault(); event.stopPropagation();
    active.point = { x: event.clientX, y: event.clientY };
    if (event.pointerType !== 'touch' && !(event.buttons & 1)) { clearGesture(true); return; }
    if (!active.moved && Math.abs(event.clientX - active.startX) < 3) return;
    active.moved = true;
    const { domain } = latest.current, value = valueAt(event.clientX);
    commit(active.edge && active.original ? resizeSelection(active.original, active.edge, value, domain, active.offset)
      : selectionBetween(active.anchor, value, domain));
  }
  function finish(event: PointerEvent<HTMLDivElement>) {
    if (pinch.current?.points.has(event.pointerId)) { event.stopPropagation(); navigate(event); flush(); clearGesture(); return; }
    const active = gesture.current;
    if (!active || active.pointerId !== event.pointerId) return;
    event.stopPropagation();
    // pointerup 的 buttons 已为 0；直接读最终坐标，不能走移动中的松键清理分支。
    if (active.moved) {
      const { domain } = latest.current, value = valueAt(event.clientX);
      commit(active.edge && active.original ? resizeSelection(active.original, active.edge, value, domain, active.offset)
        : selectionBetween(active.anchor, value, domain));
    } else if (!active.edge) commit(selectionAt(valueAt(event.clientX), latest.current.viewport, latest.current.domain));
    gesture.current = null;
    if (root.current?.hasPointerCapture(event.pointerId)) root.current.releasePointerCapture(event.pointerId);
    flush();
    const final = latest.current.selection;
    // 只在明确完成时定位；预览、取消以及双指导航都不会触发 onCommit。
    if (final && (!active.edge || active.original?.min !== final.min || active.original.max !== final.max)) latest.current.onCommit?.(final);
    gestureChanged(false);
  }
  function beginKey(event: KeyboardEvent<HTMLDivElement>, forceCommit = false) {
    if (gesture.current || pinch.current) return false;
    // Escape／失焦取消后仍可能收到系统 repeat，等待下一次真正按下再开启新手势。
    if (!keyboard.current && event.repeat) return false;
    if (!keyboard.current) {
      keyboard.current = { keys: new Set(), original: latest.current.selection, previewed: false, forceCommit };
      gestureChanged(true);
    }
    keyboard.current.keys.add(event.key);
    keyboard.current.forceCommit ||= forceCommit;
    return true;
  }
  function finishKey(event: KeyboardEvent<HTMLDivElement>) {
    const active = keyboard.current;
    if (!active?.keys.delete(event.key)) return;
    event.preventDefault(); event.stopPropagation();
    if (active.keys.size) return;
    keyboard.current = null;
    flush();
    const final = latest.current.selection;
    if (final && (active.forceCommit || active.original?.min !== final.min || active.original.max !== final.max)) latest.current.onCommit?.(final);
    gestureChanged(false);
  }
  function resizeKey(event: KeyboardEvent<HTMLDivElement>, edge: SelectionEdge) {
    const { selection: current, domain, viewport } = latest.current;
    if (!current) return;
    const floor = Math.min(1, domain.max - domain.min);
    const step = Math.max(floor, (viewport.max - viewport.min) * .01) * (event.shiftKey ? 10 : 1);
    let value: number;
    if (event.key === 'ArrowLeft' || event.key === 'ArrowDown') value = current[edge] - step;
    else if (event.key === 'ArrowRight' || event.key === 'ArrowUp') value = current[edge] + step;
    else if (event.key === 'Home') value = edge === 'min' ? domain.min : current.min + floor;
    else if (event.key === 'End') value = edge === 'max' ? domain.max : current.max - floor;
    else return;
    event.preventDefault(); event.stopPropagation();
    if (!beginKey(event)) return;
    commit(resizeSelection(current, edge, value, domain));
  }

  const span = props.viewport.max - props.viewport.min;
  const position = (value: number) => (value - props.viewport.min) / span * 100;
  const left = selection ? Math.max(0, Math.min(100, position(selection.min))) : 0;
  const right = selection ? Math.max(0, Math.min(100, position(selection.max))) : 0;
  const handles = selection ? selectionHandleLayout(selection, props.viewport, width) : null;
  const hint = selection ? `${props.formatValue(selection.min)} – ${props.formatValue(selection.max)}` : t('单击 / 拖动选择记录', 'Click / drag to select records');
  return <div ref={root} className={`cx-turn-selection ${props.selecting ? 'selecting' : ''}`} role="group" aria-label={t('记录时间选区', 'Record time selection')} tabIndex={-1}
    onPointerDown={start} onPointerMove={move} onPointerUp={finish}
    onPointerCancel={event => { if (gesture.current?.pointerId === event.pointerId || pinch.current?.points.has(event.pointerId)) { event.stopPropagation(); clearGesture(true); } }}
    onLostPointerCapture={event => { if (event.target === event.currentTarget && !event.currentTarget.hasPointerCapture(event.pointerId)
      && (gesture.current?.pointerId === event.pointerId || pinch.current?.points.has(event.pointerId))) clearGesture(true); }}
    onClick={event => event.stopPropagation()} onDoubleClick={event => { event.preventDefault(); event.stopPropagation(); }}
    onKeyDown={event => { if (event.key === 'Escape' && (gesture.current || pinch.current || keyboard.current)) { event.preventDefault(); event.stopPropagation(); clearGesture(true); } }}
    onKeyUp={finishKey} onBlur={() => { if (keyboard.current) clearGesture(true); }}>
    <div className="cx-turn-selection-visual" aria-hidden="true">{selection && <>
      <div className="cx-turn-selection-outside" style={{ left: 0, width: `${left}%` }} />
      <div className="cx-turn-selection-shade" style={{ left: `${left}%`, width: `${Math.max(0, right - left)}%` }} />
      <div className="cx-turn-selection-outside" style={{ left: `${right}%`, right: 0 }} />
      {(['min', 'max'] as const).map(edge => selection[edge] >= props.viewport.min && selection[edge] <= props.viewport.max
        ? <span key={edge} className="cx-turn-selection-line" style={{ left: `clamp(0px, ${position(selection[edge])}%, calc(100% - 1px))` }} /> : null)}
    </>}</div>
    <div className="cx-turn-ruler" role="button" tabIndex={0} aria-label={t('单击或拖动选择记录范围', 'Click or drag to select a record range')}
      onKeyDown={event => { if (event.key === 'Enter' || event.key === ' ') { event.preventDefault(); event.stopPropagation(); if (!beginKey(event, true)) return; const { viewport, domain } = latest.current; commit(selectionAt((viewport.min + viewport.max) / 2, viewport, domain)); } }}>
      <span>{hint}</span>
    </div>
    {props.selecting && <div className="cx-turn-selection-hit" aria-hidden="true" />}
    {selection && handles && (['min', 'max'] as const).map(edge => {
      const outside = selection[edge] < props.viewport.min ? 'before' : selection[edge] > props.viewport.max ? 'after' : null;
      const handle = handles[edge];
      return <div key={edge} className={`cx-turn-selection-thumb ${edge} ${outside ? 'outside' : ''}`} data-selection-edge={edge}
      data-selection-outside={outside ?? undefined} title={outside ? t('端点在视口外，可拖动、键盘调整或定位选区', 'Endpoint is outside the viewport; drag, use the keyboard or reveal the selection') : undefined}
      style={{ left: handle.left }} role="slider" tabIndex={0} aria-orientation="horizontal"
      aria-label={edge === 'min' ? t('时间选区起点', 'Time selection start') : t('时间选区终点', 'Time selection end')}
      aria-valuemin={edge === 'min' ? props.domain.min : selection.min + Math.min(1, props.domain.max - props.domain.min)}
      aria-valuemax={edge === 'max' ? props.domain.max : selection.max - Math.min(1, props.domain.max - props.domain.min)}
      aria-valuenow={selection[edge]} aria-valuetext={props.formatValue(selection[edge])} onKeyDown={event => resizeKey(event, edge)}>
      <span className="cx-turn-selection-flag" style={{ left: outside ? 12 : handle.position - handle.left }} aria-hidden="true">{outside ? outside === 'before' ? '‹' : '›' : null}</span>
    </div>;
    })}
  </div>;
}
