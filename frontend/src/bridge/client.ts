import { App } from '@modelcontextprotocol/ext-apps';
import type { OpenResult } from '../../../contracts/types';
import { HostCallError, withDeadline } from './deadline.mjs';

const PRIVATE_RESULT = 'codex-display/result';

export function readPrivateResult<T>(result: { isError?: boolean; _meta?: Record<string, unknown> }): T {
  const data = result._meta?.[PRIVATE_RESULT];
  if (!data || typeof data !== 'object' || Array.isArray(data))
    throw new HostCallError('invalid-envelope', '本地响应缺少应用私有结果。');
  if (result.isError) {
    const failure = data as { code?: unknown; message?: unknown };
    // Service errors contain fixed public messages. Never read model-visible content.
    const message = typeof failure.message === 'string' && failure.message.length <= 500
      ? failure.message : '本地读取未完成，请重试；已有记录会保留。';
    throw new HostCallError(typeof failure.code === 'string' ? failure.code : 'isError', message);
  }
  return data as T;
}

export function isOpenResult(data: unknown): data is OpenResult {
  if (!data || typeof data !== 'object') return false;
  const value = data as OpenResult;
  return typeof value.viewSessionId === 'string' && typeof value.sourceInstanceId === 'string'
    && !!value.identity && (value.identity.state === 'selectionRequired' || value.identity.state === 'verified');
}

/** Public app SDK only. This client never sends chat messages or model context. */
export class TrajectoryClient {
  readonly app = new App({ name: 'Codex Display', version: '0.1.0' },
    { availableDisplayModes: ['fullscreen', 'inline'] }, { autoResize: false });
  private connection: Promise<Record<string, unknown>> | null = null;
  private context: Record<string, unknown> = {};
  constructor(onOpen: (result: OpenResult) => void, onContext: (value: Record<string, unknown>) => void,
    onError: (error: Error) => void) {
    this.app.ontoolresult = result => {
      const candidate = result._meta?.[PRIVATE_RESULT];
      if (!result.isError && isOpenResult(candidate)) { onOpen(candidate); return; }
      // Ordinary RPC failures belong to the caller's selection token. Only an
      // opening resource result can report an unbound initialization failure.
      const ui = result._meta?.ui;
      if (ui && typeof ui === 'object' && 'resourceUri' in ui) {
        try { readPrivateResult(result); }
        catch (error) { onError(error as Error); }
      }
    };
    this.app.onhostcontextchanged = next => {
      this.context = { ...this.context, ...next };
      onContext(this.context);
    };
  }
  connect() {
    this.connection ??= withDeadline((signal: AbortSignal) => this.app.connect(undefined,
      { timeout: 12000, maxTotalTimeout: 12000, signal })).then(() => {
      this.context = { ...(this.app.getHostContext() || {}) };
      return this.context;
    });
    return this.connection;
  }
  async call<T>(name: string, args: Record<string, unknown>, options: { signal?: AbortSignal } = {}): Promise<T> {
    options.signal?.throwIfAborted();
    await this.connect();
    options.signal?.throwIfAborted();
    const result = await withDeadline((deadline: AbortSignal) => {
      const signal = options.signal ? AbortSignal.any([deadline, options.signal]) : deadline;
      signal.throwIfAborted();
      return this.app.callServerTool({ name, arguments: args },
        { timeout: 12000, maxTotalTimeout: 12000, resetTimeoutOnProgress: false, signal });
    });
    return readPrivateResult<T>(result);
  }
  async close() { await this.app.close(); }
}
