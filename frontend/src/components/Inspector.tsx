import { Fragment, useLayoutEffect, useRef, useState } from 'react';
import type { ContentPart, TraceEvent, UsageSnapshot } from '../../../contracts/types';
import type { DetailPart, TraceUIProps } from '../ui-types';
import Icon from './icons';
import { eventIcon } from './EventList';
import { eventKindDescription } from './event-descriptions';
import { interactionTargets } from './agent-navigation';
import { clockTime, duration, localizedKindLabels, latestUsage, metric, relationshipEndpoints, localizedStatusLabels } from './model';
import { usePreferences } from '../preferences';
import { useInspectorResize } from './useInspectorResize';

export function UsageFacts({ values, label }: { values: UsageSnapshot[]; label: string }) {
  const { language, t } = usePreferences();
  const usage = latestUsage(values);
  return <><dl className="cx-facts"><dt>{t('输入', 'Input')}</dt><dd>{metric(usage?.input, t('未记录', 'Not recorded'), language)}</dd><dt>{t('输出', 'Output')}</dt><dd>{metric(usage?.output, t('未记录', 'Not recorded'), language)}</dd><dt>{t('缓存', 'Cached')}</dt><dd>{metric(usage?.cachedInput, t('未记录', 'Not recorded'), language)}</dd><dt>{t('保存时间', 'Recorded at')}</dt><dd>{clockTime(usage?.recordedAt, language)}</dd><dt>{t('范围', 'Scope')}</dt><dd>{label} · {usage?.coverage ?? t('尚未读取到用量记录', 'No usage record loaded')}</dd>{usage && <><dt>{t('归属', 'Attribution')}</dt><dd>{({ selfOnly: t('仅当前任务', 'Current task only'), includesChildren: t('包含子任务，不能重复相加', 'Includes subtasks; do not add again'), includesInherited: t('包含继承上下文，不能重复相加', 'Includes inherited context; do not add again'), unseparated: t('来源未区分归属', 'Attribution not separated by source') })[usage.attribution]}</dd></>}</dl>
    <p className="cx-data-note">{t('“—”表示未记录，不等于 0。重复读取同一累计快照不会再次相加。', '“—” means not recorded, not zero. Reading the same cumulative snapshot again does not add to the total.')}</p>
    <p className="cx-data-note">{usage?.cacheSemantics === 'includedInInput' ? t('缓存属于输入的一部分，不再与输入相加。', 'Cached tokens are included in input and are not added again.') : usage?.cacheSemantics === 'separate' ? t('来源将缓存单独记录。', 'The source records cached tokens separately.') : t('缓存与输入的关系未记录。', 'The relationship between cached tokens and input is not recorded.')}</p></>;
}
export default function Inspector(props: TraceUIProps & { overlay?: boolean; preferredWidth: number; onResize: (width: number) => void }) {
  const { snapshot, view } = props;
  const { language, t } = usePreferences();
  const kindLabels = localizedKindLabels(language);
  const statusLabels = localizedStatusLabels(language);
  const tabs = [['summary', t('概述', 'Summary')], ['preview', t('预览', 'Preview')], ['raw', t('原始内容', 'Raw content')], ['source', t('来源', 'Source')]] as const;
  const completeness = { notRecorded: t('尚未保存输出', 'Output not saved'), fragment: t('已记录片段', 'Saved fragment'), complete: t('完整结果', 'Complete result'), unknown: t('完整性未知', 'Completeness unknown') };
  const event = snapshot?.events.find(item => item.eventId === view.selectedEventId);
  const detail = props.detail?.eventId === view.selectedEventId ? props.detail : null;
  const body = useRef<HTMLDivElement>(null);
  const panel = useRef<HTMLElement>(null);
  const resize = useInspectorResize(panel, Boolean(props.overlay), props.preferredWidth, props.onResize);
  const closeButton = useRef<HTMLButtonElement>(null);
  const opener = useRef<Element | null>(document.activeElement);
  const [copied, setCopied] = useState<'' | 'success' | 'unavailable'>('');
  const createdGroup = snapshot?.groups.find(group => group.creationEventId === view.selectedEventId);
  const group = createdGroup ?? snapshot?.groups.find(item => item.groupId === event?.groupId);
  const relation = snapshot?.relationships.find(item => item.relationshipId === group?.relationshipId);
  const turn = snapshot?.turns.find(item => item.turnId === event?.turnId && item.threadId === event?.threadId);
  const relatedAgents = snapshot && event && props.onAgentNavigate ? interactionTargets(snapshot, event) : [];
  useLayoutEffect(() => {
    // 初次进入窄屏详情时移入焦点；切换记录不抢回焦点，让列表可以连续浏览。
    if (props.overlay && !panel.current?.contains(document.activeElement)) closeButton.current?.focus({ preventScroll: true });
  }, [props.overlay]);
  function close() {
    props.onSelect(null);
    requestAnimationFrame(() => {
      const selected = view.selectedEventId && document.querySelector<HTMLButtonElement>(`.cx-scroll [data-event-id="${CSS.escape(view.selectedEventId)}"]`);
      if (selected) selected.focus({ preventScroll: true });
      else if (opener.current instanceof HTMLElement && opener.current.isConnected) opener.current.focus({ preventScroll: true });
      else document.querySelector<HTMLElement>('.cx-scroll')?.focus({ preventScroll: true });
    });
  }
  async function copy(text: string) {
    try {
      if (!navigator.clipboard?.writeText) throw new Error('clipboard-unavailable');
      await navigator.clipboard.writeText(text); setCopied('success');
    } catch {
      const active = document.activeElement;
      const textarea = document.createElement('textarea');
      textarea.value = text; textarea.style.position = 'fixed'; textarea.style.opacity = '0';
      let success = false;
      try { document.body.appendChild(textarea); textarea.select(); success = document.execCommand('copy'); }
      catch { /* 宿主可能拒绝剪贴板访问，失败时不显示复制成功。 */ }
      finally { textarea.remove(); if (active instanceof HTMLElement && active.isConnected) active.focus({ preventScroll: true }); }
      setCopied(success ? 'success' : 'unavailable');
    }
    setTimeout(() => setCopied(''), 2000);
  }
  function content(part: ContentPart, label: string, value: DetailPart | undefined) {
    // 来源截断、已保存内容完整性与 UI 分页分别展示；有下一页并不表示来源内容完整。
    const state = value?.completeness ?? event?.content.find(ref => ref.part === part)?.completeness ?? 'notRecorded';
    const output = part === 'output' && label === t('输出', 'Output');
    return <section className="cx-content-part" key={part}>
      <div className="cx-detail-label">{label}{output && <span className="cx-output-kind">{completeness[state as keyof typeof completeness] ?? completeness.unknown}</span>}{value?.text && <button className="cx-icon" aria-label={t(`复制当前已加载的${label}`, `Copy loaded ${label.toLowerCase()}`)} title={t(`仅复制当前已加载的${label}；未加载部分不包含在内`, `Copy only loaded ${label.toLowerCase()}; unloaded content is excluded`)} onClick={() => copy(value.text)}><Icon name="copy" /></button>}</div>
      {value?.text ? <pre className={`cx-code ${part === 'error' ? 'error' : ''}`}>{value.text}</pre> : <p className="cx-data-note">{props.detailLoading ? t('正在读取内容…', 'Loading content…') : part === 'raw' ? t('没有可确认关联的原始内容。', 'No raw content with a confirmed association.') : part === 'output' ? t('当前没有可读取的输出；部分工具结果会在结束后保存。', 'No readable output is available yet. Some tool results are saved after completion.') : t('该内容未记录或暂不可读。', 'This content was not recorded or is temporarily unreadable.')}</p>}
      {value?.sourceTruncated && <p className="cx-data-note">{t('原记录本身已截断；继续读取不能恢复未保存内容。', 'The source record was truncated. Loading more cannot recover unsaved content.')}</p>}
      {output && state === 'fragment' && <p className="cx-data-note">{t('当前仅包含已保存片段，尚无完整结果。', 'Only a saved fragment is available; the complete result has not been recorded.')}</p>}
      {value?.nextCursor && <button className="cx-button cx-more-content" disabled={props.detailLoading} onClick={() => props.onMoreDetail(part)}>{props.detailLoading ? t('正在读取…', 'Loading…') : t(`继续读取${label}`, `Load more ${label.toLowerCase()}`)}</button>}
    </section>;
  }
  function facts(item: TraceEvent) {
    return <dl className="cx-facts"><dt>{t('状态', 'Status')}</dt><dd title={item.status.explanation}>{statusLabels[item.status.value]}</dd><dt>{t('开始时间', 'Start time')}</dt><dd>{clockTime(item.timing.startMs, language)}</dd><dt>{t('结束时间', 'End time')}</dt><dd>{clockTime(item.timing.endMs, language)}</dd><dt>{t('耗时', 'Duration')}</dt><dd>{duration(item.timing.durationMs)}</dd><dt>{t('时间依据', 'Time basis')}</dt><dd>{item.timing.basis}</dd><dt>{t('所属任务', 'Task')}</dt><dd>{group?.name ?? item.threadId}</dd></dl>;
  }
  function lineage() {
    return <><dl className="cx-facts"><dt>{t('任务 ID', 'Task ID')}</dt><dd>{event?.threadId ?? snapshot?.binding.threadId}</dd><dt>{t('轮次', 'Turn')}</dt><dd>{event?.turnId ?? t('未归属轮次', 'No associated turn')}</dd>{relation ? <><dt>{t('关系类型', 'Relationship type')}</dt><dd>{relation.type}</dd>{relationshipEndpoints(relation, language).map(([label, identifier]) => <Fragment key={label}><dt>{label}</dt><dd>{identifier}</dd></Fragment>)}<dt>{t('确认依据', 'Evidence')}</dt><dd>{relation.resolution} · {relation.evidence.map(source => source.kind === 'api' ? `API item ${source.itemId}` : t(`原始段 ${source.segmentId} 字节 ${source.byteStart}–${source.byteEnd}`, `Raw segment ${source.segmentId}, bytes ${source.byteStart}–${source.byteEnd}`)).join(t('；', '; '))}</dd></> : <><dt>{t('父子关系', 'Parent-child relationship')}</dt><dd>{group?.kind === 'main' ? t('主任务', 'Main task') : t('未记录可靠关联', 'No reliable association recorded')}</dd></>}
      {group?.creationEventId && group.creationEventId !== event?.eventId && <><dt>{t('创建来源', 'Created from')}</dt><dd><button className="cx-parent-link" onClick={() => props.onSelect(group.creationEventId)}>{t('返回创建操作', 'Go to creation event')}</button></dd></>}</dl>
      {group?.readIssue && <p className="cx-data-note cx-error-text">{group.readIssue}{t('；保留已读内容，不据此判断任务失败。', '; loaded content is retained. This does not establish task failure.')}</p>}
      {group && group.kind !== 'main' && <><div className="cx-detail-label">{t('子任务 token · 独立归属', 'Subtask tokens · separate attribution')}</div><UsageFacts values={group.usage} label={group.name} /></>}
    </>;
  }
  function previewContent(item: TraceEvent) {
    if (item.kind === 'modelUsage') return <UsageFacts values={item.usage ?? []} label={t('本次模型请求', 'This model request')} />;
    const available = (part: ContentPart) => Boolean(detail?.parts[part]) || item.content.some(reference => reference.part === part);
    const tool = item.kind === 'toolCall' || item.kind === 'toolResult' || Boolean(item.toolName);
    if (tool) return <>
      {(item.kind !== 'toolResult' || available('input')) && content('input', t('输入', 'Input'), detail?.parts.input)}
      {content('output', t('输出', 'Output'), detail?.parts.output)}
      {available('error') && content('error', t('错误', 'Error'), detail?.parts.error)}
    </>;
    const parts = (['input', 'output'] as const).filter(available);
    if (!parts.length) return <p className="cx-data-note">{props.detailLoading ? t('正在读取内容…', 'Loading content…') : t('该记录尚无可读取正文。', 'This record has no readable content yet.')}</p>;
    return <>{parts.map(part => content(part, item.kind === 'compaction' ? t('已记录的压缩摘要', 'Recorded compaction summary') : t('内容', 'Content'), detail?.parts[part]))}{available('error') && content('error', t('错误', 'Error'), detail?.parts.error)}</>;
  }
  return <aside ref={panel} id="cx-record-details" className={`cx-inspector ${resize.resizing ? 'resizing' : ''}`} style={{ width: resize.width }} role={props.overlay ? 'dialog' : undefined} aria-label={t('所选记录详情', 'Selected record details')} tabIndex={-1}>
    <div className="cx-inspector-resizer" role="separator" tabIndex={0} aria-orientation="vertical" aria-controls="cx-record-details"
      aria-label={t('调整内容预览宽度', 'Resize content preview')} aria-valuemin={resize.min} aria-valuemax={resize.max} aria-valuenow={resize.width}
      aria-valuetext={t(`${resize.width} 像素`, `${resize.width} pixels`)} title={t('拖动调整预览宽度；方向键微调，双击或 Enter 恢复默认', 'Drag to resize preview; use arrow keys to adjust, double-click or press Enter to reset')}
      {...resize.handleProps} />
    <div className="cx-detail-head">{event && <Icon name={eventIcon(event)} />}<strong>{event ? kindLabels[event.kind] : t('记录详情', 'Record details')}</strong><small>{turn ? t(`${group?.kind === 'main' ? '' : '子任务 · '}第 ${turn.order + 1} 轮`, `${group?.kind === 'main' ? '' : 'Subtask · '}Turn ${turn.order + 1}`) : t('轮次未归属', 'No associated turn')}</small><button ref={closeButton} className="cx-icon" aria-label={t('关闭详情', 'Close details')} title={t('关闭详情 · Esc', 'Close details · Esc')} onClick={close}><Icon name="close" /></button></div>
    <div className="cx-tabs" role="tablist" aria-label={t('详情内容', 'Detail content')}>{tabs.map(([tab, title], index) => <button key={tab} id={`cx-tab-${tab}`} role="tab" aria-selected={view.detailTab === tab} aria-controls="cx-detail-content" tabIndex={view.detailTab === tab ? 0 : -1}
      onClick={() => props.onView({ detailTab: tab })} onKeyDown={key => { if (key.key === 'ArrowLeft' || key.key === 'ArrowRight') { key.preventDefault(); const next = tabs[(index + (key.key === 'ArrowRight' ? 1 : 3)) % 4][0]; props.onView({ detailTab: next }); document.getElementById(`cx-tab-${next}`)?.focus(); } }}>{title}</button>)}</div>
    <div ref={body} id="cx-detail-content" className="cx-detail-body" role="tabpanel" aria-labelledby={`cx-tab-${view.detailTab}`} tabIndex={0}>
      {relatedAgents.length > 0 && <nav className="cx-related-agents" aria-label={t('关联 Agent 页面', 'Related agent pages')}><span>{t('关联 Agent', 'Related agents')}</span>{relatedAgents.map(agent => {
        const name = agent.groupId === snapshot?.rootGroupId ? t('主 Agent', 'Main Agent') : agent.name;
        return <button key={agent.groupId} className="cx-agent-jump" title={t(`跳转到 ${name} 的记录页面`, `Go to records for ${name}`)} aria-label={t(`跳转到 ${name}`, `Go to ${name}`)} onClick={() => props.onAgentNavigate?.(agent.groupId)}><Icon name="branch" /><span>{name}</span><Icon name="right" /></button>;
      })}</nav>}
      {!event ? <p className="cx-note">{t('所选记录已不在当前快照中。可重新加载历史；不会自动选中其他记录。', 'The selected record is no longer in this snapshot. Reload history to retrieve it; no other record will be selected automatically.')}</p> : view.detailTab === 'summary' ? <><p className="cx-event-intro">{eventKindDescription(event.kind, language)}</p>{facts(event)}<div className="cx-detail-label">{t('本条目模型 token', 'Entry model tokens')}</div><UsageFacts values={event.usage ?? []} label={t('本次模型请求', 'This model request')} />{turn && <><div className="cx-detail-label">{t('本轮累计 token', 'Turn tokens')}</div><UsageFacts values={turn.usage ?? []} label={t('本轮', 'This turn')} /></>}<p className="cx-message">{event.status.explanation}</p>{lineage()}{content('summary', t('已记录概述', 'Recorded summary'), detail?.parts.summary)}</> : view.detailTab === 'preview' ? <>
        <p className="cx-event-intro">{eventKindDescription(event.kind, language)}</p><div className="cx-detail-meta">{event.toolName ?? kindLabels[event.kind]} · {statusLabels[event.status.value]} · {group?.name ?? t('主任务', 'Main task')}</div>
        {previewContent(event)}
        {event.kind === 'compaction' && <p className="cx-data-note">{t('仅展示已记录的摘要。不推测压缩前后 token，也不重建缺失上下文。', 'Only the recorded summary is shown. Token counts before and after compaction are not inferred, and missing context is not reconstructed.')}</p>}
        {createdGroup && lineage()}
      </> : view.detailTab === 'raw' ? content('raw', t('已关联原始内容', 'Associated raw content'), detail?.parts.raw) : <><h2>{t('记录来源与父子关系', 'Record source and parent-child relationships')}</h2>{facts(event)}{lineage()}{content('source', t('来源依据', 'Source evidence'), detail?.parts.source)}</>}
      {detail?.explanation && <p className="cx-note">{detail.explanation}</p>}
      <span className="cx-copy-status" role="status">{copied === 'success' ? t('已复制当前已加载内容', 'Copied the currently loaded content') : copied === 'unavailable' ? t('复制不可用，请选择文本复制', 'Copy is unavailable. Select the text to copy it.') : ''}</span>
    </div>
  </aside>;
}
