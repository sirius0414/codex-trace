import type { ChangeBatch, ReadChangesRequest, ThreadSnapshot } from '../../../contracts/types';
import { sameBinding } from './binding.mjs';
import { mergeGroupCoverage } from './coverage.mjs';

export class ChangeProtocolError extends Error {
  constructor() { super('增量记录批次不连续，保留已有内容并重试。'); this.name = 'ChangeProtocolError'; }
}

/** Freeze a logical batch until every continuation page has arrived. */
export class ChangeAssembly {
  private pages: ChangeBatch[] = [];
  private cursors = new Set<string>();
  private itemCount = 0;
  readonly request: ReadChangesRequest;
  constructor(request: ReadChangesRequest) { this.request = request; }
  append(page: ChangeBatch) {
    if (!sameBinding(page.binding, this.request.binding)) throw new ChangeProtocolError();
    if (page.resetRequired) return 'reset' as const;
    const first = this.pages[0];
    if (page.generation !== this.request.generation || page.baseRevision !== this.request.afterRevision
      || !Number.isSafeInteger(page.revision) || page.revision < page.baseRevision
      || first && page.revision !== first.revision || !Array.isArray(page.upsertEvents) || !Array.isArray(page.removeEventIds)) throw new ChangeProtocolError();
    if (page.continuationCursor !== null && (typeof page.continuationCursor !== 'string' || !page.continuationCursor.length || this.cursors.has(page.continuationCursor))) throw new ChangeProtocolError();
    this.itemCount += page.upsertEvents.length + page.removeEventIds.length;
    if (this.pages.length >= 1024 || this.itemCount > 100000) throw new ChangeProtocolError();
    if (page.continuationCursor) this.cursors.add(page.continuationCursor);
    this.pages.push(page);
    return page.continuationCursor ? 'more' as const : 'complete' as const;
  }
  finish(): ChangeBatch {
    const last = this.pages.at(-1);
    if (!last || last.continuationCursor !== null) throw new ChangeProtocolError();
    const upserts = new Map<string, ChangeBatch['upsertEvents'][number]>();
    const removals = new Set<string>();
    for (const page of this.pages) {
      for (const event of page.upsertEvents) {
        const previous = upserts.get(event.eventId);
        if (!previous || event.eventRevision > previous.eventRevision) upserts.set(event.eventId, event);
      }
      for (const identifier of page.removeEventIds) removals.add(identifier);
    }
    if ([...removals].some(identifier => upserts.has(identifier))) throw new ChangeProtocolError();
    return { ...last, upsertEvents: [...upserts.values()], removeEventIds: [...removals] };
  }
}

/** One atomic update; a late batch cannot roll back a newer historical page. */
export function mergeChanges(current: ThreadSnapshot, batch: ChangeBatch): ThreadSnapshot {
  if (!sameBinding(current.binding, batch.binding) || batch.resetRequired || current.generation !== batch.generation
    || batch.baseRevision > current.revision) throw new ChangeProtocolError();
  const events = new Map(current.events.map(event => [event.eventId, event]));
  for (const id of batch.removeEventIds) events.delete(id);
  for (const event of batch.upsertEvents) {
    const old = events.get(event.eventId);
    if (!old || event.eventRevision > old.eventRevision) events.set(event.eventId, event);
  }
  const sorted = [...events.values()].sort((a, b) => (a.order.section === b.order.section ? 0 : a.order.section === 'context' ? -1 : 1)
    || a.order.ordinal - b.order.ordinal || a.eventId.localeCompare(b.eventId));
  const scope = batch.coverage.scopeRevision > current.coverage.scopeRevision ? batch
    : batch.coverage.scopeRevision < current.coverage.scopeRevision ? current
      : batch.revision >= current.revision ? batch : current;
  const coverage = scope.coverage, scopeGroups = scope.groups;
  const groups = mergeGroupCoverage(batch.revision < current.revision ? current.groups : batch.groups, scopeGroups);
  if (batch.revision < current.revision) return { ...current, events: sorted, coverage, groups };
  return { ...current, revision: batch.revision, events: sorted, turns: batch.turns, groups,
    relationships: batch.relationships, statistics: batch.statistics, status: batch.status, timelineOrigin: batch.timelineOrigin,
    mainUsage: batch.mainUsage, coverage, freshness: batch.freshness,
    updatedAt: Math.max(current.updatedAt, batch.freshness.lastSuccessfulReadAt ?? 0) };
}

export type ChangeTiming = { requestStartedAt: number; receivedAt: number; generation: number; revision: number; upsertCount: number };
type Context = { snapshot: ThreadSnapshot; afterRevision?: number; isCurrent: () => boolean };
type ReaderOptions = {
  capture: () => Context | null;
  read: (request: ReadChangesRequest, signal: AbortSignal) => Promise<ChangeBatch>;
  commit: (batch: ChangeBatch, timing: ChangeTiming) => void;
  reset: (batch: ChangeBatch, signal: AbortSignal) => Promise<void>;
  failed: (failure: unknown) => void;
  received?: (timing: ChangeTiming) => void;
  now?: () => number;
  setTimer?: (callback: () => void, delay: number) => unknown;
  clearTimer?: (timer: unknown) => void;
};

