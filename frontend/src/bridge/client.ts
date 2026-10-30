import { App } from '@modelcontextprotocol/ext-apps';
import type { OpenResult } from '../../../contracts/types';
import { HostCallError, withDeadline } from './deadline.mjs';

const PRIVATE_RESULT = 'codex-trace/result';

/** 正式轨迹只读取 app-only 私有结果，模型可见的 content/structuredContent 不承载任务数据。 */
export function readPrivateResult<T>(result: { isError?: boolean; _meta?: Record<string, unknown> }): T {
  const data = result._meta?.[PRIVATE_RESULT];
  if (!data || typeof data !== 'object' || Array.isArray(data))
    throw new HostCallError('invalid-envelope', '本地响应缺少应用私有结果。');
  if (result.isError) {
    const failure = data as { code?: unknown; message?: unknown };
    // 服务端只返回固定公开错误文案；错误展示也不回退读取模型可见内容。
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

/** 正式界面的宿主边界：只通过公开 App SDK 读工具结果，不写聊天或模型上下文。 */
export class TrajectoryClient {
  readonly app = new App({ name: 'Codex Trace', version: '0.1.0' },
    { availableDisplayModes: ['fullscreen', 'inline'] }, { autoResize: false });
  private connection: Promise<Record<string, unknown>> | null = null;
  private context: Record<string, unknown> = {};
  constructor(onOpen: (result: OpenResult) => void, onContext: (value: Record<string, unknown>) => void,
    onError: (error: Error) => void) {
    this.app.ontoolresult = result => {
      const candidate = result._meta?.[PRIVATE_RESULT];
      if (!result.isError && isOpenResult(candidate)) { onOpen(candidate); return; }
      // 普通 RPC 错误由调用方按选择 token 处理，只有打开资源的失败可作为未绑定初始化错误。
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
    // 复用同一握手 Promise，所有调用等待连接就绪，避免重复注册或抢在握手前发请求。
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
      // 任务切换取消与硬超时共同生效；取消请求后仍由上层 Binding 校验抵挡迟到响应。
      const signal = options.signal ? AbortSignal.any([deadline, options.signal]) : deadline;
      signal.throwIfAborted();
      return this.app.callServerTool({ name, arguments: args },
        { timeout: 12000, maxTotalTimeout: 12000, resetTimeoutOnProgress: false, signal });
    });
    return readPrivateResult<T>(result);
  }
  async close() { await this.app.close(); }
}
