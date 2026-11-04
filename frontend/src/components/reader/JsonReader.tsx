/** 按源码索引呈现 JSON；折叠只影响可见性，复制/搜索都覆盖当前完整可读取正文。 */
import { useDeferredValue, useEffect, useId, useLayoutEffect, useMemo, useRef, useState, type ReactNode, type CSSProperties } from 'react';
import type { FieldDescription, ReaderMetadata } from '../../../../contracts/types';
import { usePreferences } from '../../preferences';
import { jsonAncestors, jsonStringPresentation, matchingField, parseJsonDocument, rawJsonValue, revealJsonNode, searchJson, type JsonNode } from './json-model';
import type { RichReaderState } from './RichReader';
import './json-reader.css';

export type JsonStringMode = 'auto' | 'text' | 'source' | 'markdown' | 'code';
export type JsonReaderState = {
  expanded?: Record<string, boolean>;
  depth?: number;
  query?: string;
  directory?: boolean;
  directoryLimit?: number;
  limits?: Record<string, number>;
  offsets?: Record<string, number>;
  selected?: string;
  stringModes?: Record<string, JsonStringMode>;
  stringRich?: Record<string, RichReaderState>;
  stringCodeLines?: Record<string, number>;
  sourceLimits?: Record<string, number>;
};
export type JsonReaderProps = {
  text: string;
  state?: JsonReaderState;
  onState: (state: JsonReaderState) => void;
  copy: (text: string, label: string) => void;
  metadata?: ReaderMetadata;
  wrap?: boolean;
  renderString?: (text: string, mode: 'markdown' | 'code', language: string | undefined, context: { state?: RichReaderState; onState: (next: RichReaderState) => void; visibleLines?: number; onVisibleLines: (count: number) => void }) => ReactNode;
};
const PAGE = 100;

