/** 只读正文阅读器：安全 Markdown 词法节点转 React；代码复制始终取原文，与可见行数独立。 */
import { Fragment, createElement, useId, useMemo, useRef, useState, type ReactNode } from 'react';
import type { Token, Tokens } from 'marked';
import { usePreferences } from '../../preferences';
import { codeTokenText, detectCodeLanguage, highlightedLines, markdownDocument, safeReaderUrl } from './reader-model';
import './rich-reader.css';

export type ReaderCopy = (text: string, label: string) => void;
export type RichReaderState = {
  collapsed?: Record<string, boolean>;
  visibleBlocks?: number;
  tableRows?: Record<string, number>;
  codeLines?: Record<string, number>;
  nestedCounts?: Record<string, number>;
};
export type CodeReaderProps = {
  text: string; language?: string; wrap?: boolean; lineNumbers?: boolean; copy: ReaderCopy; chrome?: boolean;
  visibleLines?: number; onVisibleLines?: (count: number) => void; maxLines?: number;
};
export function CodeReader({ text, language, wrap = true, lineNumbers = false, copy, visibleLines, onVisibleLines, maxLines = 200, chrome = true }: CodeReaderProps) {
  const { t } = usePreferences();
  const [localLines, setLocalLines] = useState(maxLines);
  const count = visibleLines ?? localLines;
  const rawLines = useMemo(() => text.split('\n'), [text]);
  const shownText = useMemo(() => rawLines.slice(0, count).join('\n'), [rawLines, count]);
  const detected = useMemo(() => detectCodeLanguage(text, language), [text, language]);
  const actualLanguage = detected.language;
  const lines = useMemo(() => highlightedLines(shownText, actualLanguage), [shownText, actualLanguage]);
  function more() { const next = count + 300; if (onVisibleLines) onVisibleLines(next); else setLocalLines(next); }
  return <div className="cx-reader-code">
    {chrome && <div className="cx-reader-code-head"><span>{language || actualLanguage || t('代码 / 文本', 'Code / text')}{actualLanguage && detected.basis !== 'explicit' ? t(' · 自动识别', ' · detected') : language && !actualLanguage ? t(' · 无高亮', ' · plain') : ''}</span><button type="button" className="cx-button" onClick={() => copy(text, t('代码正文', 'Code text'))}>{t('复制代码', 'Copy code')}</button></div>}
    <pre className={`cx-reader-pre ${wrap ? 'wrap' : ''} ${lineNumbers ? 'numbered' : ''}`} tabIndex={0} aria-label={t('代码正文', 'Code text')}><code>{lines.map((line, index) => <Fragment key={index}><span className="cx-reader-code-line" data-line={index + 1}>{line.length ? line.map((piece, j) => <span className={piece.className || undefined} key={j}>{piece.text}</span>) : ''}</span>{index < lines.length - 1 ? '\n' : ''}</Fragment>)}</code></pre>
    {rawLines.length > count && <div className="cx-reader-deferred"><span>{t(`已加载 ${rawLines.length} 行，显示 ${count} 行；复制包含全部已加载代码。`, `${rawLines.length} loaded lines; ${count} shown. Copy includes all loaded code.`)}</span><button className="cx-button" onClick={more}>{t('再显示 300 行', 'Show 300 more lines')}</button></div>}
    {shownText.length > 100_000 && actualLanguage && <p className="cx-data-note">{t('此代码块较大，暂以无高亮方式显示以保持响应。', 'This large code block is shown without highlighting to keep the reader responsive.')}</p>}
  </div>;
}

