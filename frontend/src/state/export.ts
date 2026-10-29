import type { Binding, DownloadRequest, DownloadResult, ExportManifest } from '../../../contracts/types';
import { sameBinding } from './binding.mjs';
import { isExportReceiptPath } from '../bridge/local-path.mjs';

const STORAGE_KEY = 'codex-display.export-intents.v1';
const KEY = /^[A-Za-z0-9][A-Za-z0-9_-]{7,127}$/;
const FILENAME = /^codex-display-[A-Za-z0-9][A-Za-z0-9._-]*\.zip$/;
const HASH = /^[0-9a-f]{64}$/;
// create/commit 分别使用稳定请求 ID；Unknown 仍是待核对状态，只有终态允许新建意图。
const phases = ['creating', 'collecting', 'ready', 'committing', 'pending', 'createUnknown', 'saveUnknown', 'verified', 'failed', 'released'] as const;
export type ExportPhase = typeof phases[number];
export type ExportIntent = { sourceInstanceId: string; threadId: string; createRequestId: string; commitRequestId: string; exportId: string | null; phase: ExportPhase };
export type ExportFeedback = { busy: boolean; message: string; messageEn: string };
type ExportMessage = { zh: string; en: string };
const translated = (zh: string, en: string): ExportMessage => ({ zh, en });
type StorageLike = Pick<Storage, 'getItem' | 'setItem'>;
type KnownExport = { filename: string | null; totalBytes: number | null; manifest: ExportManifest | null; report?: ExportMessage };
type Options = {
  call: (request: DownloadRequest, signal: AbortSignal) => Promise<DownloadResult>;
  onChange: (feedback: ExportFeedback) => void;
  storage?: StorageLike | null;
  randomId?: () => string;
  now?: () => number;
  setTimer?: (callback: () => void, delay: number) => unknown;
  clearTimer?: (timer: unknown) => void;
  pollLimit?: number;
  activeTimeLimitMs?: number;
};
const operationKey = (value: Pick<ExportIntent, 'sourceInstanceId' | 'threadId'>) => JSON.stringify([value.sourceInstanceId, value.threadId]);
const savedPhase = (phase: ExportPhase) => ['committing', 'pending', 'saveUnknown', 'verified'].includes(phase);
const terminalPhase = (phase: ExportPhase) => ['verified', 'failed', 'released'].includes(phase);
const integer = (value: unknown): value is number => Number.isSafeInteger(value) && Number(value) >= 0;
const identifier = (value: unknown): value is string => typeof value === 'string' && value.length > 0 && value.length <= 512 && !/[\x00-\x1f\x7f]/.test(value);

export class ExportResponseError extends Error {
  constructor() { super('导出响应无法核对，结果未确认；再次点击将核对同一次请求。'); this.name = 'ExportResponseError'; }
}
function requireValid(condition: unknown): asserts condition { if (!condition) throw new ExportResponseError(); }