/** 无定义时不提供解释按钮；资料随数据加载，悬浮与固定说明均不发网络请求。 */
export function FieldHelp({ field }: { field: FieldDescription }) {
  const { language, t } = usePreferences();
  const [pinned, setPinned] = useState(false);
  const [hovered, setHovered] = useState(false);
  const [focused, setFocused] = useState(false);
  const [dismissed, setDismissed] = useState(false);
  const restoringFocus = useRef(false);
  const leaveTimer = useRef<ReturnType<typeof setTimeout> | undefined>(undefined);
  useEffect(() => () => clearTimeout(leaveTimer.current), []);
  const id = useId();
  const trigger = useRef<HTMLButtonElement>(null);
  const [position, setPosition] = useState<CSSProperties>({ visibility: 'hidden' });
  const positionCard = () => {
    const rect = trigger.current?.getBoundingClientRect();
    if (!rect) return;
    if (rect.bottom < 0 || rect.top > window.innerHeight || rect.right < 0 || rect.left > window.innerWidth) {
      setPosition(previous => ({ ...previous, visibility: 'hidden' })); return;
    }
    const belowSpace = window.innerHeight - rect.bottom - 12, aboveSpace = rect.top - 12;
    const below = belowSpace >= 280 || belowSpace >= aboveSpace;
    // 卡片始终在触发按钮上方或下方，不能覆盖按钮而拦截固定/取消固定操作。
    const next: CSSProperties = { left: Math.max(12, Math.min(rect.left, window.innerWidth - 332)),
      top: below ? rect.bottom + 6 : rect.top - 6, transform: below ? 'none' : 'translateY(-100%)',
      maxHeight: Math.max(24, below ? belowSpace : aboveSpace), visibility: 'visible' };
    setPosition(previous => Object.keys(next).every(key => previous[key as keyof CSSProperties] === next[key as keyof CSSProperties]) ? previous : next);
  };
  useEffect(() => {
    if (dismissed || !hovered && !pinned && !focused) return;
    // 固定说明跟随触发字段，包含 Inspector 内滚动；滚出视口时隐藏卡片，避免留下失去上下文的覆盖层。
    window.addEventListener('resize', positionCard);
    window.addEventListener('scroll', positionCard, true);
    return () => { window.removeEventListener('resize', positionCard); window.removeEventListener('scroll', positionCard, true); };
  }, [hovered, pinned, focused, dismissed]);
  const open = !dismissed && (pinned || hovered || focused) && position.visibility !== 'hidden';
  const safeLink = /^https?:\/\//i.test(field.sourceUrl);
  return <span className={`cx-reader-field-help ${pinned ? 'is-pinned' : ''}`} data-field-help-open={open}
    onMouseEnter={() => { clearTimeout(leaveTimer.current); setDismissed(false); setHovered(true); positionCard(); }}
    onMouseLeave={() => { leaveTimer.current = setTimeout(() => setHovered(false), 180); }}
    onFocus={() => { setFocused(true); if (!restoringFocus.current) { setDismissed(false); positionCard(); } }}
    onBlur={event => { if (!event.currentTarget.contains(event.relatedTarget as Node | null)) setFocused(false); }}
    onKeyDown={event => {
      if (event.key !== 'Escape' || !open) return;
      event.preventDefault(); event.stopPropagation(); clearTimeout(leaveTimer.current);
      setPinned(false); setHovered(false); setDismissed(true);
      // 恢复触发器焦点只是关闭操作的一部分；不能被 focus-within 或本次 onFocus 立即重新打开。
      restoringFocus.current = true;
      trigger.current?.focus({ preventScroll: true });
      restoringFocus.current = false;
    }}>
    <button ref={trigger} type="button" className="cx-reader-help-trigger" aria-label={t(`字段说明 ${field.path}`, `Field definition ${field.path}`)} aria-expanded={open} aria-controls={id} onClick={() => { setDismissed(false); setPinned(value => !value); positionCard(); }}>i</button>
    <span id={id} className="cx-reader-help-card" role="note" style={position}>
      <strong>{field.scope === 'protocol' ? t('协议字段', 'Protocol field') : t('工具参数', 'Tool parameter')} · {field.path}</strong>
      <span>{language === 'en' && field.descriptionEn ? field.descriptionEn : field.description}</span>
      <span>{field.type && <span>{t('类型', 'Type')}: <code>{field.type}</code> · </span>}{field.required !== undefined && <span>{field.required ? t('必填', 'Required') : t('可选', 'Optional')}</span>}{field.unit && <span> · {t('单位', 'Unit')}: {field.unit}</span>}</span>
      {field.enum && <span>{t('可选值', 'Allowed values')}: {field.enum.join(', ')}</span>}
      <span>{t('版本', 'Version')}: {field.sourceVersion}</span>
      {field.versionMatch === 'reference' && <em>{t('历史版本无法匹配；资料仅供参考。', 'Historical version is not matched; reference only.')}</em>}
      {safeLink ? <a href={field.sourceUrl} target="_blank" rel="noreferrer">{t('查看定义来源', 'Open definition source')} ↗</a> : <span>{field.sourceUrl}</span>}
      {pinned && <button type="button" className="cx-button" onClick={() => setPinned(false)}>{t('取消固定', 'Unpin')}</button>}
    </span>
  </span>;
}

/** 大对象仍提供整个当前层的字段目录；极大目录采用窗口呈现，避免一次挂载数万按钮。 */
function KeyIndex({ nodes, onSelect }: { nodes: JsonNode[]; onSelect: (node: JsonNode) => void }) {
  const { t } = usePreferences();
  const [scrollTop, setScrollTop] = useState(0);
  const virtual = nodes.length > 1000;
  const start = virtual ? Math.max(0, Math.floor(scrollTop / 28) - 4) : 0;
  const visible = virtual ? nodes.slice(start, start + 24) : nodes;
  return <div className="cx-reader-key-index">
    <p>{t(`当前层全部 ${nodes.length} 个字段名`, `All ${nodes.length} field names at this level`)}{virtual && t(' · 滚动按需呈现', ' · rendered as you scroll')}</p>
    <div className={virtual ? 'cx-reader-key-window' : 'cx-reader-key-grid'} tabIndex={0} role="region" aria-label={t('当前层字段名，可滚动', 'Field names at this level, scrollable')} onScroll={event => { if (virtual) setScrollTop(event.currentTarget.scrollTop); }}>
      <div style={virtual ? { height: nodes.length * 28, position: 'relative' } : undefined}>
        {visible.map((node, index) => <button type="button" key={node.id} className="cx-reader-key-link" style={virtual ? { position: 'absolute', top: (start + index) * 28, height: 28, left: 0, right: 0 } : undefined} onClick={() => onSelect(node)} title={node.path}>{node.key}</button>)}
      </div>
    </div>
  </div>;
}