export type RichReaderProps = {
  text: string; state?: RichReaderState; onState?: (state: RichReaderState) => void;
  copy: ReaderCopy; chrome?: boolean; wrap?: boolean; lineNumbers?: boolean; fontSize?: number;
};
export function RichReader({ text, state: suppliedState, onState, copy, wrap = true, lineNumbers = false, fontSize, chrome = true }: RichReaderProps) {
  const { t } = usePreferences();
  const [localState, setLocalState] = useState<RichReaderState>({});
  const state = suppliedState ?? localState;
  const setState = (next: RichReaderState) => { if (onState) onState(next); else setLocalState(next); };
  const document = useMemo(() => markdownDocument(text), [text]);
  const baseId = useId();
  const container = useRef<HTMLDivElement>(null);
  const visibleBlocks = state.visibleBlocks ?? 160;
  const fold = (id: string) => setState({ ...state, collapsed: { ...state.collapsed, [id]: !state.collapsed?.[id] } });
  function navigate(index: number) {
    const collapsed = { ...state.collapsed };
    document.blocks.forEach((block, i) => { if (i <= index && block.sectionEnd > index) collapsed[block.id] = false; });
    setState({ ...state, collapsed, visibleBlocks: Math.max(visibleBlocks, index + 30) });
    requestAnimationFrame(() => {
      const heading = container.current?.querySelector<HTMLElement>(`[data-block-index="${index}"]`);
      heading?.scrollIntoView({ block: 'start', behavior: 'auto' }); heading?.focus({ preventScroll: true });
    });
  }
  function nodes(tokens: Token[] | undefined, parentKey: string): ReactNode {
    const values = tokens ?? [], shown = state.nestedCounts?.[parentKey] ?? 200;
    return <>{values.slice(0, shown).map((token, index) => render(token, `${parentKey}-${index}`))}{values.length > shown && <button className="cx-button" onClick={() => setState({ ...state, nestedCounts: { ...state.nestedCounts, [parentKey]: shown + 200 } })}>{t(`显示更多已加载内容（${shown}/${values.length}）`, `Show more loaded content (${shown}/${values.length})`)}</button>}</>;
  }
  function render(token: Token, key: string): ReactNode {
    switch (token.type) {
      case 'space': return null;
      case 'text': return <Fragment key={key}>{token.tokens ? nodes(token.tokens, key) : token.text}</Fragment>;
      case 'escape': return <Fragment key={key}>{token.text}</Fragment>;
      case 'paragraph': return <p key={key}>{nodes(token.tokens, key)}</p>;
      case 'strong': return <strong key={key}>{nodes(token.tokens, key)}</strong>;
      case 'em': return <em key={key}>{nodes(token.tokens, key)}</em>;
      case 'del': return <del key={key}>{nodes(token.tokens, key)}</del>;
      case 'codespan': return <code key={key}>{token.text}</code>;
      case 'br': return <br key={key} />;
      case 'hr': return <hr key={key} />;
      case 'blockquote': return <blockquote key={key}>{nodes(token.tokens, key)}</blockquote>;
      case 'heading': return createElement(`h${Math.min(6, token.depth)}`, { key }, nodes(token.tokens, key));
      case 'list': {
        const list = token as Tokens.List;
        const shown = state.nestedCounts?.[key] ?? 100;
        return <Fragment key={key}>{createElement(list.ordered ? 'ol' : 'ul', { ...(list.ordered ? { start: Number(list.start) || 1 } : {}) }, list.items.slice(0, shown).map((item, i) => <li key={`${key}-${i}`}>{item.task && <span className="cx-reader-task" aria-label={item.checked ? t('已完成', 'Completed') : t('未完成', 'Not completed')}>{item.checked ? '☑ ' : '☐ '}</span>}{nodes(item.tokens, `${key}-${i}`)}</li>))}{list.items.length > shown && <button className="cx-button" onClick={() => setState({ ...state, nestedCounts: { ...state.nestedCounts, [key]: shown + 200 } })}>{t(`显示更多列表项（${shown}/${list.items.length}）`, `Show more list items (${shown}/${list.items.length})`)}</button>}</Fragment>;
      }
      case 'code': return <CodeReader key={key} text={codeTokenText(token)} language={token.lang} wrap={wrap} lineNumbers={lineNumbers} copy={copy} chrome={chrome} visibleLines={state.codeLines?.[key]} onVisibleLines={count => setState({ ...state, codeLines: { ...state.codeLines, [key]: count } })} />;
      case 'frontmatter': return <section key={key} data-reader-frontmatter aria-label={t('YAML 元数据原文', 'YAML frontmatter source')}><p className="cx-data-note">{t('YAML 元数据 · 原始分隔符和字段均保留', 'YAML frontmatter · original delimiters and fields retained')}</p><CodeReader text={token.raw} language="yaml" wrap={wrap} lineNumbers={lineNumbers} copy={copy} chrome={chrome} visibleLines={state.codeLines?.[key]} onVisibleLines={count => setState({ ...state, codeLines: { ...state.codeLines, [key]: count } })} /></section>;
      case 'link': {
        const href = safeReaderUrl(token.href);
        return href ? <a key={key} href={href} title={token.title || undefined} target="_blank" rel="noreferrer noopener">{nodes(token.tokens, key)}</a> : <span key={key}>{nodes(token.tokens, key)} <code>{`(${token.href})`}</code></span>;
      }
      case 'image': {
        const href = safeReaderUrl(token.href);
        const label = `${t('图片', 'Image')}: ${token.text || token.href}`;
        return <span key={key} className="cx-reader-image">{href ? <a href={href} target="_blank" rel="noreferrer noopener">{label}</a> : <span>{label} <code>{token.href}</code></span>}</span>;
      }
      case 'html': return token.block ? <pre className="cx-reader-literal" key={key}>{token.raw}</pre> : <span className="cx-reader-tag" key={key}>{token.raw}</span>;
      case 'def': return <pre className="cx-reader-literal" key={key}>{token.raw}</pre>;
      case 'table': {
        const table = token as Tokens.Table, shown = state.tableRows?.[key] ?? 100;
        return <Fragment key={key}><div className="cx-reader-table" tabIndex={0} aria-label={t('表格，可横向滚动', 'Table, horizontally scrollable')}><table><thead><tr>{table.header.map((cell, i) => <th key={i} style={{ textAlign: cell.align ?? undefined }}>{nodes(cell.tokens, `${key}-h-${i}`)}</th>)}</tr></thead><tbody>{table.rows.slice(0, shown).map((row, i) => <tr key={i}>{row.map((cell, j) => <td key={j} style={{ textAlign: cell.align ?? undefined }}>{nodes(cell.tokens, `${key}-${i}-${j}`)}</td>)}</tr>)}</tbody></table></div>{table.rows.length > shown && <button className="cx-button" onClick={() => setState({ ...state, tableRows: { ...state.tableRows, [key]: shown + 200 } })}>{t(`显示更多表格行（${shown}/${table.rows.length}）`, `Show more table rows (${shown}/${table.rows.length})`)}</button>}</Fragment>;
      }
      default: return <span className="cx-reader-literal" key={key}>{token.raw}</span>;
    }
  }
  let hiddenUntil = -1;
  return <div ref={container} className="cx-reader-rich" style={fontSize ? { fontSize } : undefined}>
    {document.headings.length > 1 && <nav className="cx-reader-outline" aria-label={t('章节导航', 'Section navigation')}><label>{t('章节', 'Sections')}<select value="" onChange={event => { if (event.target.value) navigate(Number(event.target.value)); }}><option value="">{t(`跳转到章节（${document.headings.length}）`, `Jump to section (${document.headings.length})`)}</option>{document.headings.map(block => <option key={block.id} value={document.blocks.indexOf(block)}>{`${'　'.repeat(Math.max(0, block.token.depth - 1))}${block.token.text}`}</option>)}</select></label><button className="cx-button" onClick={() => setState({ ...state, collapsed: {} })}>{t('展开章节', 'Expand sections')}</button><button className="cx-button" onClick={() => setState({ ...state, collapsed: Object.fromEntries(document.headings.map(block => [block.id, true])) })}>{t('收起章节', 'Collapse sections')}</button></nav>}
    {document.blocks.slice(0, visibleBlocks).map((block, index) => {
      if (index < hiddenUntil) return null;
      const collapsed = Boolean(state.collapsed?.[block.id]);
      if (block.token.type === 'heading') {
        if (collapsed) hiddenUntil = block.sectionEnd;
        return <section className="cx-reader-section-heading" key={block.id} data-reader-offset={block.offset} data-block-index={index} tabIndex={-1} id={`${baseId}-${block.id}`}>{createElement(`h${Math.min(6, block.token.depth)}`, {}, <button className="cx-reader-heading-toggle" aria-expanded={!collapsed} onClick={() => fold(block.id)}><span aria-hidden="true">{collapsed ? '▸' : '▾'}</span><span>{nodes(block.token.tokens, block.id)}</span></button>)}{collapsed && <small>{t('章节已加载并折叠，复制正文仍包含全部已加载内容。', 'Section loaded and collapsed. Copy text still includes all loaded content.')}</small>}</section>;
      }
      return <div key={block.id} data-reader-offset={block.offset}>{render(block.token, block.id)}</div>;
    })}
    {document.blocks.length > visibleBlocks && <div className="cx-reader-deferred"><span>{t('剩余已加载段落暂未渲染；复制正文包含这些段落。', 'More loaded paragraphs are deferred. Copy text includes these paragraphs.')}</span><button className="cx-button" onClick={() => setState({ ...state, visibleBlocks: visibleBlocks + 160 })}>{t('显示更多段落', 'Show more paragraphs')}</button></div>}
    {document.remainder && <div className="cx-reader-deferred"><p>{t('后续已加载内容以普通文本显示，避免一次排版阻塞阅读。正文没有丢弃。', 'The remaining loaded content uses plain text to avoid blocking layout. No text is discarded.')}</p><CodeReader text={document.remainder} wrap={wrap} lineNumbers={lineNumbers} copy={copy} chrome={chrome} /></div>}
  </div>;
}
export default RichReader;
