import { useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react';
import type { EventKind, TimeRange } from '../../../contracts/types';
import type { TraceUIProps } from '../ui-types';
import { buildInfo, versionLabel } from '../build-info';
import { usePreferences } from '../preferences';
import { uiStyles } from '../ui-preferences';
import EventList from './EventList';
import Inspector, { UsageFacts } from './Inspector';
import Overview from './Overview';
import Icon from './icons';
import { rangeReveal } from './expansion';
import { activeAgentId, agentPagePatch } from './agent-navigation';
import { clockTime, eventRangeIds, localizedKindLabels, latestUsage, metric, localizedStatusLabels, visibleEvents } from './model';

export default function TraceUI(props: TraceUIProps) {
  const { snapshot, onView, trackingPaused, onTrackingPause } = props;
  const activeAgent = snapshot ? activeAgentId(snapshot, props.view) : null;
  const singlePage = props.view.agentScope.kind === 'selected' && props.view.agentScope.groupIds.length === 1 && props.view.agentScope.groupIds[0] === activeAgent;
  // 旧的多选偏好在首次绘制即收敛到一个页面，避免短暂混入其他 Agent 的记录。
  const view = useMemo(() => !snapshot || singlePage ? props.view : { ...props.view,
    agentScope: { kind: 'selected' as const, groupIds: [activeAgent!] },
    expandedGroupIds: [...new Set([...props.view.expandedGroupIds, activeAgent!])],
  }, [props.view, snapshot, singlePage, activeAgent]);
  useEffect(() => {
    if (snapshot && !singlePage) onView({ agentScope: view.agentScope, expandedGroupIds: view.expandedGroupIds });
  }, [snapshot?.binding, singlePage, activeAgent, onView]);
  function navigateAgent(groupId: string, eventId?: string) {
    if (!snapshot) return;
    const patch = agentPagePatch(snapshot, view, groupId, eventId);
    if (!patch.agentScope) return;
    // 交互跳转应直接显示目标记录；普通标签切页则保留筛选条件。
    onView({ ...patch, filters: { text: '', kinds: [], toolName: null, failedOnly: false, timeRange: null } });
    if (eventId && !snapshot.events.some(event => event.eventId === eventId)) props.onSelect(eventId);
    if (!eventId) requestAnimationFrame(() => root.current?.querySelector<HTMLElement>(`#${CSS.escape(`cx-agent-tab-${groupId}`)}`)?.focus({ preventScroll: true }));
  }
  function selectEvent(eventId: string | null) {
    const event = snapshot?.events.find(item => item.eventId === eventId);
    if (event && event.groupId !== activeAgent) navigateAgent(event.groupId, event.eventId);
    else if (eventId && !event && snapshot) {
      const parent = snapshot.groups.find(group => group.creationEventId === eventId)?.parentGroupId;
      if (parent) navigateAgent(parent, eventId);
      else props.onSelect(eventId);
    }
    else props.onSelect(eventId);
  }
  const pageProps = { ...props, view, onSelect: selectEvent, onAgentNavigate: navigateAgent };
  const { language, uiStyle, showSpanSummary, showTimelineCursors, setLanguage, setUiStyle, setShowSpanSummary, setShowTimelineCursors, t } = usePreferences();
  const kindLabels = localizedKindLabels(language), statusLabels = localizedStatusLabels(language);
  const time = (value: number | null | undefined) => clockTime(value, language);
  const versionDescription = [`Codex Display ${versionLabel}`, buildInfo.sourceHash && `${t('源码指纹', 'Source fingerprint')}: ${buildInfo.sourceHash}`, buildInfo.builtAt && `${t('构建时间', 'Built at')}: ${buildInfo.builtAt}`].filter(Boolean).join('\n');
  const styleLabels = { classic: t('经典', 'Classic'), soft: t('柔和', 'Soft'), terminal: t('终端', 'Terminal') };
  const styleNotes = { classic: t('简洁、清晰的默认界面', 'Clean and familiar'), soft: t('柔和配色与圆角控件', 'Soft colors and rounded controls'), terminal: t('等宽字体与利落边框', 'Monospace type and crisp borders') };
  const [popover, setPopover] = useState<'usage' | 'reading' | 'tasks' | 'settings' | null>(null);
  const [taskQuery, setTaskQuery] = useState('');
  const [rowCount, setRowCount] = useState(0);
  const [narrow, setNarrow] = useState(() => window.matchMedia('(max-width:650px)').matches);
  const root = useRef<HTMLElement>(null);
  const taskInput = useRef<HTMLInputElement>(null);
  const previousThread = useRef(snapshot?.binding.threadId);
  const awaitingPage = useRef<string | null>(null);
  useEffect(() => {
    awaitingPage.current = snapshot && !snapshot.events.some(event => event.groupId === activeAgent) ? activeAgent : null;
  }, [snapshot?.binding.threadId, activeAgent]);
  useEffect(() => {
    if (!snapshot || !activeAgent || awaitingPage.current !== activeAgent) return;
    const loaded = snapshot.events.filter(event => event.groupId === activeAgent);
    if (!loaded.length) return;
    // 首次补读的轮次需要展开；此后不覆盖用户在该页手动收起的阅读状态。
    awaitingPage.current = null;
    onView({ expandedTurnIds: [...new Set([...view.expandedTurnIds, ...loaded.flatMap(event => event.turnId ? [event.turnId] : [])])],
      expandedGroupIds: [...new Set([...view.expandedGroupIds, activeAgent])] });
  }, [snapshot?.events, activeAgent, onView]);
  // 关闭双游标时暂停高亮，保留已保存选区供再次开启；预览不触发列表定位。
  const rangeEvents = useMemo(() => snapshot && showTimelineCursors ? eventRangeIds(snapshot, view.turnRange) : null, [snapshot, view.turnRange, showTimelineCursors]);
  const visible = useMemo(() => snapshot ? visibleEvents(snapshot, view, props.matchedIds) : [], [snapshot, view.filters, view.agentScope, props.matchedIds]);
  const highlightedEvents = useMemo(() => rangeEvents ? new Set(visible.filter(event => rangeEvents.has(event.eventId)).map(event => event.eventId)) : null, [visible, rangeEvents]);
  function revealRange(range: TimeRange) {
    if (!snapshot) return;
    const patch = rangeReveal(snapshot, view, visible, range);
    if (patch) onView(patch);
  }
  const usage = latestUsage(snapshot?.mainUsage ?? []);
  // 结束状态只取来源证据；读取中断、没有新增和暂停跟随都不能推断任务已结束。
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
    // 窄屏详情覆盖列表时，底层列表退出键盘与读屏导航；关闭后恢复原有属性。
    const wasInert = ledger.inert, wasHidden = ledger.getAttribute('aria-hidden');
    ledger.inert = true;
    ledger.setAttribute('aria-hidden', 'true');
    return () => { ledger.inert = wasInert; if (wasHidden == null) ledger.removeAttribute('aria-hidden'); else ledger.setAttribute('aria-hidden', wasHidden); };
  }, [narrow, view.selectedEventId]);
  useEffect(() => {
    if (popover === 'tasks') taskInput.current?.focus();
    if (popover === 'settings') root.current?.querySelector<HTMLInputElement>('.cx-settings input')?.focus();
  }, [popover]);
  useEffect(() => {
    if (previousThread.current !== snapshot?.binding.threadId) { setPopover(null); previousThread.current = snapshot?.binding.threadId; }
  }, [snapshot?.binding.threadId]);
  useEffect(() => {
    const key = (event: KeyboardEvent) => {
      if (event.key !== 'Escape') return;
      // 先关闭最上层弹层，再关闭详情；关闭后把焦点交回触发控件或原记录。
      if (popover) { setPopover(null); root.current?.querySelector<HTMLButtonElement>(popover === 'usage' ? '.cx-usage-trigger' : popover === 'reading' ? '.cx-read-status' : popover === 'settings' ? '.cx-settings-trigger' : '.cx-task')?.focus(); }
      else if (view.selectedEventId) { props.onSelect(null); requestAnimationFrame(() => {
        const selected = root.current?.querySelector<HTMLButtonElement>(`.cx-scroll [data-event-id="${CSS.escape(view.selectedEventId!)}"]`);
        (selected ?? root.current?.querySelector<HTMLElement>('.cx-scroll'))?.focus({ preventScroll: true });
      }); }
    };
    document.addEventListener('keydown', key); return () => document.removeEventListener('keydown', key);
  }, [popover, view.selectedEventId, props.onSelect]);
  useEffect(() => {
    const close = (event: PointerEvent) => {
      if (popover && !(event.target as Element).closest('.cx-popover,.cx-task-picker,.cx-usage-trigger,.cx-read-status,.cx-task,.cx-settings-trigger')) setPopover(null);
    };
    document.addEventListener('pointerdown', close); return () => document.removeEventListener('pointerdown', close);
  }, [popover]);
  const sourceLabel = { user: t('用户聊天', 'User chat'), subtask: t('子任务', 'Subtask'), unknown: t('未知来源', 'Unknown source'), internal: t('内部来源', 'Internal source') };
  const tools = [...new Set(snapshot?.events.flatMap(event => event.toolName ? [event.toolName] : []) ?? [])].sort();
  const typeValue = view.filters.toolName ? `tool:${view.filters.toolName}` : view.filters.kinds[0] ?? 'all';
  const readText = props.error ? t('读取中断 · 保留已读内容', 'Read interrupted · Content retained') : snapshot?.freshness.state === 'stale' || snapshot?.freshness.state === 'unavailable' ? t('读取暂不可用 · 保留已读内容', 'Read unavailable · Content retained') : snapshot?.groups.some(group => group.readIssue) ? t('子任务读取不完整 · 查看范围', 'Incomplete subtask reads · View scope') : t(`读取 ${time(snapshot?.freshness.lastSuccessfulReadAt)} · 进展 ${time(snapshot?.freshness.lastProgressAt)}`, `Read ${time(snapshot?.freshness.lastSuccessfulReadAt)} · Progress ${time(snapshot?.freshness.lastProgressAt)}`);
  return <main ref={root} className="cx-panel" data-ui-style={uiStyle} lang={language === 'zh' ? 'zh-CN' : 'en'} aria-label={t('执行轨迹', 'Execution trace')}>
    <header className="cx-toolbar">
      <div className="cx-follow-controls"><button className="cx-icon" disabled={!snapshot || trackingPaused} aria-label={t('暂停跟踪', 'Pause tracking')} title={t('冻结瀑布图、条目数量和 Agent 状态；Agent 继续执行', 'Freeze the waterfall, record counts and Agent status; Agent execution continues')} onClick={() => onTrackingPause(true)}><Icon name="pause" /></button><button className="cx-icon" disabled={!snapshot || !trackingPaused && view.follow === 'following'} aria-label={t('继续跟踪', 'Resume tracking')} title={t('立即读取最新进展并持续刷新 Agent 状态；保留筛选和详情', 'Read the latest progress now and keep refreshing Agent status. Filters and details are retained')} onClick={() => onTrackingPause(false)}><Icon name="play" /></button></div>
      <span className={`cx-follow-label ${trackingPaused || done ? 'paused' : ''}`}>{trackingPaused ? t('已暂停', 'Paused') : done ? t('已结束', 'Ended') : t('跟踪中', 'Tracking')}</span><span className="cx-divider" /><h1>{t('执行轨迹', 'Execution trace')}</h1>
      <button className="cx-task" aria-expanded={popover === 'tasks'} aria-haspopup="dialog" title={snapshot?.title ?? t('选择任务', 'Select task')} onClick={() => { setPopover(value => value === 'tasks' ? null : 'tasks'); props.onTaskSearch(taskQuery); }}><span>{snapshot?.title || t('选择任务', 'Select task')}</span><Icon name="down" /></button>
      <span className={`cx-task-status ${snapshot?.status.value ?? 'unknown'}`} title={snapshot?.status.explanation}><span className="cx-status-dot" />{snapshot ? statusLabels[snapshot.status.value] : t('未选择', 'No task')}</span>
      <button className="cx-button cx-download" disabled={!snapshot || props.exporting} aria-label={t('导出记录', 'Export records')} title={t('导出所选任务可读取的全部原始记录，不受搜索、筛选、分页和折叠影响', 'Export all readable raw records for the selected task, regardless of search, filters, pages, or collapsed groups')} onClick={props.onExport}><Icon name="download" /><span>{props.exporting ? t('正在导出…', 'Exporting…') : t('导出记录', 'Export records')}</span></button>
      <button className="cx-button cx-settings-trigger" aria-label={t('设置', 'Settings')} title={t('设置', 'Settings')} aria-expanded={popover === 'settings'} aria-haspopup="dialog" aria-controls="cx-settings" onClick={() => setPopover(value => value === 'settings' ? null : 'settings')}><Icon name="settings" /><span>{t('设置', 'Settings')}</span></button>
    </header>
    <Overview key={snapshot?.binding.threadId ?? 'empty'} {...pageProps} highlightedEvents={highlightedEvents} onRangeCommit={revealRange} />
    <div className="cx-filterbar"><label className="cx-search"><Icon name="search" /><input aria-label={t('搜索已加载记录', 'Search loaded records')} value={view.filters.text} placeholder={t('搜索已加载记录…', 'Search loaded records…')} onChange={event => filter({ text: event.target.value })} /></label>
      <select className="cx-type" aria-label={t('事件类型或工具', 'Event type or tool')} value={typeValue} onChange={event => filter(event.target.value.startsWith('tool:') ? { kinds: [], toolName: event.target.value.slice(5) } : { kinds: event.target.value === 'all' ? [] : [event.target.value as EventKind], toolName: null })}>
        <option value="all">{t('全部类型', 'All types')}</option>{Object.entries(kindLabels).map(([kind, label]) => <option key={kind} value={kind}>{label}</option>)}{tools.length > 0 && <optgroup label={t('工具名称', 'Tool name')}>{tools.map(tool => <option key={tool} value={`tool:${tool}`}>{tool}</option>)}</optgroup>}
      </select><button className="cx-button cx-error-filter" aria-pressed={view.filters.failedOnly} onClick={() => filter({ failedOnly: !view.filters.failedOnly })}><Icon name="alert" />{t('仅失败', 'Failed only')}</button>
    </div>
    <div id="cx-agent-page" role="tabpanel" aria-labelledby={activeAgent ? `cx-agent-tab-${activeAgent}` : undefined} className={`cx-work ${view.selectedEventId ? 'has-detail' : ''}`}>
      <EventList key={`${snapshot?.binding.threadId ?? 'empty'}:${activeAgent}`} {...pageProps} visible={visible} highlightedEvents={highlightedEvents} onCount={setRowCount} />
      {view.selectedEventId && <Inspector {...pageProps} overlay={narrow} />}
    </div>
    <aside className="cx-usage-bar" aria-label={t('主任务 token 用量，不受 Agent 筛选影响', 'Main task token usage, independent of Agent filters')}>
      <span className="cx-usage-label"><Icon name="usage" />{t('主任务', 'Main task')} <small>tokens</small></span>
      <dl className="cx-usage-metrics"><div><dt>{t('输入', 'Input')}</dt><dd title={usage?.input.availability !== 'recorded' ? t('未记录', 'Not recorded') : undefined} aria-label={usage?.input.availability !== 'recorded' ? t('未记录', 'Not recorded') : undefined}>{metric(usage?.input, t('未记录', '—'), language)}</dd></div><div><dt>{t('输出', 'Output')}</dt><dd title={usage?.output.availability !== 'recorded' ? t('未记录', 'Not recorded') : undefined} aria-label={usage?.output.availability !== 'recorded' ? t('未记录', 'Not recorded') : undefined}>{metric(usage?.output, t('未记录', '—'), language)}</dd></div><div><dt>{t('缓存', 'Cached')}</dt><dd title={usage?.cachedInput.availability !== 'recorded' ? t('未记录', 'Not recorded') : undefined} aria-label={usage?.cachedInput.availability !== 'recorded' ? t('未记录', 'Not recorded') : undefined}>{metric(usage?.cachedInput, t('未记录', '—'), language)}</dd></div></dl>
      <span className="cx-usage-note">{usage?.cacheSemantics === 'includedInInput' ? t('缓存已含在输入中', 'Cache included in input') : usage?.cacheSemantics === 'separate' ? t('缓存单独记录', 'Cache recorded separately') : t('缓存归属未记录', 'Cache semantics not recorded')}</span>
      <button className="cx-icon cx-usage-trigger" aria-expanded={popover === 'usage'} aria-label={t('查看用量范围与说明', 'View usage scope and notes')} title={t('查看用量范围与说明', 'View usage scope and notes')} onClick={() => setPopover(value => value === 'usage' ? null : 'usage')}><Icon name="info" /></button>
    </aside>
    <footer className="cx-footer"><span className="cx-count" title={t(`当前显示 ${rowCount} 行；已加载 ${snapshot?.coverage.loadedEventCount ?? 0} 条已识别记录，包含折叠子任务内容`, `${rowCount} rows shown; ${snapshot?.coverage.loadedEventCount ?? 0} recognized records loaded, including collapsed subtasks`)}>{t(`${rowCount} 行 / ${snapshot?.coverage.loadedEventCount ?? 0} 条已读`, `${rowCount} rows / ${snapshot?.coverage.loadedEventCount ?? 0} read`)}</span>
      <button className={`cx-read-status ${props.error || snapshot?.freshness.state === 'stale' || snapshot?.groups.some(group => group.readIssue) ? 'problem' : ''}`} aria-label={t('查看记录读取状态', 'View record reading status')} onClick={() => setPopover(value => value === 'reading' ? null : 'reading')}><Icon name={props.error ? 'alert' : 'database'} /><span>{props.searching && !props.error ? t('正在检索已加载正文…', 'Searching loaded content…') : readText}</span></button>
      {props.loading && <span className="cx-loading">{t('读取中…', 'Reading…')}</span>}
      {snapshot?.coverage.searchTextCoverage === 'partialLoadedContent' && <span className="cx-partial-search" title={snapshot.coverage.explanation}>{t('搜索范围不完整', 'Search scope is partial')}</span>}
      <span className="cx-version" title={versionDescription} aria-label={versionDescription}>{versionLabel}</span>
    </footer>
    {popover === 'tasks' && <section className="cx-task-picker" role="dialog" aria-label={t('选择任务', 'Select task')}><div className="cx-picker-search"><label className="cx-search"><Icon name="search" /><input ref={taskInput} value={taskQuery} placeholder={t('搜索任务…', 'Search tasks…')} aria-label={t('搜索任务', 'Search tasks')} onChange={event => { setTaskQuery(event.target.value); props.onTaskSearch(event.target.value); }} /></label><button className="cx-icon" aria-label={t('关闭任务选择', 'Close task picker')} onClick={() => setPopover(null)}><Icon name="close" /></button></div>
      <div className="cx-task-scopes" aria-label={t('任务来源范围', 'Task source scope')}><button aria-pressed={props.threadScope === 'user'} onClick={() => props.onThreadScope('user')}>{t('聊天', 'Chats')}</button><button aria-pressed={props.threadScope === 'all'} onClick={() => props.onThreadScope('all')}>{t('全部来源', 'All sources')}</button></div>
      <div className="cx-task-options" role="listbox" aria-label={t('任务列表', 'Task list')}>{props.threads.map(thread => <button key={thread.threadId} role="option" aria-selected={thread.threadId === snapshot?.binding.threadId} title={thread.threadId} onClick={() => { props.onTask(thread.threadId); setPopover(null); }}><span>{thread.title || thread.threadId}</span>{props.threadScope === 'all' && <small>{sourceLabel[thread.source]}</small>}</button>)}{!props.threads.length && <p className="cx-note">{t('当前范围没有匹配任务。', 'No matching tasks in this scope.')}</p>}{props.threadsMore && <button className="cx-load" onClick={props.onMoreThreads}>{t('加载更多任务', 'Load more tasks')}</button>}</div>
    </section>}
    {(popover === 'usage' || popover === 'reading') && <section className={`cx-popover ${popover === 'usage' ? 'cx-usage-popover' : ''}`} role="dialog" aria-label={popover === 'usage' ? t('主任务 token 用量', 'Main task token usage') : t('记录读取状态', 'Record reading status')}><div className="cx-popover-head">{popover === 'usage' ? t('主任务 · token 用量', 'Main task · Token usage') : t('记录读取状态', 'Record reading status')}<button className="cx-icon" aria-label={popover === 'usage' ? t('关闭用量', 'Close usage') : t('关闭读取状态', 'Close reading status')} onClick={() => setPopover(null)}><Icon name="close" /></button></div>
      {popover === 'usage' ? <UsageFacts values={snapshot?.mainUsage ?? []} label={t('主任务；不受列表筛选影响', 'Main task; independent of list filters')} /> : <><dl className="cx-facts"><dt>{t('最近成功读取', 'Last successful read')}</dt><dd>{time(snapshot?.freshness.lastSuccessfulReadAt)}</dd><dt>{t('最近已知进展', 'Last known progress')}</dt><dd>{time(snapshot?.freshness.lastProgressAt)}</dd></dl><p>{props.error || snapshot?.freshness.limitation || t('读取成功本身不表示任务有新的进展。', 'A successful read alone does not indicate new task progress.')}</p><p>{snapshot?.coverage.explanation}</p>{snapshot?.freshness.sources.filter(source => source.reason).map(source => <p key={source.sourceId}>{source.sourceId}：{source.reason}</p>)}<p>{t('暂停跟踪会冻结瀑布图、条目数量和 Agent 状态；继续后读取最新进展。部分工具输出结束后保存，任务状态由已有证据独立判断。', 'Pausing tracking freezes the waterfall, record counts and Agent status. Resume to read the latest progress. Some tool output is saved on completion; task status is determined separately from recorded evidence.')}</p>{props.error && <button className="cx-button" onClick={props.onRetry}>{t('重试读取', 'Retry reading')}</button>}</>}
    </section>}
    {popover === 'settings' && <section id="cx-settings" className="cx-popover cx-settings" role="dialog" aria-labelledby="cx-settings-title">
      <div className="cx-popover-head"><span id="cx-settings-title">{t('设置', 'Settings')}</span><button className="cx-icon" aria-label={t('关闭设置', 'Close settings')} onClick={() => { setPopover(null); root.current?.querySelector<HTMLButtonElement>('.cx-settings-trigger')?.focus(); }}><Icon name="close" /></button></div>
      <fieldset className="cx-timeline-settings"><legend>{t('时间概览', 'Timeline')}</legend>
        <label className="cx-setting-toggle"><span><strong>{t('折叠空档', 'Fold gaps')}</strong><small>{t('压缩无记录空档的显示宽度', 'Shorten gaps without recorded intervals')}</small></span><input type="checkbox" aria-label={t('折叠无记录空档', 'Fold unrecorded gaps')} checked={(view.timelineSpacing ?? 'compact') === 'compact'} onChange={event => onView({ timelineSpacing: event.target.checked ? 'compact' : 'runtime' })} /></label>
        <label className="cx-setting-toggle"><span><strong>{t('显示双游标', 'Show dual cursors')}</strong><small>{t('瀑布图时间范围选择与高亮', 'Select and highlight a time range in the waterfall')}</small></span><input type="checkbox" aria-label={t('显示双游标', 'Show dual cursors')} checked={showTimelineCursors} onChange={event => setShowTimelineCursors(event.target.checked)} /></label>
        <label className="cx-setting-toggle"><span><strong>{t('显示 Span 摘要', 'Show Span summary')}</strong><small>{t('运行跨度、覆盖条、滚动条与选区说明', 'Runtime, coverage, scrollbar and selection notes')}</small></span><input type="checkbox" aria-label={t('显示 Span 摘要', 'Show Span summary')} checked={showSpanSummary} onChange={event => setShowSpanSummary(event.target.checked)} /></label>
      </fieldset>
      <label className="cx-language-field"><span>{t('界面语言', 'Interface language')}</span><select value={language} onChange={event => setLanguage(event.target.value as 'zh' | 'en')}><option value="zh">简体中文</option><option value="en">English</option></select></label>
      <fieldset className="cx-style-options"><legend>{t('UI 模板', 'UI template')}</legend>{uiStyles.map(style => <label key={style} className={`cx-style-option ${uiStyle === style ? 'selected' : ''}`}><input type="radio" name="cx-ui-style" value={style} checked={uiStyle === style} onChange={() => setUiStyle(style)} /><span className={`cx-style-preview ${style}`} aria-hidden="true"><i /><i /><i /></span><span><strong>{styleLabels[style]}</strong><small>{styleNotes[style]}</small></span></label>)}</fieldset>
      <p className="cx-preference-note">{t('更改立即生效，刷新后保留。', 'Changes apply immediately and are remembered on reload.')}</p>
    </section>}
    {props.exportMessage && <div className="cx-export-message" role="status" tabIndex={0} aria-label={t('导出结果', 'Export result')}>{props.exportMessage}</div>}
  </main>;
}
