import { Fragment, useLayoutEffect, useRef, useState } from 'react';
import type { ContentPart, TraceEvent, UsageSnapshot } from '../../../contracts/types';
import type { DetailPart, TraceUIProps } from '../ui-types';
import Icon from './icons';
import { eventIcon } from './EventList';
import { clockTime, duration, kindLabels, latestUsage, metric, relationshipEndpoints, statusLabels } from './model';

const tabs = [['summary', '概述'], ['preview', '预览'], ['raw', '原始内容'], ['source', '来源']] as const;
const completeness = { notRecorded: '尚未保存输出', fragment: '已记录片段', complete: '完整结果', unknown: '完整性未知' };
export function UsageFacts({ values, label }: { values: UsageSnapshot[]; label: string }) {
  const usage = latestUsage(values);
  return <><dl className="cx-facts"><dt>输入</dt><dd>{metric(usage?.input, '未记录')}</dd><dt>输出</dt><dd>{metric(usage?.output, '未记录')}</dd><dt>缓存</dt><dd>{metric(usage?.cachedInput, '未记录')}</dd><dt>保存时间</dt><dd>{clockTime(usage?.recordedAt)}</dd><dt>范围</dt><dd>{label} · {usage?.coverage ?? '尚未读取到用量记录'}</dd>{usage && <><dt>归属</dt><dd>{({ selfOnly: '仅当前任务', includesChildren: '包含子任务，不能重复相加', includesInherited: '包含继承上下文，不能重复相加', unseparated: '来源未区分归属' })[usage.attribution]}</dd></>}</dl>
    <p className="cx-data-note">“—”表示未记录，不等于 0。重复读取同一累计快照不会再次相加。</p>
    <p className="cx-data-note">{usage?.cacheSemantics === 'includedInInput' ? '缓存属于输入的一部分，不再与输入相加。' : usage?.cacheSemantics === 'separate' ? '来源将缓存单独记录。' : '缓存与输入的关系未记录。'}</p></>;
}
export default function Inspector(props: TraceUIProps & { overlay?: boolean }) {
  const { snapshot, view } = props;
  const event = snapshot?.events.find(item => item.eventId === view.selectedEventId);
  const detail = props.detail?.eventId === view.selectedEventId ? props.detail : null;
  const body = useRef<HTMLDivElement>(null);
  const panel = useRef<HTMLElement>(null);
  const closeButton = useRef<HTMLButtonElement>(null);
  const opener = useRef<Element | null>(document.activeElement);
  const [copied, setCopied] = useState('');
  const createdGroup = snapshot?.groups.find(group => group.creationEventId === view.selectedEventId);
  const group = createdGroup ?? snapshot?.groups.find(item => item.groupId === event?.groupId);
  const relation = snapshot?.relationships.find(item => item.relationshipId === group?.relationshipId);
  const turn = snapshot?.turns.find(item => item.turnId === event?.turnId && item.threadId === event?.threadId);
  useLayoutEffect(() => {
    // Overlay details replace the usable list surface. Wide split-view details
    // deliberately leave focus on the row so list arrow navigation still works.
    if (props.overlay && !panel.current?.contains(document.activeElement)) closeButton.current?.focus({ preventScroll: true });
  }, [props.overlay, view.selectedEventId]);
  function close() {
    props.onSelect(null);
    requestAnimationFrame(() => {
      const selected = view.selectedEventId && document.querySelector<HTMLButtonElement>(`.cx-scroll [data-event-id="${CSS.escape(view.selectedEventId)}"]`);
      if (selected) selected.focus({ preventScroll: true });
      else if (opener.current instanceof HTMLElement && opener.current.isConnected) opener.current.focus({ preventScroll: true });
      else document.querySelector<HTMLElement>('.cx-scroll')?.focus({ preventScroll: true });
    });
  }
  function containTab(event: React.KeyboardEvent<HTMLElement>) {
    if (!props.overlay || event.key !== 'Tab') return;
    const focusable = [...panel.current!.querySelectorAll<HTMLElement>('button,input,select,textarea,a[href],[tabindex]')]
      .filter(element => element.tabIndex >= 0 && !element.hasAttribute('disabled') && !element.closest('[inert]') && element.getClientRects().length > 0);
    if (!focusable.length) { event.preventDefault(); panel.current?.focus(); return; }
    const first = focusable[0], last = focusable.at(-1)!;
    if (event.shiftKey && (document.activeElement === first || document.activeElement === panel.current)) { event.preventDefault(); last.focus(); }
    else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus(); }
  }
  async function copy(text: string) {
    try {
      if (!navigator.clipboard?.writeText) throw new Error('clipboard-unavailable');
      await navigator.clipboard.writeText(text); setCopied('已复制当前已加载内容');
    } catch {
      const active = document.activeElement;
      const textarea = document.createElement('textarea');
      textarea.value = text; textarea.style.position = 'fixed'; textarea.style.opacity = '0';
      let success = false;
      try { document.body.appendChild(textarea); textarea.select(); success = document.execCommand('copy'); }
      catch { /* The host can deny clipboard access; do not claim success. */ }
      finally { textarea.remove(); if (active instanceof HTMLElement && active.isConnected) active.focus({ preventScroll: true }); }
      setCopied(success ? '已复制当前已加载内容' : '复制不可用，请选择文本复制');
    }
    setTimeout(() => setCopied(''), 2000);
  }
  function content(part: ContentPart, label: string, value: DetailPart | undefined) {
    const state = value?.completeness ?? event?.content.find(ref => ref.part === part)?.completeness ?? 'notRecorded';
    const output = part === 'output' && label === '输出';
    return <section className="cx-content-part" key={part}>
      <div className="cx-detail-label">{label}{output && <span className="cx-output-kind">{completeness[state as keyof typeof completeness] ?? '完整性未知'}</span>}{value?.text && <button className="cx-icon" aria-label={`复制当前已加载的${label}`} title={`仅复制当前已加载的${label}；未加载部分不包含在内`} onClick={() => copy(value.text)}><Icon name="copy" /></button>}</div>
      {value?.text ? <pre className={`cx-code ${part === 'error' ? 'error' : ''}`}>{value.text}</pre> : <p className="cx-data-note">{props.detailLoading ? '正在读取内容…' : part === 'raw' ? '没有可确认关联的原始内容。' : part === 'output' ? '当前没有可读取的输出；部分工具结果会在结束后保存。' : '该内容未记录或暂不可读。'}</p>}
      {value?.sourceTruncated && <p className="cx-data-note">原记录本身已截断；继续读取不能恢复未保存内容。</p>}
      {output && state === 'fragment' && <p className="cx-data-note">当前仅包含已保存片段，尚无完整结果。</p>}
      {value?.nextCursor && <button className="cx-button cx-more-content" disabled={props.detailLoading} onClick={() => props.onMoreDetail(part)}>{props.detailLoading ? '正在读取…' : `继续读取${label}`}</button>}
    </section>;
  }
  function facts(item: TraceEvent) {
    return <dl className="cx-facts"><dt>状态</dt><dd title={item.status.explanation}>{statusLabels[item.status.value]}</dd><dt>开始时间</dt><dd>{clockTime(item.timing.startMs)}</dd><dt>结束时间</dt><dd>{clockTime(item.timing.endMs)}</dd><dt>耗时</dt><dd>{duration(item.timing.durationMs)}</dd><dt>时间依据</dt><dd>{item.timing.basis}</dd><dt>所属任务</dt><dd>{group?.name ?? item.threadId}</dd></dl>;
  }
  function lineage() {
    return <><dl className="cx-facts"><dt>任务 ID</dt><dd>{event?.threadId ?? snapshot?.binding.threadId}</dd><dt>轮次</dt><dd>{event?.turnId ?? '未归属轮次'}</dd>{relation ? <><dt>关系类型</dt><dd>{relation.type}</dd>{relationshipEndpoints(relation).map(([label, identifier]) => <Fragment key={label}><dt>{label}</dt><dd>{identifier}</dd></Fragment>)}<dt>确认依据</dt><dd>{relation.resolution} · {relation.evidence.map(source => source.kind === 'api' ? `API item ${source.itemId}` : `原始段 ${source.segmentId} 字节 ${source.byteStart}–${source.byteEnd}`).join('；')}</dd></> : <><dt>父子关系</dt><dd>{group?.kind === 'main' ? '主任务' : '未记录可靠关联'}</dd></>}
      {group?.creationEventId && group.creationEventId !== event?.eventId && <><dt>创建来源</dt><dd><button className="cx-parent-link" onClick={() => props.onSelect(group.creationEventId)}>返回创建操作</button></dd></>}</dl>
      {group?.readIssue && <p className="cx-data-note cx-error-text">{group.readIssue}；保留已读内容，不据此判断任务失败。</p>}
      {group && group.kind !== 'main' && <><div className="cx-detail-label">子任务 token · 独立归属</div><UsageFacts values={group.usage} label={group.name} /></>}
    </>;
  }
  function previewContent(item: TraceEvent) {
    const available = (part: ContentPart) => Boolean(detail?.parts[part]) || item.content.some(reference => reference.part === part);
    const tool = item.kind === 'toolCall' || item.kind === 'toolResult' || Boolean(item.toolName);
    if (tool) return <>
      {(item.kind !== 'toolResult' || available('input')) && content('input', '输入', detail?.parts.input)}
      {content('output', '输出', detail?.parts.output)}
      {available('error') && content('error', '错误', detail?.parts.error)}
    </>;
    const parts = (['input', 'output'] as const).filter(available);
    if (!parts.length) return <p className="cx-data-note">{props.detailLoading ? '正在读取内容…' : '该记录尚无可读取正文。'}</p>;
    return <>{parts.map(part => content(part, item.kind === 'compaction' ? '已记录的压缩摘要' : '内容', detail?.parts[part]))}{available('error') && content('error', '错误', detail?.parts.error)}</>;
  }
  return <aside ref={panel} className="cx-inspector" role={props.overlay ? 'dialog' : undefined} aria-label="所选记录详情" tabIndex={-1} onKeyDown={containTab}>
    <div className="cx-detail-head">{event && <Icon name={eventIcon(event)} />}<strong>{event ? kindLabels[event.kind] : '记录详情'}</strong><small>{turn ? `${group?.kind === 'main' ? '' : '子任务 · '}第 ${turn.order + 1} 轮` : '轮次未归属'}</small><button ref={closeButton} className="cx-icon" aria-label="关闭详情" title="关闭详情 · Esc" onClick={close}><Icon name="close" /></button></div>
    <div className="cx-tabs" role="tablist" aria-label="详情内容">{tabs.map(([tab, title], index) => <button key={tab} id={`cx-tab-${tab}`} role="tab" aria-selected={view.detailTab === tab} aria-controls="cx-detail-content" tabIndex={view.detailTab === tab ? 0 : -1}
      onClick={() => props.onView({ detailTab: tab })} onKeyDown={key => { if (key.key === 'ArrowLeft' || key.key === 'ArrowRight') { key.preventDefault(); const next = tabs[(index + (key.key === 'ArrowRight' ? 1 : 3)) % 4][0]; props.onView({ detailTab: next }); document.getElementById(`cx-tab-${next}`)?.focus(); } }}>{title}</button>)}</div>
    <div ref={body} id="cx-detail-content" className="cx-detail-body" role="tabpanel" aria-labelledby={`cx-tab-${view.detailTab}`} tabIndex={0}>
      {!event ? <p className="cx-note">所选记录已不在当前快照中。可重新加载历史；不会自动选中其他记录。</p> : view.detailTab === 'summary' ? <><h2>{detail?.summary || event.preview || kindLabels[event.kind]}</h2>{facts(event)}<p className="cx-message">{event.status.explanation}</p>{lineage()}{content('summary', '已记录概述', detail?.parts.summary)}</> : view.detailTab === 'preview' ? <>
        <h2>{detail?.summary || event.preview || kindLabels[event.kind]}</h2><div className="cx-detail-meta">{event.toolName ?? kindLabels[event.kind]} · {statusLabels[event.status.value]} · {group?.name ?? '主任务'}</div>
        {previewContent(event)}
        {event.kind === 'compaction' && <p className="cx-data-note">仅展示已记录的摘要。不推测压缩前后 token，也不重建缺失上下文。</p>}
        {createdGroup && lineage()}
      </> : view.detailTab === 'raw' ? content('raw', '已关联原始内容', detail?.parts.raw) : <><h2>记录来源与父子关系</h2>{facts(event)}{lineage()}{content('source', '来源依据', detail?.parts.source)}</>}
      {detail?.explanation && <p className="cx-note">{detail.explanation}</p>}
      <span className="cx-copy-status" role="status">{copied}</span>
    </div>
  </aside>;
}