/** 回执须与观察绑定、原请求及冻结产物一致；字段自称 verified 仍不足以直接显示成功。 */
export function validateExportResult(value: unknown, binding: Binding, intent: ExportIntent, known?: KnownExport): DownloadResult {
  requireValid(value && typeof value === 'object' && !Array.isArray(value));
  const result = value as DownloadResult;
  requireValid(sameBinding(result.binding, binding) && ['collecting', 'ready', 'failed', 'released', 'unknown'].includes(result.status));
  requireValid(result.save && typeof result.save === 'object' && typeof result.explanation === 'string' && result.explanation.length <= 8192);
  requireValid(result.chunk === null);
  if (result.status === 'unknown') {
    requireValid(result.exportId === null && result.createRequestId === null && result.filename === null && result.totalBytes === null
      && result.manifest === null && result.save.status === 'unknown');
  } else {
    requireValid(identifier(result.exportId) && result.createRequestId === intent.createRequestId);
    requireValid(!intent.exportId || result.exportId === intent.exportId);
  }
  requireValid(result.expiresAt === null || integer(result.expiresAt));
  requireValid(result.filename === null || typeof result.filename === 'string' && result.filename.length <= 255 && FILENAME.test(result.filename));
  requireValid(result.totalBytes === null || integer(result.totalBytes));
  if (result.status !== 'unknown') {
    requireValid(!known?.filename || result.filename === null || result.filename === known.filename);
    requireValid(known?.totalBytes == null || result.totalBytes === null || result.totalBytes === known.totalBytes);
  }
  if (result.manifest !== null) {
    const manifest = result.manifest;
    requireValid(manifest && manifest.schemaVersion === '1.0.0' && manifest.rootThreadId === binding.threadId && manifest.atomicSnapshot === false
      && integer(manifest.enumerationCutoff) && integer(manifest.collectionStartedAt)
      && (manifest.collectionEndedAt === null || integer(manifest.collectionEndedAt) && manifest.collectionEndedAt >= manifest.collectionStartedAt)
      && Array.isArray(manifest.sources) && Array.isArray(manifest.relationships) && typeof manifest.enumerationComplete === 'boolean'
      && Array.isArray(manifest.unresolvedRanges) && manifest.unresolvedRanges.every(item => typeof item === 'string'));
    requireValid(manifest.sources.every(source => source && identifier(source.sourceId) && identifier(source.threadId)
      && ['copied', 'partial', 'unreadable', 'unsafeInheritedRange'].includes(source.state)));
  }
  const save = result.save;
  requireValid(['notRequested', 'pending', 'verified', 'failed', 'unknown'].includes(save.status));
  requireValid(save.status === 'notRequested' ? save.requestId === null : save.requestId === intent.commitRequestId);
  if (save.status === 'pending' || save.status === 'verified' || save.status === 'failed') requireValid(['ready', 'released'].includes(result.status));
  if (save.status === 'unknown') requireValid(['requestNotRecorded', 'verificationUnavailable'].includes(save.reason));
  if (save.status === 'failed') requireValid(['destinationUnavailable', 'permissionDenied', 'resourceLimit', 'writeFailed'].includes(save.code));
  if (result.status === 'ready') requireValid(result.filename !== null && result.totalBytes !== null);
  if (save.status === 'verified') {
    // 此处核对响应内部的一致性；实际目录约束、落盘与读回校验由服务端执行。
    requireValid(save.filename === result.filename && save.bytes === result.totalBytes && integer(save.bytes) && integer(save.verifiedAt)
      && typeof save.sha256 === 'string' && HASH.test(save.sha256) && isExportReceiptPath(save.path, save.filename));
  }
  return result;
}

/** 保存成功与来源完整性分别报告，ZIP 已校验不代表所有原始来源都已读到。 */
export function exportScopeMessage(manifest: ExportManifest | null, language: 'zh' | 'en' = 'zh') {
  if (!manifest) return language === 'zh' ? '来源范围以 ZIP 清单为准，当前未取得清单，不能确认来源完整性。'
    : 'The ZIP manifest defines source coverage. The manifest is unavailable, so source completeness cannot be confirmed.';
  const copied = manifest.sources.filter(source => source.state === 'copied').length;
  const incomplete = !manifest.enumerationComplete || manifest.unresolvedRanges.length > 0 || copied !== manifest.sources.length;
  const date = (value: number) => new Date(value).toLocaleString(language === 'zh' ? 'zh-CN' : 'en-US', { hour12: false });
  if (language === 'en') {
    const interval = manifest.collectionEndedAt == null ? `collection started at ${date(manifest.collectionStartedAt)}`
      : `collected ${date(manifest.collectionStartedAt)}–${date(manifest.collectionEndedAt)}`;
    return `Main task and confirmed subtasks; ${interval}. ${incomplete ? `Source coverage is incomplete (${copied}/${manifest.sources.length} sources copied in full); see the manifest for unread ranges.` : `${copied} sources copied.`} Source cutoffs are listed in the manifest; this is not a simultaneous snapshot across files.`;
  }
  const interval = manifest.collectionEndedAt == null ? `开始采集于 ${date(manifest.collectionStartedAt)}`
    : `采集于 ${date(manifest.collectionStartedAt)}–${date(manifest.collectionEndedAt)}`;
  return `主任务及已确认子任务；${interval}。${incomplete ? `来源范围不完整（完整复制 ${copied}/${manifest.sources.length} 个来源），未读取范围见清单。` : `${copied} 个来源已复制。`}各来源截止位置见清单，并非跨文件同一时刻快照。`;
}

