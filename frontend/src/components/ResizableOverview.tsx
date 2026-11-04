/** 概览与下方工作区的高度分隔；不改任务状态，容器缩小时保留用户期望高度。 */
import { useLayoutEffect, useRef, useState, type PointerEvent, type ReactNode } from 'react';
import { usePreferences } from '../preferences';
import { clampOverviewHeight, DEFAULT_OVERVIEW_HEIGHT, overviewHeightBounds } from './overview-size';

export default function ResizableOverview({ children }: { children: ReactNode }) {
  const { t } = usePreferences();
  const pane = useRef<HTMLDivElement>(null), handle = useRef<HTMLDivElement>(null);
  const [preferred, setPreferred] = useState(DEFAULT_OVERVIEW_HEIGHT);
  const [bounds, setBounds] = useState({ min: 100, max: DEFAULT_OVERVIEW_HEIGHT });
  const [resizing, setResizing] = useState(false);
  const drag = useRef<{ id: number; y: number; height: number; previous: number; handle: HTMLDivElement } | null>(null);
  const height = clampOverviewHeight(preferred, bounds);
  function finish(cancelled: boolean) {
    const active = drag.current;
    if (!active) return;
    drag.current = null; setResizing(false);
    if (cancelled) setPreferred(active.previous);
    try { if (active.handle.hasPointerCapture(active.id)) active.handle.releasePointerCapture(active.id); } catch { /* 浏览器已取消指针时仍完成布局回滚。 */ }
  }
  useLayoutEffect(() => {
    const element = pane.current, root = element?.parentElement;
    if (!element || !root) return;
    let previousSize = '';
    const measure = () => {
      const size = `${root.clientWidth}:${root.clientHeight}`;
      // 宿主尺寸变化后原拖动坐标失效；只回滚本次手势，不丢失此前选择的尺寸。
      if (previousSize && size !== previousSize) finish(true);
      previousSize = size;
      const chrome = [...root.children].filter((child): child is HTMLElement => child instanceof HTMLElement
        && child !== element && !child.classList.contains('cx-work') && !['absolute', 'fixed'].includes(getComputedStyle(child).position));
      const chromeHeight = chrome.reduce((sum, child) => sum + child.getBoundingClientRect().height, 0) + (handle.current?.offsetHeight ?? 8);
      const next = overviewHeightBounds(root.clientHeight, chromeHeight);
      setBounds(current => current.min === next.min && current.max === next.max ? current : next);
    };
    const observer = new ResizeObserver(measure);
    const observe = () => { observer.disconnect(); observer.observe(root); for (const child of root.children) if (child !== element && !child.classList.contains('cx-work')) observer.observe(child); measure(); };
    observe();
    const children = new MutationObserver(observe);
    children.observe(root, { childList: true });
    // 展开阅读会将概览置为 inert；不能让尚未结束的手势继续修改背景布局。
    const inert = new MutationObserver(() => { if (element.inert) finish(true); });
    inert.observe(element, { attributes: true, attributeFilter: ['inert'] });
    const cancel = () => finish(true);
    const key = (event: KeyboardEvent) => { if (drag.current && event.key === 'Escape') { event.preventDefault(); event.stopImmediatePropagation(); finish(true); } };
    window.addEventListener('blur', cancel); document.addEventListener('keydown', key, true);
    return () => { observer.disconnect(); children.disconnect(); inert.disconnect(); window.removeEventListener('blur', cancel); document.removeEventListener('keydown', key, true); const active = drag.current; drag.current = null; try { if (active?.handle.hasPointerCapture(active.id)) active.handle.releasePointerCapture(active.id); } catch { /* 卸载后捕获可能已由宿主释放。 */ } };
  }, []);
  function move(event: PointerEvent<HTMLDivElement>) {
    if (drag.current?.id === event.pointerId) setPreferred(clampOverviewHeight(drag.current.height + event.clientY - drag.current.y, bounds));
  }
  return <div ref={pane} className={`cx-overview-pane ${resizing ? 'resizing' : ''}`} style={{ height: height + 8 }}>
    <div id="cx-overview-content" className="cx-overview-frame">{children}</div>
    <div ref={handle} className="cx-overview-resizer" role="separator" tabIndex={0} aria-orientation="horizontal" aria-controls="cx-overview-content cx-agent-page"
      aria-label={t('调整概览与记录区域高度', 'Resize overview and records')} aria-valuemin={bounds.min} aria-valuemax={bounds.max} aria-valuenow={height} aria-valuetext={t(`概览 ${height} 像素`, `Overview ${height} pixels`)}
      title={t('拖动调整概览与记录区域；上下方向键微调，双击或 Enter 恢复默认', 'Drag to resize overview and records; use up/down arrows to adjust, double-click or press Enter to reset')}
      onPointerDown={event => { if (drag.current || event.button !== 0 || !event.isPrimary || bounds.min === bounds.max) return; event.preventDefault(); event.currentTarget.focus({ preventScroll: true }); try { event.currentTarget.setPointerCapture(event.pointerId); } catch { return; } drag.current = { id: event.pointerId, y: event.clientY, height, previous: preferred, handle: event.currentTarget }; setResizing(true); }}
      onPointerMove={event => { if (drag.current?.id !== event.pointerId) return; if (event.pointerType !== 'touch' && !(event.buttons & 1)) { finish(false); return; } move(event); }}
      onPointerUp={event => { if (drag.current?.id === event.pointerId) { move(event); finish(false); } }}
      onPointerCancel={event => { if (drag.current?.id === event.pointerId) finish(true); }}
      onLostPointerCapture={event => { if (drag.current?.id === event.pointerId) finish(true); }}
      onDoubleClick={() => setPreferred(DEFAULT_OVERVIEW_HEIGHT)}
      onKeyDown={event => {
        if (drag.current) return;
        const step = event.shiftKey ? 50 : 10;
        const next = event.key === 'ArrowUp' ? height - step : event.key === 'ArrowDown' ? height + step
          : event.key === 'Home' ? bounds.min : event.key === 'End' ? bounds.max : event.key === 'Enter' ? DEFAULT_OVERVIEW_HEIGHT : null;
        if (next == null) return;
        event.preventDefault(); event.stopPropagation(); setPreferred(event.key === 'Enter' ? DEFAULT_OVERVIEW_HEIGHT : clampOverviewHeight(next, bounds));
      }} />
  </div>;
}
