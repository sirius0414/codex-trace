import { useEffect, useMemo, useRef } from 'react';
import type { TimeRange } from '../../../contracts/types';
import type { TraceUIProps } from '../ui-types';
import { usePreferences } from '../preferences';
import Icon from './icons';
import { scopedEvents, scopedModelRequests } from './model';
import { activeAgentId, agentPagePatch } from './agent-navigation';
import { buildActiveTimeline } from './active-timeline';
import TurnWaterfall from './TurnWaterfall';

/** 各 Agent 的概览入口；顺序图与历史记录的原始时间保持独立。 */
export default function Overview(props: TraceUIProps & { selectionRequest?: number; highlightedEvents: ReadonlySet<string> | null; onRangeCommit: (range: TimeRange) => void }) {
  const { snapshot, view, onView } = props;
  const { t } = usePreferences();
  const events = useMemo(() => snapshot ? scopedEvents(snapshot, view) : [], [snapshot, view.agentScope]);
  const timeline = useMemo(() => view.overviewMode === 'turns' ? buildActiveTimeline(snapshot) : null, [snapshot?.events, snapshot?.turns, view.overviewMode]);
  const agents = snapshot ? [{ groupId: snapshot.rootGroupId, name: t('主 Agent', 'Main Agent') }, ...snapshot.groups.filter(g => g.groupId !== snapshot.rootGroupId)] : [];
  const activeAgent = snapshot ? activeAgentId(snapshot, view) : null;
  const tabs = useRef<HTMLDivElement>(null);
  function openAgent(groupId: string) {
    if (snapshot && groupId !== activeAgent) onView(agentPagePatch(snapshot, view, groupId));
  }
  function agentKey(event: React.KeyboardEvent<HTMLButtonElement>, index: number) {
    if (!['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) return;
    event.preventDefault();
    const next = event.key === 'Home' ? 0 : event.key === 'End' ? agents.length - 1 : (index + (event.key === 'ArrowRight' ? 1 : agents.length - 1)) % agents.length;
    openAgent(agents[next].groupId);
    tabs.current?.querySelectorAll<HTMLButtonElement>('[role="tab"]')[next]?.focus();
  }
  useEffect(() => { tabs.current?.querySelector<HTMLElement>('[aria-selected="true"]')?.scrollIntoView({ block: 'nearest', inline: 'nearest' }); }, [activeAgent]);
  const turnItems = useMemo(() => {
    const map = new Map<string, typeof events>();
    for (const e of events) if (e.turnId) { const key = JSON.stringify([e.threadId, e.turnId]); const list = map.get(key) ?? []; list.push(e); map.set(key, list); }
    return map;
  }, [events]);
  const turns = snapshot?.turns.filter(turn => turnItems.has(JSON.stringify([turn.threadId, turn.turnId]))) ?? [];
  const requests = snapshot ? scopedModelRequests(snapshot, view) : new Set<string>();
  return <section className="cx-overview" aria-label={t('执行概览', 'Execution overview')}>
    <div className="cx-agent-bar"><span className="cx-agent-label"><Icon name="branch" /><small>{agents.length || '—'}</small></span>
      <div ref={tabs} className="cx-agent-buttons" role="tablist" aria-label={t('Agent 页面', 'Agent pages')}>
        {agents.map((agent, i) => <button key={agent.groupId} id={`cx-agent-tab-${agent.groupId}`} type="button" role="tab" className="cx-agent-button" aria-selected={activeAgent === agent.groupId} aria-controls="cx-agent-page" tabIndex={activeAgent === agent.groupId ? 0 : -1} title={agent.name} onClick={() => openAgent(agent.groupId)} onKeyDown={e => agentKey(e, i)}><Icon name={agent.groupId === snapshot?.rootGroupId ? 'model' : 'branch'} /><span>{agent.name}</span></button>)}
        {!snapshot && <span className="cx-muted">{t('选择任务后显示 Agent', 'Select a task to see agents')}</span>}
      </div>
    </div>
    <div className="cx-overview-toolbar"><div className="cx-modes" aria-label={t('概览方式', 'Overview mode')}>{([['duration', t('顺序', 'Sequence')], ['turns', t('轮次', 'Turns')], ['calls', t('调用', 'Calls')]] as const).map(([mode, label]) => <button key={mode} aria-pressed={view.overviewMode === mode} onClick={() => onView({ overviewMode: mode })}>{label}</button>)}</div></div>
    {view.overviewMode === 'duration' ? <TurnWaterfall {...props} /> : view.overviewMode === 'turns' ? <div className="cx-turns" aria-label={t('已加载轮次', 'Loaded turns')}>{turns.length ? turns.map(turn => {
      const key = JSON.stringify([turn.threadId, turn.turnId]), items = turnItems.get(key)!;
      const spans = timeline?.turnSpans.get(key) ?? [];
      const start = spans.length ? Math.min(Number.MAX_SAFE_INTEGER - 1, Math.round(spans[0].start)) : null;
      const end = start == null ? null : Math.min(Number.MAX_SAFE_INTEGER, spans.reduce((last, s) => Math.max(last, Math.round(s.end)), start + 1));
      return <button key={key} disabled={start == null || end == null} title={t('按已记录原始时间定位列表', 'Reveal list records using their original recorded times')} onClick={() => { if (start != null && end != null) { const range = { startMs: start, endMs: end }; onView({ follow: 'paused', turnRange: range }); props.onRangeCommit(range); } }}>Turn {turn.order + 1}<span>{t(`${items.length} 条`, `${items.length} records`)}</span></button>;
    }) : <span className="cx-muted">{t('尚无可展示轮次', 'No turns to display yet')}</span>}</div> : <div className="cx-calls"><div>{t('工具调用', 'Tool calls')} <b>{new Set(events.filter(e => e.kind === 'toolCall').map(e => e.callKey ?? e.eventId)).size}</b></div><div>{t('模型请求', 'Model requests')} <b>{requests.size || t('未记录', 'Not recorded')}</b></div><span>{t('当前 Agent 范围 · 已加载记录', 'Current agent scope · loaded records')}</span></div>}
  </section>;
}
