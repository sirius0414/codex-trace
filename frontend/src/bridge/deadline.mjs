/** Deadline also covers hosts whose progress messages would otherwise extend RPCs. */
export class HostCallError extends Error {
  constructor(code, message) { super(message); this.name = 'HostCallError'; this.code = code; }
}

export async function withDeadline(operation, timeoutMs = 12000) {
  if (!Number.isFinite(timeoutMs) || timeoutMs <= 0 || timeoutMs > 2147483647)
    throw new HostCallError('invalid-timeout', '本地工具读取的等待时间配置无效。');
  const controller = new AbortController();
  const timeoutError = new HostCallError('timeout', '本地工具读取超时；宿主通道未在限定时间内返回。');
  let timer;
  const deadline = new Promise((_, reject) => {
    timer = setTimeout(() => {
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
