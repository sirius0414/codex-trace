import { App } from '@modelcontextprotocol/ext-apps';
import { HostCallError, withDeadline } from './deadline.mjs';

export const APP_VERSION = '0.1.0-m0.5';

export type Activation = {
  kind: 'activation'; activationId: string; serverInstanceId: string;
  entrypoint?: 'global' | 'thread'; serverVersion?: string;
  candidateThreadId: string | null; identityReason: string; metadataKeys: string[];
  downloadExpectedSha256: string; downloadExpectedBytes: number;
};

/** M0 宿主接入诊断适配器；正式轨迹使用 client.ts，二者结果信封不可混用。 */
export class HostAdapter {
  readonly instanceId = crypto.randomUUID();
  readonly app = new App({ name: 'Codex Trace', version: APP_VERSION },
    { availableDisplayModes: ['fullscreen', 'inline'] }, { autoResize: false });
  private context: Record<string, unknown> = {};
  private connection: Promise<Record<string, unknown>> | null = null;
  constructor(onActivation: (activation: Activation) => void, onContext: (value: Record<string, unknown>) => void) {
    // 在握手前注册回调，宿主可能在连接完成前立即送达打开结果。
    this.app.ontoolresult = result => {
      const data = result.structuredContent as Record<string, unknown> | undefined;
      if (data?.kind === 'activation' && typeof data.activationId === 'string') onActivation(data as Activation);
    };
    this.app.onhostcontextchanged = next => {
      this.context = { ...this.context, ...next };
      onContext(this.context);
    };
  }
  connect() {
    // 诊断和普通请求共用同一握手 Promise，打开结果可在握手收尾时先行到达。
    this.connection ??= withDeadline((signal: AbortSignal) => this.app.connect(undefined, { timeout: 12000, maxTotalTimeout: 12000, signal })).then(() => {
      this.context = { ...(this.app.getHostContext() || {}) };
      return this.context;
    });
    return this.connection;
  }
  private async request(name: string, args: Record<string, unknown>) {
    await this.connect();
    const result = await withDeadline((signal: AbortSignal) => this.app.callServerTool({ name, arguments: args },
      { timeout: 12000, maxTotalTimeout: 12000, resetTimeoutOnProgress: false, signal }));
    if (result.isError) throw new HostCallError('isError', '本地工具读取失败；服务端拒绝或未能完成请求。');
    return result;
  }
  async call<T>(name: string, args: Record<string, unknown>): Promise<T> {
    const result = await this.request(name, args);
    if (!result.structuredContent) throw new HostCallError('invalid-envelope', '本地工具读取失败；响应缺少结构化结果。');
    return result.structuredContent as T;
  }
  async privateResult<T>(name: string, args: Record<string, unknown>): Promise<T> {
    const result = await this.request(name, args);
    const data = result._meta?.['codex-trace/result'];
    if (!data || typeof data !== 'object' || Array.isArray(data))
      throw new HostCallError('invalid-envelope', '本地工具响应缺少应用私有结果，导出结果尚未确认。');
    return data as T;
  }
  async observe(activation: Activation) {
    await this.connect();
    // 诊断只采字段名、显示模式和能力键，不提交完整的宿主上下文对象。
    return this.call('probe_host_observation', { activationId: activation.activationId, uiInstanceId: this.instanceId,
      contextKeys: Object.keys(this.context).sort(), displayMode: this.context.displayMode ?? null,
      hostCapabilityKeys: Object.keys(this.app.getHostCapabilities() || {}).sort() });
  }
  async diagnostic(activationId: string, phase: string, values: Record<string, unknown> = {}) {
    return this.call('probe_diagnostic_event', { activationId, uiInstanceId: this.instanceId, phase, ...values });
  }
  contextKeys() { return Object.keys(this.context).sort(); }
  capabilityKeys() { return Object.keys(this.app.getHostCapabilities() || {}).sort(); }
  async close() { await this.app.close(); }
}