/** Serialized foreground reader. Follow/scroll state is intentionally absent. */
export class ChangeReader {
  private active = false;
  private visible = true;
  private disposed = false;
  private busy = false;
  private wakeRequested = false;
  private generation = 0;
  private failures = 0;
  private timer: unknown = null;
  private controller: AbortController | null = null;
  private now: () => number;
  private setTimer: (callback: () => void, delay: number) => unknown;
  private clearTimer: (timer: unknown) => void;
  private options: ReaderOptions;
  constructor(options: ReaderOptions) {
    this.options = options;
    this.now = options.now ?? (() => performance.now());
    this.setTimer = options.setTimer ?? ((callback, delay) => setTimeout(callback, delay));
    this.clearTimer = options.clearTimer ?? (timer => clearTimeout(timer as ReturnType<typeof setTimeout>));
  }
  start() { if (this.disposed) return; this.active = true; this.failures = 0; this.schedule(0); }
  pause() { this.active = false; this.cancel(); }
  setVisible(visible: boolean) {
    if (visible === this.visible) return;
    this.visible = visible;
    if (!visible) this.cancel(); else if (this.active) this.schedule(0);
  }
  retry() { this.failures = 0; if (this.active) this.schedule(0); }
  dispose() { this.disposed = true; this.active = false; this.cancel(); }
  private cancel() {
    this.generation++; this.wakeRequested = false;
    if (this.timer !== null) { this.clearTimer(this.timer); this.timer = null; }
    this.controller?.abort();
  }
  private schedule(delay: number) {
    if (this.disposed || !this.active || !this.visible) return;
    if (this.busy) { this.wakeRequested = true; return; }
    if (this.timer !== null) this.clearTimer(this.timer);
    this.timer = this.setTimer(() => { this.timer = null; void this.tick(); }, Math.max(0, delay));
  }
  private async tick() {
    if (this.busy || this.disposed || !this.active || !this.visible) return;
    const context = this.options.capture();
    if (!context) return;
    this.busy = true;
    const generation = this.generation;
    const controller = new AbortController(); this.controller = controller;
    const requestStartedAt = this.now();
    let delay = 750;
    const valid = () => !this.disposed && this.active && this.visible && generation === this.generation && context.isCurrent();
    const request: ReadChangesRequest = { binding: context.snapshot.binding, generation: context.snapshot.generation,
      afterRevision: context.afterRevision ?? context.snapshot.revision, continuationCursor: null };
    const assembly = new ChangeAssembly(request);
    let firstReceivedAt: number | null = null;
    try {
      for (;;) {
        const response = await this.options.read({ ...request }, controller.signal);
        if (!valid()) return;
        firstReceivedAt ??= this.now();
        const state = assembly.append(response);
        if (state === 'reset') { await this.options.reset(response, controller.signal); if (!valid()) return; break; }
        if (state === 'more') { request.continuationCursor = response.continuationCursor; continue; }
        const batch = assembly.finish();
        const timing = { requestStartedAt, receivedAt: firstReceivedAt, generation: batch.generation, revision: batch.revision, upsertCount: batch.upsertEvents.length };
        this.options.received?.(timing);
        this.options.commit(batch, timing);
        break;
      }
      this.failures = 0;
      delay = Math.max(0, 750 - (this.now() - requestStartedAt));
    } catch (failure) {
      if (!valid()) return;
      this.options.failed(failure);
      delay = [1000, 2000, 5000, 10000][Math.min(this.failures++, 3)];
    } finally {
      this.busy = false;
      if (this.controller === controller) this.controller = null;
      const immediate = this.wakeRequested; this.wakeRequested = false;
      if (this.active && this.visible && !this.disposed) this.schedule(immediate ? 0 : delay);
    }
  }
}

/** Opt-in, metadata-only local harness instrumentation; disabled in normal UI. */
declare global {
  interface Window {
    __CODEX_DISPLAY_TIMING__?: boolean;
    __CODEX_DISPLAY_TIMINGS__?: Array<ChangeTiming & { phase: 'received' | 'committed' | 'dom'; at: number }>;
  }
}
export function recordChangeTiming(phase: 'received' | 'committed' | 'dom', timing: ChangeTiming) {
  if (typeof window === 'undefined' || window.__CODEX_DISPLAY_TIMING__ !== true) return;
  const records = window.__CODEX_DISPLAY_TIMINGS__ ??= [];
  records.push({ ...timing, phase, at: performance.now() });
  if (records.length > 300) records.splice(0, records.length - 300);
}