export function JsonReader({ text, state = {}, onState, copy, metadata, renderString, wrap = true }: JsonReaderProps) {
  const { t } = usePreferences();
  const parsed = useMemo(() => parseJsonDocument(text), [text]);
  const directory = useMemo(() => parsed.document ? [...parsed.document.nodes.values()].filter(node => node.parent !== null) : [], [parsed]);
  const deferredQuery = useDeferredValue(state.query ?? '');
  const found = useMemo(() => parsed.document ? searchJson(parsed.document, deferredQuery) : { matches: [], total: 0 }, [parsed, deferredQuery]);
  const container = useRef<HTMLDivElement>(null);
  // 重挂载是恢复阅读，不是新搜索；只在用户改变查询/目标时再次定位。
  const lastQuery = useRef(state.query ?? '');
  const lastSelected = useRef<string | undefined>(state.selected);
  const directoryLimit = state.directoryLimit ?? PAGE;
  const patch = (next: Partial<JsonReaderState>) => onState({ ...state, ...next });
  function renderSource(source: string, key: string, decoded = false) {
    const limit = state.sourceLimits?.[key] ?? 100_000;
    // 显示窗口与复制输入独立；包括结构解析回退，不能一次挂载整段超大正文。
    const boundary = limit < source.length && /[\uD800-\uDBFF]/.test(source[limit - 1] ?? '') ? limit - 1 : limit;
    const shown = source.slice(0, boundary);
    return <><pre className={['cx-reader-source', decoded ? 'cx-reader-string-body' : '', wrap ? 'wrap' : ''].filter(Boolean).join(' ')} aria-label={decoded ? t('字符串正文（已解码）', 'String body (decoded)') : t('原始文本', 'Original text')}>{shown}</pre>{shown.length < source.length && <div className="cx-reader-deferred"><span>{t(`已显示 ${shown.length.toLocaleString()} / ${source.length.toLocaleString()} 个已加载字符单元；复制仍包含全部已加载正文。`, `${shown.length.toLocaleString()} / ${source.length.toLocaleString()} loaded code units shown; copying still includes the entire loaded body.`)}</span><button type="button" className="cx-button" onClick={() => patch({ sourceLimits: { ...state.sourceLimits, [key]: limit + 100_000 } })}>{t('再显示 100,000 个字符单元', 'Show 100,000 more code units')}</button></div>}</>;
  }
  function reveal(node: JsonNode) {
    if (!parsed.document) return;
    patch(revealJsonNode(parsed.document, node, state, PAGE));
    // 重复选择同一个目录/搜索项也要重新定位，但常规刷新不能拉走用户的滚动位置。
    lastSelected.current = undefined;
  }
  useEffect(() => {
    if (deferredQuery === lastQuery.current) return;
    lastQuery.current = deferredQuery;
    if (found.matches[0]) reveal(found.matches[0]);
  }, [deferredQuery, found]);
  useLayoutEffect(() => {
    if (!state.selected || state.selected === lastSelected.current) return;
    const target = container.current?.querySelector<HTMLElement>(`[data-json-id="${CSS.escape(state.selected)}"]`);
    if (target) { target.scrollIntoView({ block: 'nearest' }); target.focus({ preventScroll: true }); lastSelected.current = state.selected; }
  }, [state.selected, state.expanded, state.limits, state.offsets]);
  if (!parsed.document) return <div className="cx-reader-json-fallback"><p className="cx-data-note">{parsed.error === 'limit' ? t('正文超过结构阅读的资源上限；下方保留原文。', 'This content exceeds the structured reader limit; its source is preserved below.') : t('无法为当前正文构建 JSON 字段树；正文可能不完整或使用其他格式，下方保留原文。', 'A JSON field tree is unavailable for this body. It may be incomplete or use another format; its source is preserved below.')}</p><button type="button" className="cx-button" onClick={() => copy(text, t('已加载正文', 'Loaded body'))}>{t('复制已加载正文', 'Copy loaded body')}</button>{renderSource(text, '$fallback')}</div>;
  const document = parsed.document;
  const expandedAt = (node: JsonNode) => state.expanded?.[node.id] ?? node.depth < (state.depth ?? 1);
  const matched = new Set(found.matches.map(node => node.id));
  const selected = state.selected ? document.nodes.get(state.selected) : undefined;
  const selectedChain = new Set(selected ? [...jsonAncestors(document, selected), selected].map(node => node.id) : []);
  let rendered = 0;
  const summary = (node: JsonNode) => {
    if (node.kind === 'object') return t(`{ } · ${node.children?.length ?? 0} 个字段`, `{ } · ${node.children?.length ?? 0} fields`);
    if (node.kind === 'array') return t(`[ ] · ${node.children?.length ?? 0} 个元素`, `[ ] · ${node.children?.length ?? 0} items`);
    if (node.kind === 'string') {
      const value = node.value ?? '';
      return value.length > 120 ? `${JSON.stringify(value.slice(0, 100))}… · ${t(`${value.length} 个 UTF-16 单元`, `${value.length} UTF-16 units`)}` : JSON.stringify(value);
    }
    const raw = rawJsonValue(document, node);
    return raw.length > 120 ? `${raw.slice(0, 100)}… · ${t(`${raw.length} 字符`, `${raw.length} characters`)}` : raw;
  };
  const renderNode = (node: JsonNode): ReactNode => {
    if (++rendered > 2000 && !selectedChain.has(node.id)) return null;
    const field = matchingField(metadata?.fields, node.path);
    const stringView = jsonStringPresentation(node, field);
    const expandable = Boolean(node.children?.length) || stringView.expandable;
    const expanded = expandable && expandedAt(node);
    const mode = state.stringModes?.[node.id] ?? 'auto';
    const readingMode = mode === 'auto' ? stringView.mode : mode;
    const autoLabel = stringView.mode === 'text' ? t('纯文本', 'Text') : stringView.mode === 'markdown' ? 'Markdown' : t('代码', 'Code');
    const limit = state.limits?.[node.id] ?? PAGE;
    const offset = Math.min(state.offsets?.[node.id] ?? 0, Math.max(0, Math.floor(((node.children?.length ?? 1) - 1) / PAGE) * PAGE));
    const longString = node.kind === 'string' && (node.value?.length ?? 0) > 120;
    const stringLength = (node.value?.length ?? 0).toLocaleString();
    const lengthTitle = t(`${stringLength} 个 UTF-16 字符单元`, `${stringLength} UTF-16 code units`);
    return <div className={`cx-reader-json-node ${matched.has(node.id) ? 'is-match' : ''}`} key={node.id}>
      <div className={`cx-reader-json-row ${longString ? 'has-long-string' : ''}`} data-json-id={node.id} tabIndex={0} onKeyDown={event => {
        if (expandable && (event.key === 'ArrowRight' || event.key === 'ArrowLeft')) { event.preventDefault(); patch({ expanded: { ...state.expanded, [node.id]: event.key === 'ArrowRight' } }); }
      }}>
        {expandable ? <button className="cx-reader-json-toggle" type="button" aria-label={t(`${expanded ? '收起' : '展开'} ${node.key}`, `${expanded ? 'Collapse' : 'Expand'} ${node.key}`)} aria-expanded={expanded} onClick={() => patch({ expanded: { ...state.expanded, [node.id]: !expanded } })}>{expanded ? '▾' : '▸'}</button> : <span className="cx-reader-json-spacer" />}
        <span className="cx-reader-json-key" title={node.path || '/'}>{node.key}</span>{field && <FieldHelp field={field} />}
        <span className={`cx-reader-json-value is-${node.kind}`} title={longString ? `${node.kind} · ${lengthTitle}` : node.kind}>{longString ? `${JSON.stringify((node.value ?? '').slice(0, 100))}…` : summary(node)}</span>
        {longString && <span className="cx-reader-json-size" title={lengthTitle} aria-label={lengthTitle}>{t(`${stringLength} 单元`, `${stringLength} units`)}</span>}
        <span className="cx-reader-json-actions"><button type="button" title={t('复制字段值（原始 JSON）', 'Copy value (original JSON)')} aria-label={t(`复制字段值 ${node.path || '/'}`, `Copy value ${node.path || '/'}`)} onClick={() => copy(rawJsonValue(document, node), t('JSON 字段值（原始 JSON）', 'JSON field value (original JSON)'))}>⧉</button><button type="button" title={t('复制字段路径（JSON Pointer）', 'Copy field path (JSON Pointer)')} aria-label={t(`复制字段路径 ${node.path || '/'}`, `Copy path ${node.path || '/'}`)} onClick={() => copy(node.path, t('字段路径（JSON Pointer）', 'Field path (JSON Pointer)'))}>/</button></span>
      </div>
      {expanded && node.children && <div className="cx-reader-json-children">
        {node.kind === 'object' && node.children.length > PAGE && <KeyIndex nodes={node.children} onSelect={reveal} />}
        {offset > 0 && <button type="button" className="cx-button cx-reader-json-more" onClick={() => patch({ offsets: { ...state.offsets, [node.id]: Math.max(0, offset - PAGE) }, limits: { ...state.limits, [node.id]: PAGE } })}>{t('上一组值', 'Previous values')}</button>}
        {node.children.slice(offset, offset + limit).map(renderNode)}
        {(offset > 0 || node.children.length > offset + limit) && <p className="cx-data-note">{t(`已解析全部 ${node.children.length} 项 · 当前显示值 ${offset + 1}–${Math.min(offset + limit, node.children.length)}`, `All ${node.children.length} items parsed · showing values ${offset + 1}–${Math.min(offset + limit, node.children.length)}`)}</p>}
        {node.children.length > offset + limit && <button type="button" className="cx-button cx-reader-json-more" onClick={() => patch({ offsets: { ...state.offsets, [node.id]: offset + PAGE }, limits: { ...state.limits, [node.id]: PAGE } })}>{t(`下一组 ${Math.min(PAGE, node.children.length - offset - limit)} 项`, `Next ${Math.min(PAGE, node.children.length - offset - limit)} items`)}</button>}
      </div>}
      {expanded && node.kind === 'string' && <div className="cx-reader-json-string">
        <div className="cx-reader-string-toolbar"><label>{t('字符串视图', 'String view')} <select value={mode} onChange={event => patch({ stringModes: { ...state.stringModes, [node.id]: event.target.value as JsonStringMode } })}><option value="auto">{t(`正文阅读 · 自动（${autoLabel}）`, `Read body · Auto (${autoLabel})`)}</option><option value="text">{t('正文阅读 · 纯文本', 'Read body · Text')}</option><option value="markdown">Markdown</option><option value="code">{t('代码', 'Code')}</option><option value="source">{t('字符串原文（JSON）', 'String source (JSON)')}</option></select></label><button type="button" className="cx-button" onClick={() => copy(node.value ?? '', t('字符串正文（已解码）', 'String body (decoded)'))}>{t('复制字符串正文', 'Copy string body')}</button><button type="button" className="cx-button" onClick={() => copy(rawJsonValue(document, node), t('JSON 字符串原文', 'JSON string source'))}>{t('复制 JSON 原文', 'Copy JSON source')}</button></div>
        <p className="cx-data-note">{readingMode === 'source' ? t('原始 JSON 字符串，包含引号和原有转义；切换正文阅读可显示已记录的换行。', 'Original JSON string, including quotes and escapes. Read body shows its recorded line breaks.') : t('正文按 JSON 规则解码一次；实际换行直接显示，字面的反斜杠不再转换。', 'Body decoded once using JSON rules. Recorded line breaks are displayed; literal backslashes are not converted again.')}</p>
        {(readingMode === 'markdown' || readingMode === 'code') && renderString ? renderString(node.value ?? '', readingMode, stringView.language, { state: state.stringRich?.[node.id], onState: next => patch({ stringRich: { ...state.stringRich, [node.id]: next } }), visibleLines: state.stringCodeLines?.[node.id], onVisibleLines: count => patch({ stringCodeLines: { ...state.stringCodeLines, [node.id]: count } }) }) : renderSource(readingMode === 'source' ? rawJsonValue(document, node) : node.value ?? '', node.id, readingMode !== 'source')}

      </div>}
    </div>;
  };
  return <div ref={container} className="cx-reader-json">
    <div className="cx-reader-json-toolbar"><label>{t('展开深度', 'Expand depth')} <select value={state.depth ?? 1} onChange={event => patch({ depth: Number(event.target.value), expanded: {} })}>{[0, 1, 2, 3, 4].map(depth => <option key={depth} value={depth}>{depth}</option>)}</select></label><button type="button" className="cx-button" aria-pressed={state.directory ?? false} onClick={() => patch({ directory: !state.directory })}>{t('字段目录', 'Field directory')} ({directory.length})</button><button type="button" className="cx-button" onClick={() => copy(text, t('完整可读取 JSON 正文', 'Entire readable JSON body'))}>{t('复制整个 JSON 正文', 'Copy entire JSON body')}</button></div>
    <label className="cx-reader-json-search">{t('搜索字段和值', 'Find keys and values')}<input type="search" value={state.query ?? ''} onChange={event => patch({ query: event.target.value })} placeholder={t('搜索全部已解析字段和值…', 'Search all parsed keys and values…')} /></label>
    {deferredQuery.trim() && <div className="cx-reader-json-results"><p role="status">{t(`${found.total} 处命中 · 完整可读取正文`, `${found.total} matches · entire readable body`)}{found.total > found.matches.length && t(` · 列出前 ${found.matches.length} 处，请缩小搜索`, ` · first ${found.matches.length} shown; refine your query`)}</p>{found.matches.map(node => <button className="cx-reader-path" type="button" key={node.id} onClick={() => reveal(node)}>{node.path || '(root)'} <small>{summary(node)}</small></button>)}</div>}
    {state.directory && <nav className="cx-reader-json-directory" aria-label={t('JSON 字段目录', 'JSON field directory')}>{directory.slice(0, directoryLimit).map(node => <button type="button" className="cx-reader-path" key={node.id} onClick={() => reveal(node)}>{node.path}</button>)}{directory.length > directoryLimit && <button type="button" className="cx-button" onClick={() => patch({ directoryLimit: directoryLimit + PAGE })}>{t(`更多路径（${directoryLimit} / ${directory.length}）`, `More paths (${directoryLimit} / ${directory.length})`)}</button>}</nav>}
    {document.duplicateKeys && <p className="cx-data-note">{t('包含重复字段名；所有出现位置均保留。JSON Pointer 路径可能相同，复制值按所选位置读取。', 'Duplicate keys are preserved. JSON Pointer paths can be identical; copying a value uses the selected occurrence.')}</p>}
    {metadata?.fields?.length ? <p className="cx-data-note">{t('带 i 的字段有来源说明；其余字段未提供可靠定义。', 'Fields marked i have sourced definitions; other fields have no reliable definition available.')}</p> : null}
    <div className="cx-reader-json-tree">{renderNode(document.root)}</div>
    {rendered > 2000 && <p className="cx-data-note">{t('已达到 2,000 个可见节点上限。请降低展开深度或使用字段目录/搜索定位；复制包含所有正文。', 'The 2,000 visible-node limit was reached. Reduce expansion depth or use the directory/search to locate fields; copying includes the entire body.')}</p>}
  </div>;
}

export default JsonReader;
