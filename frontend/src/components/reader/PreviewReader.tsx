/** 详情阅读编排：各 part 独立识别、折叠和切换；Raw 仍由 Inspector 展示原始关联记录。 */
import { useDeferredValue, useEffect, useMemo, useRef, type CSSProperties } from 'react';
import type { ContentPart, TraceEvent } from '../../../../contracts/types';
import type { DetailData, DetailPart } from '../../ui-types';
import { usePreferences } from '../../preferences';
import { JsonReader } from './JsonReader';
import { RichReader, CodeReader } from './RichReader';
import { detectReader, previewParts, readableComplete, searchPreview, type ReaderMode, type TextHit } from './preview-model';
import type { BlockReadingState, ReadingState } from './reading-state';

type Props = { event: TraceEvent; detail: DetailData | null; loading: boolean; state: ReadingState; onState: (state: ReadingState) => void; copy: (text: string, label: string) => void; onMore: (part: ContentPart, readAll?: boolean) => void };
export default function PreviewReader({ event, detail, loading, state, onState, copy, onMore }: Props) {
  const { t, readerFontSize, readerWrap, readerLineNumbers } = usePreferences();
  const root = useRef<HTMLDivElement>(null);
  const previousHit = useRef(state.hit);
  const parts = detail?.parts ?? {};
  const labels: Record<string, string> = { input: t('输入', 'Input'), output: t('输出', 'Output'), error: t('错误', 'Error') };
  const query = useDeferredValue(state.query);
  const search = useMemo(() => searchPreview(parts, query), [parts, query]);
  const partNames = previewParts(event, parts);
  const isTool = event.kind === 'toolCall' || event.kind === 'toolResult' || Boolean(event.toolName);
  const patchBlock = (part: string, patch: Partial<BlockReadingState>) => onState({ ...state, blocks: { ...state.blocks, [part]: { ...state.blocks[part], ...patch } } });
  const update = (patch: Partial<ReadingState>) => onState({ ...state, ...patch });
  const focusHit = (hit: TextHit) => {
    // 使用原文字符偏移定位。切到正文原文可定位 Markdown 语法及被折叠内容，未改写查询目标。
    onState({ ...state, hit, blocks: { ...state.blocks, [hit.part]: { ...state.blocks[hit.part], collapsed: false, source: true, sourceLimit: Math.max(100000, hit.end + 4000) } } });
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
    const shared = { wrap: readerWrap, lineNumbers: readerLineNumbers, copy };
    const raw = block.source || mode === 'text' || mode === 'json' && !complete;
    const limit = block.sourceLimit ?? 100000;
    const shown = value.text.slice(0, limit);
    const hit = state.hit?.part === part && state.hit.end <= shown.length && value.text.slice(state.hit.start, state.hit.end) === state.hit.match ? state.hit : null;
    if (raw) return <>
      {mode === 'json' && !complete && !block.source && <p className="cx-reader-notice">{t('JSON 正文尚未读齐，先显示已加载文本；字段目录尚不可用。', 'JSON is not fully loaded. Showing loaded text; the field directory is not available yet.')}</p>}
      <pre className={`cx-reader-source ${readerWrap ? 'wrap' : ''}`} style={{ fontSize: readerFontSize - 2 }}>{hit ? <>{shown.slice(0, hit.start)}<mark data-reader-hit tabIndex={-1}>{shown.slice(hit.start, hit.end)}</mark>{shown.slice(hit.end)}</> : shown || t('（空正文）', '(Empty body)')}</pre>
      {shown.length < value.text.length && <button className="cx-button" onClick={() => patchBlock(part, { sourceLimit: limit + 100000 })}>{t(`显示更多已加载文本（已显示 ${shown.length} / ${value.text.length} 字符；复制包含全部已加载正文）`, `Show more loaded text (${shown.length} / ${value.text.length} characters shown; copy includes all loaded text)`)}</button>}
    </>;
    if (mode === 'json') return <JsonReader text={value.text} state={block.json} onState={json => patchBlock(part, { json })} copy={copy} metadata={metadata} wrap={readerWrap}
      renderString={(text, mode, language, context) => mode === 'markdown' ? <RichReader text={text} fontSize={readerFontSize} state={context.state} onState={context.onState} {...shared} /> : <CodeReader text={text} language={language} visibleLines={context.visibleLines} onVisibleLines={context.onVisibleLines} {...shared} />} />;
    if (mode === 'markdown') return <RichReader text={value.text} state={block.rich} onState={rich => patchBlock(part, { rich })} fontSize={readerFontSize} {...shared} />;
    return <CodeReader text={value.text} language={block.language || (mode === 'diff' ? 'diff' : metadata?.language)} visibleLines={block.codeLines} onVisibleLines={codeLines => patchBlock(part, { codeLines })} {...shared} />;
  }

  function spotlight(part: ContentPart, value: DetailPart) {
    const meta = value.reader ?? event.content.find(ref => ref.part === part)?.reader;
    if (part !== 'input' || !meta?.adapter || !readableComplete(value)) return null;
    // 参数仍完整保留在下方 JSON；这里只抽取可靠工具定义标注的字符串，不拼装 shell 参数数组。
    if (meta.adapter === 'patch' && meta.format === 'json' && value.text.length <= 1_000_000) {
      try {
        const changes: unknown = JSON.parse(value.text);
        if (!Array.isArray(changes)) return null;
        const files = changes.slice(0, 8).flatMap((change, index) => {
          const definition = meta.fields?.find(field => field.path === `/${index}/diff`);
          if (!definition || !change || typeof change !== 'object' || typeof change.diff !== 'string' || typeof change.path !== 'string') return [];
          const file = state.blocks[part]?.patchFiles?.[index] ?? {};
          const updateFile = (patch: typeof file) => patchBlock(part, { patchFiles: { ...state.blocks[part]?.patchFiles, [index]: { ...file, ...patch } } });
          return [<section className="cx-reader-file" key={index}><button className="cx-reader-collapse" aria-expanded={!file.collapsed} onClick={() => updateFile({ collapsed: !file.collapsed })}>{file.collapsed ? '▸' : '▾'} {change.path}</button>{!file.collapsed && <CodeReader text={change.diff} language={definition.format === 'diff' ? 'diff' : definition.language} wrap={readerWrap} lineNumbers={readerLineNumbers} copy={copy} visibleLines={file.lines} onVisibleLines={lines => updateFile({ lines })} />}</section>];
        });
        return files.length ? <div className="cx-reader-command"><h3>{t('已记录的文件修改', 'Recorded file changes')}</h3>{files}<p className="cx-data-note">{t('快捷阅读最多展示 8 个文件；全部修改和参数原文保留在下方字段树。', 'Quick reading shows up to 8 files; all changes and original parameters remain in the field tree below.')}</p></div> : null;
      } catch { return null; }
    }
    if (meta.adapter !== 'command' || meta.format !== 'json') return null;
    const field = meta.fields?.find(field => field.format === 'code' && /^\/[\w]+$/.test(field.path));
    if (!field) return null;
    try {
      const object: unknown = JSON.parse(value.text);
      const command = object && typeof object === 'object' && !Array.isArray(object) ? (object as Record<string, unknown>)[field.path.slice(1)] : null;
      if (typeof command !== 'string') return null;
      return <div className="cx-reader-command"><h3>{t('命令正文', 'Command body')}</h3><CodeReader text={command} language={field.language} wrap={readerWrap} lineNumbers={readerLineNumbers} visibleLines={state.blocks[part]?.commandLines} onVisibleLines={commandLines => patchBlock(part, { commandLines })} copy={copy} /><p className="cx-data-note">{t('下方保留全部参数及参数原文。', 'All parameters and their source text are available below.')}</p></div>;
    } catch { return null; }
  }

  return <div ref={root} className="cx-reader" style={{ '--cx-reader-font': `${readerFontSize}px`, '--cx-reader-code-font': `${readerFontSize - 2}px` } as CSSProperties}>
    <div className="cx-reader-search"><input type="search" aria-label={t('搜索条目已加载正文', 'Search loaded entry body')} placeholder={t('搜索本条目正文…', 'Search this entry…')} value={state.query} onChange={e => update({ query: e.target.value, hit: undefined })} />
      <small>{t('范围：已加载的输入、输出、错误；不含 Raw 及尚未加载部分。', 'Scope: loaded input, output and error; excludes Raw and unloaded content.')}</small>
      {query && <div className="cx-reader-search-results"><p role="status">{t(`${search.total} 处命中${search.total > search.hits.length ? `；显示前 ${search.hits.length} 处，请缩小查询` : ''}`, `${search.total} matches${search.total > search.hits.length ? `; showing first ${search.hits.length}, refine your query` : ''}`)}</p>{search.hits.map((hit, index) => <button key={`${hit.part}:${hit.start}`} onClick={() => focusHit(hit)}><b>{labels[hit.part]} {index + 1}</b> {hit.before}<mark>{hit.match}</mark>{hit.after}</button>)}</div>}
    </div>
    {!partNames.length && <p className="cx-data-note">{loading ? t('正在读取内容…', 'Loading content…') : t('该记录尚无可读取正文。', 'This record has no readable body yet.')}</p>}
    {partNames.map(part => {
      const value = parts[part], ref = event.content.find(item => item.part === part), metadata = value?.reader ?? ref?.reader;
      const block = state.blocks[part] ?? {};
      const collapsed = block.collapsed ?? (isTool && (event.kind === 'toolResult' ? part === 'input' : part === 'output') && event.status.value !== 'failed');
      const completeness = value?.completeness ?? ref?.completeness ?? 'notRecorded';
      const sourceTruncated = value?.sourceTruncated ?? ref?.sourceTruncated;
      const more = value ? !readableComplete(value) : Boolean(ref?.readableBytes);
      const mode = block.mode ?? 'auto';
      const autoMode = detectReader(value?.text ?? '', metadata);
      const modes: [ReaderMode, string][] = [['auto', t('自动', 'Auto')], ['markdown', 'Markdown'], ['code', t('代码 / 命令', 'Code / command')], ['json', 'JSON'], ['diff', 'Diff'], ['text', t('纯文本', 'Text')]];
      return <section className={`cx-reader-block ${part === 'error' ? 'cx-reader-error' : ''}`} key={part} data-reader-part={part}>
        <div className="cx-reader-block-head"><button className="cx-reader-collapse" aria-expanded={!collapsed} onClick={() => patchBlock(part, { collapsed: !collapsed })}><span aria-hidden="true">{collapsed ? '▸' : '▾'}</span> {isTool ? labels[part] : part === 'error' ? labels.error : t('正文', 'Body')}</button><span className="cx-reader-format" title={metadata?.origin}>{mode === 'auto' ? `${t('自动', 'Auto')} · ${autoMode}` : mode}</span></div>
        <div className="cx-reader-integrity">
          {value && <span>{t(`已加载 ${new TextEncoder().encode(value.text).length.toLocaleString()}${value.readableBytes == null ? '' : ` / ${value.readableBytes.toLocaleString()}`} 字节`, `${new TextEncoder().encode(value.text).length.toLocaleString()}${value.readableBytes == null ? '' : ` / ${value.readableBytes.toLocaleString()}`} bytes loaded`)}</span>}
          {collapsed && value && <span>{t('已加载内容已折叠', 'Loaded content collapsed')}</span>}
          {more && <span>{t('尚有内容未加载', 'More content is not loaded')}</span>}
          {sourceTruncated && <span>{t('来源本身已截断', 'Source was truncated')}</span>}
          {completeness === 'notRecorded' ? <span>{t('输出尚未保存', 'Output not saved')}</span> : completeness === 'fragment' ? <span>{t('来源仅保存片段', 'Only a source fragment is saved')}</span> : completeness === 'unknown' ? <span>{t('来源完整性未知', 'Source completeness unknown')}</span> : <span>{t('来源标记为完整', 'Source marked complete')}</span>}
        </div>
        {!collapsed && <>
          <div className="cx-reader-block-tools"><div className="cx-reader-switch" role="group" aria-label={t(`${labels[part]}视图`, `${labels[part]} view`)}><button aria-pressed={!block.source} onClick={() => patchBlock(part, { source: false })}>{t('阅读视图', 'Reading view')}</button><button aria-pressed={Boolean(block.source)} onClick={() => patchBlock(part, { source: true })}>{t('正文原文', 'Body source')}</button></div>
            <select aria-label={t(`${labels[part]}格式`, `${labels[part]} format`)} value={mode} onChange={e => patchBlock(part, { mode: e.target.value as ReaderMode, source: false })}>{modes.map(([id, label]) => <option value={id} key={id}>{label}</option>)}</select>
            {['code', 'diff'].includes(mode === 'auto' ? autoMode : mode) && <select aria-label={t(`${labels[part]}代码语言`, `${labels[part]} code language`)} value={block.language ?? ''} onChange={e => patchBlock(part, { language: e.target.value })}><option value="">{t('自动语言', 'Auto language')}</option>{['text', 'bash', 'javascript', 'typescript', 'python', 'json', 'diff', 'css', 'xml', 'sql', 'yaml', 'rust', 'go', 'swift'].map(language => <option key={language}>{language}</option>)}</select>}
            {value && <button className="cx-button" onClick={() => copy(value.text, t(`已加载${labels[part]}正文`, `loaded ${labels[part].toLowerCase()} body`))}>{t('复制已加载正文', 'Copy loaded body')}</button>}
          </div>
          {block.source && <p className="cx-data-note">{t('当前内容块的原始文本。关联的原始记录请在 Raw 中查看。', 'Original text of this body block. Associated raw records are in Raw.')}</p>}
          {metadata?.auxiliary?.length ? <dl className="cx-reader-aux">{metadata.auxiliary.map((item, i) => <div key={i}><dt>{item.name}</dt><dd>{item.value}</dd></div>)}</dl> : null}
          {!block.source && value && spotlight(part, value)}
          {value ? renderBody(part, value, block) : <p className="cx-data-note">{loading ? t('正在读取正文…', 'Loading body…') : t('当前没有可读取正文。', 'No readable body is available.')}</p>}
        </>}
        {sourceTruncated && <p className="cx-data-note">{t('继续加载只能读取已保存字节，无法恢复来源缺失部分。', 'Loading more can only retrieve saved bytes; missing source content cannot be recovered.')}</p>}
        {value?.nextCursor && <button className="cx-button cx-more-content" disabled={loading} onClick={() => onMore(part)}>{loading ? t('正在读取…', 'Loading…') : t(`继续读取${labels[part]}`, `Load more ${labels[part].toLowerCase()}`)}</button>}
        {value?.nextCursor && value.readableBytes != null && value.readableBytes <= 8_000_000 && <button className="cx-button cx-more-content" disabled={loading} onClick={() => onMore(part, true)}>{t('读齐此块已保存正文', 'Load all saved text in this block')}</button>}
      </section>;
    })}
  </div>;
}
