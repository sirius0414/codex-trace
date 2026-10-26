/** 可见窗口对应等比例滑块；自定义指针拖动保留抓取位置，原生 range 保留键盘与读屏语义。 */
import { useRef, useState, type PointerEvent } from 'react';
import type { Viewport } from './viewport';

type Props = {
  domain: Viewport;
  viewport: Viewport;
  width: number;
  label: string;
  valueText: string;
  title: string;
  onChange: (viewport: Viewport) => void;
};

export default function TimelineScrollbar(props: Props) {
  const { domain, viewport, width, label, valueText, title, onChange } = props;
  const input = useRef<HTMLInputElement>(null);
  const thumb = useRef<HTMLDivElement>(null);
  const track = useRef<HTMLDivElement>(null);
  const drag = useRef<{ id: number; grabRatio: number } | null>(null);
  const current = useRef(props);
  current.current = props;
  const [dragging, setDragging] = useState(false);
  const span = viewport.max - viewport.min, fullSpan = domain.max - domain.min;
  // 高倍放大保留可抓取宽度，接近全貌时仍留 1px 轨道；平移按扣除滑块后的距离计算。
  const thumbWidth = Math.min(width > 1 ? width - 1 : width, Math.max(24, width * span / fullSpan));
  const movable = Math.max(0, fullSpan - span);
  const progress = movable > 0 ? Math.max(0, Math.min(1, (viewport.min - domain.min) / movable)) : 0;

  function move(clientX: number) {
    if (!drag.current || !track.current || !thumb.current) return;
    const bounds = track.current.getBoundingClientRect(), thumbBounds = thumb.current.getBoundingClientRect();
    const travel = bounds.width - thumbBounds.width;
    if (travel <= 0) return;
    const { domain, viewport, onChange } = current.current;
    const span = viewport.max - viewport.min;
    const fraction = Math.max(0, Math.min(1, (clientX - bounds.left - drag.current.grabRatio * thumbBounds.width) / travel));
    const start = domain.min + fraction * (domain.max - domain.min - span);
    onChange({ min: start, max: start + span });
  }

  function end(event: PointerEvent<HTMLDivElement>) {
    if (drag.current?.id !== event.pointerId) return;
    drag.current = null;
    setDragging(false);
    if (event.currentTarget.hasPointerCapture(event.pointerId)) event.currentTarget.releasePointerCapture(event.pointerId);
  }

  return <div ref={track} className={`cx-timeline-scroll ${dragging ? 'dragging' : ''}`} title={title}
    onPointerDown={event => {
      if (drag.current || event.button !== 0 || !thumb.current) return;
      event.preventDefault();
      input.current?.focus({ preventScroll: true });
      const bounds = thumb.current.getBoundingClientRect();
      const grabbedThumb = thumb.current.contains(event.target as Node);
      drag.current = { id: event.pointerId, grabRatio: grabbedThumb && bounds.width > 0 ? (event.clientX - bounds.left) / bounds.width : .5 };
      event.currentTarget.setPointerCapture(event.pointerId);
      setDragging(true);
      if (!grabbedThumb) move(event.clientX);
    }}
    onPointerMove={event => {
      if (drag.current?.id !== event.pointerId) return;
      if (event.pointerType !== 'touch' && !(event.buttons & 1)) { end(event); return; }
      event.preventDefault(); move(event.clientX);
    }}
    onPointerUp={end} onPointerCancel={end} onLostPointerCapture={event => {
      if (event.target === event.currentTarget && !event.currentTarget.hasPointerCapture(event.pointerId)) end(event);
    }}>
    <div className="cx-scroll-rail" aria-hidden="true"><div ref={thumb} className="cx-scroll-thumb" style={{ left: progress * (width - thumbWidth), width: thumbWidth }} /></div>
    <input ref={input} className="cx-scroll-input" type="range" aria-label={label} aria-valuetext={valueText}
      min={domain.min} max={domain.max - span} step="any" value={viewport.min}
      onChange={event => { const start = Number(event.target.value); onChange({ min: start, max: start + span }); }} />
  </div>;
}
