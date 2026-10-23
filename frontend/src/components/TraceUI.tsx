import { useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react';
import type { EventKind } from '../../../contracts/types';
import type { TraceUIProps } from '../ui-types';
import { versionDescription, versionLabel } from '../build-info';
import EventList from './EventList';
import Inspector, { UsageFacts } from './Inspector';
import Overview from './Overview';
import Icon from './icons';
import { clockTime, kindLabels, latestUsage, metric, statusLabels, visibleEvents } from './model';

export default function TraceUI(props: TraceUIProps) {
  const { snapshot, view, onView } = props;
  const [popover, setPopover] = useState<'usage' | 'reading' | 'tasks' | null>(null);
  const [taskQuery, setTaskQuery] = useState('');
  const [rowCount, setRowCount] = useState(0);
  const [narrow, setNarrow] = useState(() => window.matchMedia('(max-width:650px)').matches);
  const root = useRef<HTMLElement>(null);
  const taskInput = useRef<HTMLInputElement>(null);
  const previousThread = useRef(snapshot?.binding.threadId);
  const visible = useMemo(() => snapshot ? visibleEvents(snapshot, view, props.matchedIds) : [], [snapshot, view.filters, view.agentScope, props.matchedIds]);
  const usage = latestUsage(snapshot?.mainUsage ?? []);
  const done = snapshot?.status.value === 'completed' || snapshot?.status.value === 'stopped' || snapshot?.status.value === 'failed';
  const filter = (patch: Partial<typeof view.filters>) => onView({ follow: 'paused', filters: { ...view.filters, ...patch } });
  useEffect(() => {
    const media = window.matchMedia('(max-width:650px)');
    const change = () => setNarrow(media.matches);
    media.addEventListener('change', change); change();
    return () => media.removeEventListener('change', change);
  }, []);
  useLayoutEffect(() => {
    const ledger = root.current?.querySelector<HTMLElement>('.cx-ledger');
    if (!ledger || !narrow || !view.selectedEventId) return;
    const wasInert = ledger.inert, wasHidden = ledger.getAttribute('aria-hidden');
    ledger.inert = true;
    ledger.setAttribute('aria-hidden', 'true');
    return () => { ledger.inert = wasInert; if (wasHidden == null) ledger.removeAttribute('aria-hidden'); else ledger.setAttribute('aria-hidden', wasHidden); };
  }, [narrow, view.selectedEventId]);
  useEffect(() => {
    if (popover === 'tasks') taskInput.current?.focus();
  }, [popover]);
  useEffect(() => {
    if (previousThread.current !== snapshot?.binding.threadId) { setPopover(null); previousThread.current = snapshot?.binding.threadId; }
  }, [snapshot?.binding.threadId]);
  useEffect(() => {
    const key = (event: KeyboardEvent) => {
      if (event.key !== 'Escape') return;
      if (popover) { setPopover(null); root.current?.querySelector<HTMLButtonElement>(popover === 'usage' ? '.cx-usage-trigger' : popover === 'reading' ? '.cx-read-status' : '.cx-task')?.focus(); }
      else if (view.selectedEventId) { props.onSelect(null); requestAnimationFrame(() => {
        const selected = root.current?.querySelector<HTMLButtonElement>(`.cx-scroll [data-event-id="${CSS.escape(view.selectedEventId!)}"]`);
        (selected ?? root.current?.querySelector<HTMLElement>('.cx-scroll'))?.focus({ preventScroll: true });
      }); }
    };
    document.addEventListener('keydown', key); return () => document.removeEventListener('keydown', key);
  }, [popover, view.selectedEventId, props.onSelect]);
  useEffect(() => {
    const close = (event: PointerEvent) => {
      if (popover && !(event.target as Element).closest('.cx-popover,.cx-task-picker,.cx-usage-trigger,.cx-read-status,.cx-task')) setPopover(null);
    };
    document.addEventListener('pointerdown', close); return () => document.removeEventListener('pointerdown', close);
  }, [popover]);
  const sourceLabel = { user: '用户聊天', subtask: '子任务', unknown: '未知来源', internal: '内部来源' };
  const tools = [...new Set(snapshot?.events.flatMap(event => event.toolName ? [event.toolName] : []) ?? [])].sort();
  const typeValue = view.filters.toolName ? `tool:${view.filters.toolName}` : view.filters.kinds[0] ?? 'all';
  const readText = props.error ? '读取中断 · 保留已读内容' : snapshot?.freshness.state === 'stale' || snapshot?.freshness.state === 'unavailable' ? '读取暂不可用 · 保留已读内容' : snapshot?.groups.some(group => group.readIssue) ? '子任务读取不完整 · 查看范围' : `读取 ${clockTime(snapshot?.freshness.lastSuccessfulReadAt)} · 进展 ${clockTime(snapshot?.freshness.lastProgressAt)}`;
  return <main ref={root} className="cx-panel" aria-label="执行轨迹">
    <header className="cx-toolbar">
      <div className="cx-follow-controls"><button className="cx-icon" disabled={!snapshot || done || view.follow !== 'following'} aria-label="暂停跟随" title="暂停自动滚动，继续读取；不暂停 Agent 执行" onClick={() => onView({ follow: 'paused' })}><Icon name="pause" /></button><button className="cx-icon" disabled={!snapshot || done || view.follow === 'following'} aria-label="恢复跟随" title="跟随已保存记录，部分工具输出结束后可用；保留筛选和详情" onClick={() => onView({ follow: 'following' })}><Icon name="play" /></button></div>
      <span className={`cx-follow-label ${view.follow === 'paused' || done ? 'paused' : ''}`}>{done ? '已结束' : view.follow === 'following' ? '跟随中' : '已暂停'}</span><span className="cx-divider" /><h1>执行轨迹</h1>
      <button className="cx-task" aria-expanded={popover === 'tasks'} aria-haspopup="dialog" title={snapshot?.title ?? '选择任务'} onClick={() => { setPopover(value => value === 'tasks' ? null : 'tasks'); props.onTaskSearch(taskQuery); }}><span>{snapshot?.title || '选择任务'}</span><Icon name="down" /></button>
      <span className={`cx-task-status ${snapshot?.status.value ?? 'unknown'}`} title={snapshot?.status.explanation}><span className="cx-status-dot" />{snapshot ? statusLabels[snapshot.status.value] : '未选择'}</span>
      <button className="cx-button cx-download" disabled={!snapshot || props.exporting} aria-label="导出记录" title="导出所选任务可读取的全部原始记录，不受搜索、筛选、分页和折叠影响" onClick={props.onExport}><Icon name="download" /><span>{props.exporting ? '正在导出…' : '导出记录'}</span></button>
    </header>
    <Overview key={snapshot?.binding.threadId ?? 'empty'} {...props} />
    <div className="cx-filterbar"><label className="cx-search"><Icon name="search" /><input aria-label="搜索已加载记录" value={view.filters.text} placeholder="搜索已加载记录…" onChange={event => filter({ text: event.target.value })} /></label>
      <select className="cx-type" aria-label="事件类型或工具" value={typeValue} onChange={event => filter(event.target.value.startsWith('tool:') ? { kinds: [], toolName: event.target.value.slice(5) } : { kinds: event.target.value === 'all' ? [] : [event.target.value as EventKind], toolName: null })}>
        <option value="all">全部类型</option>{Object.entries(kindLabels).map(([kind, label]) => <option key={kind} value={kind}>{label}</option>)}{tools.length > 0 && <optgroup label="工具名称">{tools.map(tool => <option key={tool} value={`tool:${tool}`}>{tool}</option>)}</optgroup>}
      </select><button className="cx-button cx-error-filter" aria-pressed={view.filters.failedOnly} onClick={() => filter({ failedOnly: !view.filters.failedOnly })}><Icon name="alert" />仅失败</button>
    </div>
    <div className={`cx-work ${view.selectedEventId ? 'has-detail' : ''}`}>
      <EventList {...props} visible={visible} onCount={setRowCount} />
      {view.selectedEventId && <Inspector {...props} overlay={narrow} />}
    </div>
    <aside className="cx-usage-bar" aria-label="主任务 token 用量，不受 Agent 筛选影响">
      <span className="cx-usage-label"><Icon name="usage" />主任务 <small>tokens</small></span>
      <dl className="cx-usage-metrics"><div><dt>输入</dt><dd>{metric(usage?.input, '未记录')}</dd></div><div><dt>输出</dt><dd>{metric(usage?.output, '未记录')}</dd></div><div><dt>缓存</dt><dd>{metric(usage?.cachedInput, '未记录')}</dd></div></dl>
      <span className="cx-usage-note">{usage?.cacheSemantics === 'includedInInput' ? '缓存已含在输入中' : usage?.cacheSemantics === 'separate' ? '缓存单独记录' : '缓存归属未记录'}</span>
      <button className="cx-icon cx-usage-trigger" aria-expanded={popover === 'usage'} aria-label="查看用量范围与说明" title="查看用量范围与说明" onClick={() => setPopover(value => value === 'usage' ? null : 'usage')}><Icon name="info" /></button>
    </aside>
    <footer className="cx-footer"><span className="cx-count" title={`当前显示 ${rowCount} 行；已加载 ${snapshot?.coverage.loadedEventCount ?? 0} 条已识别记录，包含折叠子任务内容`}>{rowCount} 行 / {snapshot?.coverage.loadedEventCount ?? 0} 条已读</span>
      <button className={`cx-read-status ${props.error || snapshot?.freshness.state === 'stale' || snapshot?.groups.some(group => group.readIssue) ? 'problem' : ''}`} aria-label="查看记录读取状态" onClick={() => setPopover(value => value === 'reading' ? null : 'reading')}><Icon name={props.error ? 'alert' : 'database'} /><span>{props.searching && !props.error ? '正在检索已加载正文…' : readText}</span></button>
      {props.loading && <span className="cx-loading">读取中…</span>}
      {snapshot?.coverage.searchTextCoverage === 'partialLoadedContent' && <span className="cx-partial-search" title={snapshot.coverage.explanation}>搜索范围不完整</span>}
      <span className="cx-version" title={versionDescription} aria-label={versionDescription}>{versionLabel}</span>
    </footer>
    {popover === 'tasks' && <section className="cx-task-picker" role="dialog" aria-label="选择任务"><div className="cx-picker-search"><label className="cx-search"><Icon name="search" /><input ref={taskInput} value={taskQuery} placeholder="搜索任务…" aria-label="搜索任务" onChange={event => { setTaskQuery(event.target.value); props.onTaskSearch(event.target.value); }} /></label><button className="cx-icon" aria-label="关闭任务选择" onClick={() => setPopover(null)}><Icon name="close" /></button></div>
      <div className="cx-task-scopes" aria-label="任务来源范围"><button aria-pressed={props.threadScope === 'user'} onClick={() => props.onThreadScope('user')}>聊天</button><button aria-pressed={props.threadScope === 'all'} onClick={() => props.onThreadScope('all')}>全部来源</button></div>
      <div className="cx-task-options" role="listbox" aria-label="任务列表">{props.threads.map(thread => <button key={thread.threadId} role="option" aria-selected={thread.threadId === snapshot?.binding.threadId} title={thread.threadId} onClick={() => { props.onTask(thread.threadId); setPopover(null); }}><span>{thread.title || thread.threadId}</span>{props.threadScope === 'all' && <small>{sourceLabel[thread.source]}</small>}</button>)}{!props.threads.length && <p className="cx-note">当前范围没有匹配任务。</p>}{props.threadsMore && <button className="cx-load" onClick={props.onMoreThreads}>加载更多任务</button>}</div>
    </section>}
    {(popover === 'usage' || popover === 'reading') && <section className={`cx-popover ${popover === 'usage' ? 'cx-usage-popover' : ''}`} role="dialog" aria-label={popover === 'usage' ? '主任务 token 用量' : '记录读取状态'}><div className="cx-popover-head">{popover === 'usage' ? '主任务 · token 用量' : '记录读取状态'}<button className="cx-icon" aria-label={popover === 'usage' ? '关闭用量' : '关闭读取状态'} onClick={() => setPopover(null)}><Icon name="close" /></button></div>
      {popover === 'usage' ? <UsageFacts values={snapshot?.mainUsage ?? []} label="主任务；不受列表筛选影响" /> : <><dl className="cx-facts"><dt>最近成功读取</dt><dd>{clockTime(snapshot?.freshness.lastSuccessfulReadAt)}</dd><dt>最近已知进展</dt><dd>{clockTime(snapshot?.freshness.lastProgressAt)}</dd></dl><p>{props.error || snapshot?.freshness.limitation || '读取成功本身不表示任务有新的进展。'}</p><p>{snapshot?.coverage.explanation}</p>{snapshot?.freshness.sources.filter(source => source.reason).map(source => <p key={source.sourceId}>{source.sourceId}：{source.reason}</p>)}<p>跟随只影响滚动。部分工具输出结束后保存，任务状态由已有证据独立判断。</p>{props.error && <button className="cx-button" onClick={props.onRetry}>重试读取</button>}</>}
    </section>}
    {props.exportMessage && <div className="cx-export-message" role="status" tabIndex={0} aria-label="导出结果">{props.exportMessage}</div>}
  </main>;
}
