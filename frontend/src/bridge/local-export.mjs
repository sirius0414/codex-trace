const UUID = /^[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}$/i;
const HASH = /^[a-f0-9]{64}$/;

export class LocalExportError extends Error {
  constructor(code, message) { super(message); this.name = 'LocalExportError'; this.code = code; }
}

function assertCurrent(isCurrent) {
  if (!isCurrent()) throw new LocalExportError('cancelled', '任务选择已变化，旧导出结果不会显示到当前任务。');
}

/** M0 合成保存探针：等价任务 ID 的重复校验仍复用原保存意图，不能生成第二个请求。 */
export class LocalExportBinding {
  constructor() { this.clear(); }
  clear() { this.key = null; this.attempt = null; }
  select(activationId, uiInstanceId, threadId) {
    const key = JSON.stringify([activationId ?? null, uiInstanceId ?? null, threadId.trim().toLowerCase()]);
    if (this.key === key) return false;
    this.key = key;
    this.attempt = null;
    return true;
  }
  getAttempt(activation, uiInstanceId, threadId) {
    this.select(activation?.activationId, uiInstanceId, threadId);
    this.attempt ??= new LocalExportAttempt(activation, uiInstanceId);
    return this.attempt;
  }
}

/** 单次幂等保存尝试固定请求 ID，并发点击共用进行中的 Promise，重试仍核对原请求。 */
export class LocalExportAttempt {
  constructor(activation, uiInstanceId, requestId = crypto.randomUUID()) {
    if (!activation || typeof activation.activationId !== 'string' || !UUID.test(activation.activationId) ||
        typeof uiInstanceId !== 'string' || !UUID.test(uiInstanceId) || typeof requestId !== 'string' || !UUID.test(requestId) ||
        !Number.isSafeInteger(activation.downloadExpectedBytes) || activation.downloadExpectedBytes < 0 ||
        typeof activation.downloadExpectedSha256 !== 'string' || !HASH.test(activation.downloadExpectedSha256))
      throw new LocalExportError('invalid-manifest', '导出验证信息无效，尚未提交本次导出。');
    this.request = Object.freeze({ activationId: activation.activationId, uiInstanceId, requestId });
    this.expectedBytes = activation.downloadExpectedBytes;
    this.expectedHash = activation.downloadExpectedSha256;
    this.inFlight = null;
  }

  run(call, isCurrent = () => true) {
    if (!isCurrent()) return Promise.reject(new LocalExportError('cancelled', '任务选择已变化，旧导出结果不会显示到当前任务。'));
    if (this.inFlight) return this.inFlight;
    const pending = this.perform(call, isCurrent);
    this.inFlight = pending;
    const clear = () => { if (this.inFlight === pending) this.inFlight = null; };
    void pending.then(clear, clear);
    return pending;
  }

  async perform(call, isCurrent) {
    assertCurrent(isCurrent);
    const result = await call(this.request);
    assertCurrent(isCurrent);
    if (!result || result.kind !== 'local-export' || result.requestId !== this.request.requestId ||
        result.verified !== true || result.synthetic !== true ||
        typeof result.filename !== 'string' || !result.filename || result.filename.length > 255 || /[\x00-\x1f\x7f/\\]/.test(result.filename) ||
        typeof result.path !== 'string' || !result.path.startsWith('/') || result.path.length > 8192 || /[\x00-\x1f\x7f]/.test(result.path) ||
        result.path.split('/').some(part => part === '.' || part === '..') || !result.path.endsWith('/' + result.filename) ||
        !Number.isSafeInteger(result.bytes) || result.bytes < 0 || typeof result.sha256 !== 'string' || !HASH.test(result.sha256))
      throw new LocalExportError('invalid-envelope', '本地导出响应无法核对，结果未确认；可重试核对同一次导出。');
    if (result.bytes !== this.expectedBytes || result.sha256 !== this.expectedHash)
      throw new LocalExportError('checksum-mismatch', '本地导出长度或校验值不符，结果未确认；可重试核对同一次导出。');
    return { kind: 'local-export', requestId: result.requestId, filename: result.filename, path: result.path,
      bytes: result.bytes, sha256: result.sha256, verified: true, synthetic: true };
  }
}

/** 丢失响应或超时无法证明文件未落盘，因此只报告未确认，不报告保存失败。 */
export function exportFailure(error) {
  const code = ['timeout', 'rpc-error', 'isError', 'invalid-envelope', 'invalid-manifest', 'checksum-mismatch'].includes(error?.code)
    ? error.code : 'unknown';
  if (code === 'invalid-manifest') return { code, message: '导出验证信息无效，尚未提交本次导出。' };
  return { code, message: code === 'timeout'
    ? '等待超时，结果未确认；可重试核对同一次导出。'
    : '导出结果未确认；可重试核对同一次导出。' };
}
