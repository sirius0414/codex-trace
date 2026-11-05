/** 无损 JSON 连续文本阅读器；只对长值提供就地折叠，搜索和复制由 Preview 统一提供。 */
import { Fragment, useLayoutEffect, useMemo, useRef } from 'react';
import { usePreferences } from '../../preferences';
import { parseJsonDocument } from './json-model';
import { isLongJsonValue, jsonRows, JSON_READING_LIMITS, type JsonReaderState } from './json-layout';
import type { TextHit } from './preview-model';
import './json-reader.css';
export type { JsonReaderState } from './json-layout';
export type JsonReaderProps = { text: string; state?: JsonReaderState; onState: (state: JsonReaderState) => void; wrap?: boolean; lineNumbers?: boolean; hit?: TextHit | null };
export function JsonReader({ text, state = {}, onState, wrap = true, lineNumbers = false, hit }: JsonReaderProps) {
  const { t } = usePreferences();
  const root = useRef<HTMLDivElement>(null);
  const pendingFocus = useRef<string | null>(null);
  useLayoutEffect(() => {
    if (!pendingFocus.current) return;
    const row = root.current?.querySelector<HTMLElement>(`[data-json-id="${CSS.escape(pendingFocus.current)}"]`);
    row?.scrollIntoView({ block: 'nearest' });
    row?.querySelector<HTMLElement>('.cx-json-fold')?.focus({ preventScroll: true });
    pendingFocus.current = null;
  }, [state.expanded]);
  const parsed = useMemo(() => parseJsonDocument(text), [text]);
  const rows = useMemo(() => parsed.document ? jsonRows(parsed.document, state) : [], [parsed, state.expanded]);
  const mark = (start: number, end: number) => {
    if (!hit || hit.start < start || hit.end > end) return text.slice(start, end);
    return <>{text.slice(start, hit.start)}<mark data-reader-hit tabIndex={-1}>{text.slice(hit.start, hit.end)}</mark>{text.slice(hit.end, end)}</>;
  };
  const renderWindow = (key: string, start: number, end: number) => {
    const offset = Math.min(state.sourceOffsets?.[key] ?? 0, Math.max(0, end - start - 1));
    const from = start + offset, to = Math.min(end, from + JSON_READING_LIMITS.characters);
    const move = (next: number) => onState({ ...state, sourceOffsets: { ...state.sourceOffsets, [key]: next } });
    return <>{offset > 0 && <button className="cx-json-window" onClick={() => move(Math.max(0, offset - JSON_READING_LIMITS.characters))}>{t('前一段', 'Previous text')} …</button>}{mark(from, to)}{to < end && <button className="cx-json-window" onClick={() => move(offset + JSON_READING_LIMITS.characters)}>… {t('继续显示已加载文本', 'Show more loaded text')}</button>}</>;
  };
  if (!parsed.document) return <div className="cx-reader-json-fallback"><p className="cx-data-note">{parsed.error === 'limit' ? t('JSON 超出排版资源上限，显示原文。', 'JSON exceeds the layout resource limit; showing source.') : t('JSON 无法解析，显示原文。', 'JSON could not be parsed; showing source.')}</p><pre tabIndex={0} className={`cx-reader-source ${wrap ? 'wrap' : ''}`}>{renderWindow('$fallback', 0, text.length)}</pre></div>;
  const document = parsed.document;
  const offset = Math.min(state.rowOffset ?? 0, Math.max(0, rows.length - 1));
  // 行数与字符数同时有界；大量已展开长字符串也不能一次挂载整个正文。
  let characters = 0;
  const visibleRows = rows.slice(offset, offset + JSON_READING_LIMITS.rows).filter(row => {
    if (characters >= JSON_READING_LIMITS.characters * 2) return false;
    characters += (row.close ? 1 : row.folded ? JSON_READING_LIMITS.preview : row.node.children?.length ? 1 : Math.min(JSON_READING_LIMITS.characters, row.node.end - row.node.start))
      + Math.min(JSON_READING_LIMITS.characters, (row.node.keyEnd ?? 0) - (row.node.keyStart ?? 0)) + row.node.depth * 2 + 3;
    return true;
  });
  const page = (next: number) => onState({ ...state, rowOffset: next });
  return <div ref={root} className="cx-reader-json">
    {offset > 0 && <button className="cx-button" onClick={() => page(Math.max(0, offset - JSON_READING_LIMITS.rows))}>{t('显示前一段 JSON', 'Previous JSON section')}</button>}
    <pre className={`cx-reader-pre cx-json-text ${wrap ? 'wrap' : ''} ${lineNumbers ? 'numbered' : ''}`} tabIndex={0} aria-label={t('JSON 正文', 'JSON body')}><code>{visibleRows.map((row, index) => {
      const { node, close, folded, comma } = row;
      const long = isLongJsonValue(document, node);
      const toggle = () => {
        const next = { ...state, expanded: { ...state.expanded, [node.id]: !state.expanded?.[node.id] } };
        const index = jsonRows(document, next).findIndex(row => row.node.id === node.id);
        pendingFocus.current = node.id;
        onState({ ...next, rowOffset: Math.min(offset, index) });
      };
      return <Fragment key={`${node.id}:${close ? 'close' : 'value'}`}><span className="cx-reader-code-line" data-line={offset + index + 1} data-json-id={node.id}>
        {'  '.repeat(node.depth)}{!close && node.keyStart !== undefined && <><span className="hljs-attr">{renderWindow(`key:${node.id}`, node.keyStart, node.keyEnd!)}</span>{': '}</>}
        {close ? text[node.end - 1] : folded ? <button className="cx-json-fold" aria-label={t(`展开 ${node.path}`, `Expand ${node.path}`)} aria-expanded={false} onClick={toggle}>{node.kind === 'object' ? '{…}' : node.kind === 'array' ? '[…]' : `${text.slice(node.start, node.start + JSON_READING_LIMITS.preview)}…"`}</button> : node.children?.length ? text[node.start] : <span className={node.kind === 'string' ? 'hljs-string' : 'hljs-literal'}>{renderWindow(node.id, node.start, node.end)}</span>}
        {comma ? ',' : ''}{long && !folded && <button className="cx-json-fold cx-json-collapse" aria-label={t(`收起 ${node.path}`, `Collapse ${node.path}`)} aria-expanded={true} onClick={toggle}>{t('收起', 'Collapse')}</button>}
      </span>{'\n'}</Fragment>;
    })}</code></pre>
    {rows.length > offset + visibleRows.length && <button className="cx-button" onClick={() => page(offset + visibleRows.length)}>{t('继续显示已加载 JSON', 'Show more loaded JSON')}</button>}
  </div>;
}
export default JsonReader;