/** 导出状态机：采集就绪后提交保存，未确认时沿用原请求核对，verified 后才显示成功。
 * 页面恢复只存 ID 和阶段；路径、清单与正文不写入 sessionStorage。
 */
export class ExportCoordinator {
  private options: Options;
  private intents = new Map<string, ExportIntent>();
  private known = new Map<string, KnownExport>();
  private storage: StorageLike | null;
  private storageUnavailable = false;
  private binding: Binding | null = null;
  private generation = 0;
  private visible = true;
  private disposed = false;
  private controller: AbortController | null = null;
  private running: Promise<void> | null = null;
  private queue: Promise<unknown> = Promise.resolve();
  private visibilityWaiters = new Set<() => void>();
  private now: () => number;
  private setTimer: (callback: () => void, delay: number) => unknown;
  private clearTimer: (timer: unknown) => void;
  constructor(options: Options) {
    this.options = options; this.now = options.now ?? (() => performance.now());
    this.setTimer = options.setTimer ?? ((callback, delay) => setTimeout(callback, delay));
    this.clearTimer = options.clearTimer ?? (timer => clearTimeout(timer as ReturnType<typeof setTimeout>));
    try { this.storage = options.storage === undefined ? globalThis.sessionStorage : options.storage; }
    catch { this.storage = null; }
    if (!this.storage) this.storageUnavailable = true;
    this.restore();
  }
  private restore() {
    if (!this.storage) return;
    try {
      const encoded = this.storage.getItem(STORAGE_KEY);
      if (!encoded) return;
      requireValid(encoded.length <= 200000);
      const values = JSON.parse(encoded);
      requireValid(Array.isArray(values) && values.length <= 128);
      for (const value of values) {
        requireValid(value && identifier(value.sourceInstanceId) && identifier(value.threadId) && KEY.test(value.createRequestId)
          && KEY.test(value.commitRequestId) && value.createRequestId !== value.commitRequestId
          && (value.exportId === null || identifier(value.exportId)) && phases.includes(value.phase));
        const intent: ExportIntent = { sourceInstanceId: value.sourceInstanceId, threadId: value.threadId,
          createRequestId: value.createRequestId, commitRequestId: value.commitRequestId, exportId: value.exportId, phase: value.phase };
        this.intents.set(operationKey(intent), intent);
      }
    } catch { this.storageUnavailable = true; }
  }
  private persist() {
    try { if (this.storage) this.storage.setItem(STORAGE_KEY, JSON.stringify([...this.intents.values()])); }
    catch { this.storageUnavailable = true; }
  }
  private feedback(busy: boolean, message: ExportMessage) {
    if (!this.disposed) this.options.onChange({ busy,
      message: message.zh && this.storageUnavailable
        ? `${message.zh} 当前容器无法持久保存恢复标识，请保持本页面以便核对同一次请求。` : message.zh,
      messageEn: message.en && this.storageUnavailable
        ? `${message.en} This container cannot persist recovery identifiers. Keep this page open to verify the same request.` : message.en });
  }
  bind(binding: Binding | null) {
    if (sameBinding(this.binding, binding) || !this.binding && !binding) return;
    const previous = this.binding, previousIntent = previous ? this.intents.get(operationKey(previous)) : undefined;
    // 界面代次终止旧反馈，但恢复键按来源和任务保留，重选同一任务仍可核对原保存。
    this.generation++; this.controller?.abort(); this.controller = null; this.running = null;
    this.binding = binding ? { ...binding } : null;
    if (previous && previousIntent?.exportId) this.release(previous, previousIntent);
    const intent = binding ? this.intents.get(operationKey(binding)) : undefined;
    const known = intent ? this.known.get(intent.createRequestId) : undefined;
    this.feedback(false, known?.report ?? (!intent ? translated('', '') : terminalPhase(intent.phase)
      ? translated('上次导出流程已结束；点击“导出记录”可采集新的快照。', 'The previous export has ended. Click “Export records” to collect a new snapshot.')
      : savedPhase(intent.phase) ? translated('上次保存结果尚待核对；点击“导出记录”核对同一次请求。', 'The previous save still needs verification. Click “Export records” to check the same request.')
        : translated('上次导出尚未提交保存；点击“导出记录”继续核对。', 'The previous export has not been submitted for saving. Click “Export records” to continue checking it.')));
  }
  setVisible(visible: boolean) {
    this.visible = visible;
    if (visible) { for (const wake of this.visibilityWaiters) wake(); this.visibilityWaiters.clear(); }
  }
  dispose() {
    if (this.disposed) return;
    const binding = this.binding, intent = binding ? this.intents.get(operationKey(binding)) : undefined;
    this.disposed = true; this.generation++; this.controller?.abort(); this.binding = null;
    if (binding && intent?.exportId) this.release(binding, intent);
  }
  private rpc(request: DownloadRequest, signal: AbortSignal, beforeDispatch?: () => void) {
    // release 与后续 commit 共用串行通道，派发前检查可识别排队期间已释放的快照。
    const pending = this.queue.then(() => { signal.throwIfAborted(); beforeDispatch?.(); return this.options.call(request, signal); });
    this.queue = pending.catch(() => undefined);
    return pending;
  }
  private release(binding: Binding, intent: ExportIntent) {
    if (!intent.exportId) return;
    const controller = new AbortController();
    void this.rpc({ action: 'release', binding, exportId: intent.exportId }, controller.signal).then(value => {
      const result = validateExportResult(value, binding, intent, this.known.get(intent.createRequestId));
      // release 只释放临时材料，不能取消已接受的保存，也不能丢弃尚待核对的回执。
      if (result.status === 'released' && !savedPhase(intent.phase) && result.save.status === 'notRequested') {
        intent.phase = 'released'; this.persist();
      }
    }).catch(() => { /* 释放失败不改变保存结论；临时资源另有服务端过期回收。 */ });
  }
  private sleep(delay: number, signal: AbortSignal) {
    return new Promise<void>((resolve, reject) => {
      signal.throwIfAborted();
      const aborted = () => { this.clearTimer(timer); reject(new DOMException('Cancelled', 'AbortError')); };
      const timer = this.setTimer(() => { signal.removeEventListener('abort', aborted); resolve(); }, delay);
      signal.addEventListener('abort', aborted, { once: true });
    });
  }
  private waitVisible(signal: AbortSignal) {
    if (this.visible) return Promise.resolve();
    return new Promise<void>((resolve, reject) => {
      const finish = () => { this.visibilityWaiters.delete(finish); signal.removeEventListener('abort', aborted); resolve(); };
      const aborted = () => { this.visibilityWaiters.delete(finish); reject(new DOMException('Cancelled', 'AbortError')); };
      signal.throwIfAborted(); this.visibilityWaiters.add(finish); signal.addEventListener('abort', aborted, { once: true });
    });
  }
  run(): Promise<void> {
    if (this.disposed || !this.binding) return Promise.resolve();
    if (this.running) return this.running;
    const binding = { ...this.binding }, key = operationKey(binding);
    let intent = this.intents.get(key);
    if (!intent || terminalPhase(intent.phase)) {
      if (!intent && this.intents.size >= 128) {
        // 只淘汰终态意图；未完成/未确认的恢复键必须保留，即使因此无法新建导出。
        for (const [previousKey, previous] of this.intents) {
          if (!terminalPhase(previous.phase)) continue;
          this.intents.delete(previousKey); this.known.delete(previous.createRequestId);
          if (this.intents.size < 128) break;
        }
      }
      if (!intent && this.intents.size >= 128) { this.feedback(false, translated('待核对导出意图数量已达上限，请先核对已有请求。', 'The limit for unverified exports has been reached. Verify existing requests first.')); return Promise.resolve(); }
      const random = this.options.randomId ?? (() => crypto.randomUUID());
      const createRequestId = random(), commitRequestId = random();
      if (!KEY.test(createRequestId) || !KEY.test(commitRequestId) || createRequestId === commitRequestId) {
        this.feedback(false, translated('无法生成独立导出请求标识，尚未提交。', 'Unable to generate distinct export request identifiers. Nothing has been submitted.')); return Promise.resolve();
      }
      if (intent) this.known.delete(intent.createRequestId);
      intent = { sourceInstanceId: binding.sourceInstanceId, threadId: binding.threadId, createRequestId, commitRequestId, exportId: null, phase: 'creating' };
      this.intents.set(key, intent); this.persist();
    }
    const controller = new AbortController(); this.controller = controller;
    const generation = this.generation;
    const recoverCommit = intent.phase === 'saveUnknown' && !!intent.exportId;
    const pending = this.perform(binding, intent, generation, controller.signal, recoverCommit).catch(() => {
      // 取消后的旧流程不再写界面；本地异常只解除忙碌态，不推断保存已失败。
      if (!controller.signal.aborted && !this.disposed && this.generation === generation && sameBinding(this.binding, binding)) {
        intent.phase = savedPhase(intent.phase) ? 'saveUnknown' : 'createUnknown'; this.persist();
        this.feedback(false, translated('导出结果未确认；再次点击“导出记录”将核对同一次请求。', 'The export result is unconfirmed. Click “Export records” again to check the same request.'));
      }
    });
    this.running = pending;
    void pending.finally(() => { if (this.running === pending) this.running = null; if (this.controller === controller) this.controller = null; });
    return pending;
  }
  private async perform(binding: Binding, intent: ExportIntent, generation: number, signal: AbortSignal, recoverCommit: boolean) {
    const valid = () => !signal.aborted && !this.disposed && this.generation === generation && sameBinding(this.binding, binding);
    const started = this.now(); let paused = 0, count = 0, failures = 0;
    while (valid()) {
      // 每轮派发前等待页面可见；仅 waitVisible 的等待扣除预算，在途请求仍可完成。
      // 页面恢复后仍沿用同一组请求 ID。
      const pausedAt = this.now(); await this.waitVisible(signal); paused += this.now() - pausedAt;
      if (!valid()) return;
      if (count >= (this.options.pollLimit ?? 60) || this.now() - started - paused >= (this.options.activeTimeLimitMs ?? 120000)) {
        this.feedback(false, translated('等待已达到本次核对时限，结果未确认；再次点击“导出记录”将继续同一次请求。', 'The verification time limit has been reached, and the result is unconfirmed. Click “Export records” again to continue the same request.')); return;
      }
      let request: DownloadRequest;
      const known = this.known.get(intent.createRequestId);
      const explicitRecovery = recoverCommit; recoverCommit = false;
      if (explicitRecovery) request = { action: 'commit', binding, exportId: intent.exportId!, requestId: intent.commitRequestId };
      else if (savedPhase(intent.phase)) request = { action: 'result', binding, requestId: intent.commitRequestId };
      else if (intent.phase === 'ready' && known?.filename && known.totalBytes !== null) {
        request = { action: 'commit', binding, exportId: intent.exportId!, requestId: intent.commitRequestId };
      } else if (intent.exportId) request = { action: 'status', binding, exportId: intent.exportId };
      else request = { action: 'create', binding, requestId: intent.createRequestId };
      this.feedback(true, request.action === 'create' ? translated('正在冻结所选任务及已确认子任务的可读原始记录…', 'Freezing readable original records for the selected task and confirmed subtasks…')
        : request.action === 'commit' ? translated('正在保存并核验本地 ZIP…', 'Saving and verifying the local ZIP…') : request.action === 'result' ? translated('正在核对同一次本地保存结果…', 'Checking the result of the same local save request…') : translated('正在采集已冻结来源；尚未提交本地保存。', 'Collecting frozen sources. Local saving has not been submitted.'));
      count++;
      try {
        const response = await this.rpc(request, signal, request.action === 'commit' ? () => {
          // 在实际派发前才记录 committing；排队时前一条 release 可能已释放该快照。
          if (intent.phase === 'released') throw Object.assign(new Error('export-released'), { code: 'exportExpired' });
          intent.phase = 'committing'; this.persist();
        } : undefined);
        if (!valid()) return;
        const result = validateExportResult(response, binding, intent, known);
        // result 只查询已接受的提交，不能把未确认结论转换成一次新的保存授权。
        if (request.action === 'result') requireValid(result.save.status !== 'notRequested');
        if (result.exportId) intent.exportId = result.exportId;
        if (result.status !== 'unknown') this.known.set(intent.createRequestId, { filename: result.filename ?? known?.filename ?? null,
          totalBytes: result.totalBytes ?? known?.totalBytes ?? null, manifest: result.manifest ?? known?.manifest ?? null });
        failures = 0;
        if (result.save.status === 'verified') {
          intent.phase = 'verified'; this.persist();
          const manifest = result.manifest ?? known?.manifest ?? null;
          const report = translated(
            `本地导出已校验：${result.save.path}（${result.save.bytes.toLocaleString('zh-CN')} 字节，SHA-256 ${result.save.sha256}）。${exportScopeMessage(manifest)}`,
            `Local export verified: ${result.save.path} (${result.save.bytes.toLocaleString('en-US')} bytes, SHA-256 ${result.save.sha256}). ${exportScopeMessage(manifest, 'en')}`,
          );
          this.known.set(intent.createRequestId, { filename: result.filename, totalBytes: result.totalBytes, manifest: result.manifest ?? known?.manifest ?? null, report });
          this.feedback(false, report); this.release(binding, intent); return;
        }
        if (result.save.status === 'failed') {
          intent.phase = 'failed'; this.persist();
          const reason = {
            destinationUnavailable: translated('保存目录不可用', 'the save directory is unavailable'),
            permissionDenied: translated('没有保存权限', 'permission to save was denied'),
            resourceLimit: translated('资源不足', 'resources are insufficient'),
            writeFailed: translated('写入未完成', 'writing did not complete'),
          }[result.save.code];
          this.feedback(false, translated(`本次保存未发布最终文件：${reason.zh}。再次点击可重新采集并导出。`,
            `This save did not publish a final file: ${reason.en}. Click again to collect and export a new snapshot.`)); this.release(binding, intent); return;
        }
        if (result.save.status === 'pending') intent.phase = 'pending';
        else if (result.save.status === 'unknown' || result.status === 'unknown') {
          intent.phase = savedPhase(intent.phase) ? 'saveUnknown' : 'createUnknown'; this.persist();
          if (explicitRecovery && request.action === 'commit') { await this.sleep(1000, signal); continue; }
          this.feedback(false, translated('导出结果未确认，文件可能已保存；再次点击将恢复或核对同一次请求，请勿据此判断文件不存在。', 'The export result is unconfirmed; the file may already be saved. Click again to recover or verify the same request. This does not mean the file is missing.')); return;
        } else if (result.status === 'failed' || result.status === 'released') {
          intent.phase = result.status; this.persist();
          this.feedback(false, result.status === 'released' ? translated('上次未提交的导出快照已释放；再次点击可采集新的快照。', 'The previous snapshot was released before saving. Click again to collect a new snapshot.')
            : translated('未能取得可导出的原始记录；尚未提交本地保存。再次点击可重新采集。', 'No exportable original records could be collected. Local saving has not been submitted. Click again to retry collection.')); return;
        } else if (result.status === 'ready') intent.phase = 'ready';
        else intent.phase = 'collecting';
        this.persist();
        if (intent.phase !== 'ready') await this.sleep(1000, signal);
      } catch (failure) {
        if (!valid()) return;
        const code = failure && typeof failure === 'object' && 'code' in failure ? failure.code : null;
        // 服务只在接受 commit 键之前返回 exportExpired；已接受的保存返回持久化结论。
        if (code === 'exportExpired') {
          intent.phase = 'released'; this.persist();
          this.feedback(false, translated('上次未提交的导出快照已释放；再次点击可采集新的快照。', 'The previous snapshot was released before saving. Click again to collect a new snapshot.')); return;
        }
        intent.phase = savedPhase(intent.phase) ? 'saveUnknown' : 'createUnknown'; this.persist();
        if (failure instanceof ExportResponseError || code === 'identityRequired' || code === 'idempotencyConflict') {
          this.feedback(false, code === 'identityRequired' ? translated('观察会话已失效；请重新打开本任务的执行轨迹，再核对原导出请求。', 'The observation session has expired. Reopen this task’s execution trace, then verify the original export request.')
            : translated('导出响应无法核对，结果未确认；再次点击将核对同一次请求。', 'The export response could not be verified, and the result is unconfirmed. Click again to check the same request.')); return;
        }
        this.feedback(true, translated('本地请求未返回确定结果，正在等待核对同一次导出；不会创建另一份保存请求。', 'The local request returned no definitive result. Waiting to verify the same export without creating another save request.'));
        await this.sleep([1000, 2000, 5000, 10000][Math.min(failures++, 3)], signal);
      }
    }
  }
}
