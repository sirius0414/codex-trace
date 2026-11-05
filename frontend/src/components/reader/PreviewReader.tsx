/** 详情阅读编排：各 part 独立识别、折叠和切换；Raw 仍由 Inspector 展示原始关联记录。 */
import { useDeferredValue, useEffect, useLayoutEffect, useMemo, useRef, type CSSProperties } from 'react';
import type { ContentPart, TraceEvent } from '../../../../contracts/types';
import type { DetailData, DetailPart } from '../../ui-types';
import { usePreferences } from '../../preferences';
import { JsonReader } from './JsonReader';
import { RichReader, CodeReader } from './RichReader';
import { detectReader, previewCollapseDefaults, previewParts, readableComplete, searchPreview, type ReaderMode, type TextHit } from './preview-model';
import { parseJsonDocument } from './json-model';
import { revealJsonHit } from './json-layout';
import { protocolBody } from './protocol-body';
import { initializePreviewState, type BlockReadingState, type ReadingState } from './reading-state';

type Props = { event: TraceEvent; detail: DetailData | null; loading: boolean; state: ReadingState; onState: (state: ReadingState) => void; copy: (text: string, label: string) => void; onMore: (part: ContentPart, readAll?: boolean) => void };
export default function PreviewReader({ event, detail, loading, state: savedState, onState, copy, onMore }: Props) {
  const { t, readerFontSize, readerWrap, readerLineNumbers } = usePreferences();
  const parts = detail?.parts ?? {};
  const collapseDefaults = previewCollapseDefaults(event, parts);
  const state = initializePreviewState(event, parts, savedState);
  useLayoutEffect(() => {
    if (state !== savedState) onState(state);
  }, [state, savedState, onState]);
  const root = useRef<HTMLDivElement>(null);
  const previousHit = useRef(state.hit);
  const labels: Record<string, string> = { input: t('输入', 'Input'), output: t('输出', 'Output'), error: t('错误', 'Error') };
  const query = useDeferredValue(state.query);
  const search = useMemo(() => searchPreview(parts, query), [parts, query]);
  const partNames = previewParts(event, parts);
  const isTool = event.kind === 'toolCall' || event.kind === 'toolResult' || Boolean(event.toolName);
  const patchBlock = (part: string, patch: Partial<BlockReadingState>) => onState({ ...state, blocks: { ...state.blocks, [part]: { ...state.blocks[part], ...patch } } });
  const update = (patch: Partial<ReadingState>) => onState({ ...state, ...patch });
  const focusHit = (hit: TextHit) => {
    const block = state.blocks[hit.part] ?? {};
    const value = parts[hit.part];
    const metadata = value?.reader ?? event.content.find(ref => ref.part === hit.part)?.reader;
    const mode = block.mode && block.mode !== 'auto' ? block.mode : detectReader(value?.text ?? '', metadata);
    const parsed = value && mode === 'json' && readableComplete(value) && !block.source ? parseJsonDocument(value.text).document : undefined;
    const json = parsed ? revealJsonHit(parsed, block.json ?? {}, hit) : null;
    // JSON 命中展开局部长值；其余正文使用精确源码定位，包含 Markdown 语法和包装内容。
    onState({ ...state, hit: { ...hit }, blocks: { ...state.blocks, [hit.part]: { ...block, collapsed: false,
      source: !json, json: json ?? block.json, sourceOffset: Math.max(0, hit.start - 200), sourceLimit: Math.max(100000, hit.end - hit.start + 400) } } });
  };
  useEffect(() => {
    if (!state.hit || state.hit === previousHit.current) return;
    previousHit.current = state.hit;
    const target = root.current?.querySelector<HTMLElement>('[data-reader-hit]');
    target?.scrollIntoView({ block: 'center' }); target?.focus({ preventScroll: true });
  }, [state.hit]);

  function renderBody(part: ContentPart, value: DetailPart, block: BlockReadingState) {
    const metadata = value.reader ?? event.content.find(ref => ref.part === part)?.reader;
    const mode = block.mode && block.mode !== 'auto' ? block.mode : detectReader(value.text, metadata);
    const complete = readableComplete(value);
    const shared = { wrap: readerWrap, lineNumbers: readerLineNumbers, copy, chrome: false };
    const raw = block.source || mode === 'text' || mode === 'json' && !complete;
    const limit = block.sourceLimit ?? 100000;
    const offset = Math.min(block.sourceOffset ?? 0, Math.max(0, value.text.length - 1));
    const shown = value.text.slice(offset, offset + limit);
    const hit = state.hit?.part === part && value.text.slice(state.hit.start, state.hit.end) === state.hit.match ? state.hit : null;
    if (raw) return <>
      {mode === 'json' && !complete && !block.source && <p className="cx-reader-notice">{t('JSON 尚未读齐，显示已加载原文。', 'JSON is not fully loaded; showing loaded source.')}</p>}
      {offset > 0 && <button className="cx-button" onClick={() => patchBlock(part, { sourceOffset: Math.max(0, offset - limit) })}>{t('显示前一段原文', 'Previous source section')}</button>}
      <pre tabIndex={0} className={`cx-reader-source ${readerWrap ? 'wrap' : ''} ${!block.source && mode === 'text' ? 'cx-reader-prose' : ''}`} style={{ fontSize: !block.source && mode === 'text' ? readerFontSize : readerFontSize - 2 }}>{hit && hit.start >= offset && hit.end <= offset + shown.length ? <>{shown.slice(0, hit.start - offset)}<mark data-reader-hit tabIndex={-1}>{shown.slice(hit.start - offset, hit.end - offset)}</mark>{shown.slice(hit.end - offset)}</> : shown || t('（空正文）', '(Empty body)')}</pre>
      {offset + shown.length < value.text.length && <button className="cx-button" onClick={() => patchBlock(part, { sourceOffset: offset + limit })}>{t('继续显示已加载原文', 'Show more loaded source')}</button>}
    </>;
    if (mode === 'json') {
      const body = !state.hit && (!block.mode || block.mode === 'auto') ? protocolBody(value.text, metadata) : null;
      if (body !== null && ['code', 'diff'].includes(detectReader(body))) return <CodeReader text={body} language={detectReader(body) === 'diff' ? 'diff' : undefined} visibleLines={block.codeLines} onVisibleLines={codeLines => patchBlock(part, { codeLines })} {...shared} />;
      if (body !== null) return <RichReader text={body} state={block.rich} onState={rich => patchBlock(part, { rich })} fontSize={readerFontSize} {...shared} />;
      return <JsonReader text={value.text} state={block.json} onState={json => patchBlock(part, { json })} wrap={readerWrap} lineNumbers={readerLineNumbers} hit={hit} />;
    }
    if (mode === 'markdown') return <RichReader text={value.text} state={block.rich} onState={rich => patchBlock(part, { rich })} fontSize={readerFontSize} {...shared} />;
    return <CodeReader text={value.text} language={block.language || (mode === 'diff' ? 'diff' : metadata?.language)} visibleLines={block.codeLines} onVisibleLines={codeLines => patchBlock(part, { codeLines })} {...shared} />;
  }

  return <div ref={root} className="cx-reader" style={{ '--cx-reader-font': `${readerFontSize}px`, '--cx-reader-code-font': `${readerFontSize - 2}px` } as CSSProperties}>
    <div className="cx-reader-search"><input type="search" aria-label={t('搜索条目已加载正文', 'Search loaded entry body')} placeholder={t('搜索本条目已加载正文…', 'Search loaded entry body…')} value={state.query} onChange={e => update({ query: e.target.value, hit: undefined })} />
      {query && <div className="cx-reader-search-results"><p role="status">{t(`${search.total} 处命中 · 已加载输入、输出和错误${search.total > search.hits.length ? `；显示前 ${search.hits.length} 处，请缩小查询` : ''}`, `${search.total} matches · loaded input, output and error${search.total > search.hits.length ? `; showing first ${search.hits.length}, refine your query` : ''}`)}</p>{search.hits.map((hit, index) => <button key={`${hit.part}:${hit.start}`} onClick={() => focusHit(hit)}><b>{labels[hit.part]} {index + 1}</b> {hit.before}<mark>{hit.match}</mark>{hit.after}</button>)}</div>}
    </div>
    {!partNames.length && <p className="cx-data-note">{loading ? t('正在读取内容…', 'Loading content…') : t('该记录尚无可读取正文。', 'This record has no readable body yet.')}</p>}
    {partNames.map(part => {
      const value = parts[part], ref = event.content.find(item => item.part === part), metadata = value?.reader ?? ref?.reader;
      const block = state.blocks[part] ?? {};
      const collapsed = block.collapsed ?? block.defaultCollapsed ?? collapseDefaults[part] ?? false;
      const completeness = value?.completeness ?? ref?.completeness ?? 'notRecorded';
      const sourceTruncated = value?.sourceTruncated ?? ref?.sourceTruncated;
      const more = value ? !readableComplete(value) : Boolean(ref?.readableBytes);
      const mode = block.mode ?? 'auto';
      const autoMode = detectReader(value?.text ?? '', metadata);
      const modes: [ReaderMode, string][] = [['auto', t('自动', 'Auto')], ['markdown', 'Markdown'], ['code', t('代码 / 命令', 'Code / command')], ['json', 'JSON'], ['diff', 'Diff'], ['text', t('纯文本', 'Text')]];
      return <section className={`cx-reader-block ${part === 'error' ? 'cx-reader-error' : ''}`} key={part} data-reader-part={part}>
        <div className="cx-reader-block-head"><button className="cx-reader-collapse" aria-expanded={!collapsed} onClick={() => patchBlock(part, { collapsed: !collapsed })}><span aria-hidden="true">{collapsed ? '▸' : '▾'}</span> {isTool || partNames.length > 1 ? labels[part] : part === 'error' ? labels.error : t('正文', 'Body')}</button>
          {!collapsed && <div className="cx-reader-block-tools">
            <div className="cx-reader-switch" role="group" aria-label={t(`${labels[part]}视图`, `${labels[part]} view`)}><button aria-pressed={!block.source} onClick={() => patchBlock(part, { source: false })}>{t('阅读', 'Read')}</button><button aria-pressed={Boolean(block.source)} onClick={() => patchBlock(part, { source: true })}>{t('原文', 'Source')}</button></div>
            {value && <button className="cx-button cx-reader-copy" title={t('复制全部已加载原始正文', 'Copy all loaded original text')} onClick={() => copy(value.text, t(`已加载${labels[part]}正文`, `loaded ${labels[part].toLowerCase()} body`))}>{t('复制', 'Copy')}</button>}
            <details className="cx-reader-more" onKeyDown={event => { if (event.key === 'Escape') { event.preventDefault(); event.stopPropagation(); event.currentTarget.open = false; event.currentTarget.querySelector('summary')?.focus(); } }}>
              <summary aria-label={t(`${labels[part]}更多`, `${labels[part]} more`)}>{t('更多', 'More')}</summary>
              <div className="cx-reader-format-options"><label>{t('格式', 'Format')}<select aria-label={t(`${labels[part]}格式`, `${labels[part]} format`)} value={mode} onChange={e => patchBlock(part, { mode: e.target.value as ReaderMode, source: false })}>{modes.map(([id, label]) => <option value={id} key={id}>{label}</option>)}</select></label>
                <label>{t('代码语言', 'Code language')}<select aria-label={t(`${labels[part]}代码语言`, `${labels[part]} code language`)} value={block.language ?? ''} onChange={e => patchBlock(part, { language: e.target.value, mode: 'code', source: false })}><option value="">{t('自动语言', 'Auto language')}</option>{['text', 'bash', 'javascript', 'typescript', 'python', 'json', 'diff', 'css', 'xml', 'sql', 'yaml', 'rust', 'go', 'swift'].map(language => <option key={language}>{language}</option>)}</select></label>
                <small>{t('自动识别', 'Detected')}: {autoMode}</small>
              </div>
            </details>
          </div>}
        </div>
        {(more || sourceTruncated || completeness === 'notRecorded' || completeness === 'fragment') && <div className="cx-reader-integrity">
          {more && <span>{t('尚有内容未加载', 'More content is not loaded')}</span>}
          {sourceTruncated && <span>{t('来源本身已截断', 'Source was truncated')}</span>}
          {completeness === 'notRecorded' && <span>{t('输出尚未保存', 'Output not saved')}</span>}
          {completeness === 'fragment' && <span>{t('来源仅保存片段', 'Only a source fragment is saved')}</span>}
        </div>}
        {!collapsed && <>
          {metadata?.auxiliary?.length ? <dl className="cx-reader-aux">{metadata.auxiliary.map((item, i) => <div key={i}><dt>{item.name}</dt><dd>{item.value}</dd></div>)}</dl> : null}
          {value ? renderBody(part, value, block) : <p className="cx-data-note">{loading ? t('正在读取正文…', 'Loading body…') : t('当前没有可读取正文。', 'No readable body is available.')}</p>}
        </>}
        {sourceTruncated && <p className="cx-data-note">{t('继续加载只能读取已保存字节，无法恢复来源缺失部分。', 'Loading more can only retrieve saved bytes; missing source content cannot be recovered.')}</p>}
        {value?.nextCursor && <button className="cx-button cx-more-content" disabled={loading} onClick={() => onMore(part)}>{loading ? t('正在读取…', 'Loading…') : t(`继续读取${labels[part]}`, `Load more ${labels[part].toLowerCase()}`)}</button>}
        {value?.nextCursor && value.readableBytes != null && value.readableBytes <= 8_000_000 && <button className="cx-button cx-more-content" disabled={loading} onClick={() => onMore(part, true)}>{t('读齐此块已保存正文', 'Load all saved text in this block')}</button>}
      </section>;
    })}
  </div>;
}
