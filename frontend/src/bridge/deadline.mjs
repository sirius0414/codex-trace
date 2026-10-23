/** 固定公开错误码供调用方区分传输失败与超时，不传播宿主的任意异常文本。 */
export class HostCallError extends Error {
  constructor(code, message) { super(message); this.name = 'HostCallError'; this.code = code; }
}

/** 设置独立硬截止时间，避免宿主的 progress 消息不断延长 SDK 内部 RPC 超时。 */
export async function withDeadline(operation, timeoutMs = 12000) {
  if (!Number.isFinite(timeoutMs) || timeoutMs <= 0 || timeoutMs > 2147483647)
    throw new HostCallError('invalid-timeout', '本地工具读取的等待时间配置无效。');
  const controller = new AbortController();
  const timeoutError = new HostCallError('timeout', '本地工具读取超时；宿主通道未在限定时间内返回。');
  let timer;
  const deadline = new Promise((_, reject) => {
    timer = setTimeout(() => {
      // 先以明确 timeout 拒绝，再发 abort；避免中止异常抢先掩盖超时原因。
      reject(timeoutError);
      controller.abort();
    }, timeoutMs);
  });
  try { return await Promise.race([Promise.resolve().then(() => operation(controller.signal)), deadline]); }
  catch (error) {
    if (error === timeoutError) throw error;
    if (error?.code === -32001) throw timeoutError;
    throw new HostCallError('rpc-error', '本地工具读取失败；宿主返回了协议或连接错误。');
  }
  finally { clearTimeout(timer); }
}
