export class DownloadError extends Error {
  constructor(code, message) { super(message); this.name = 'DownloadError'; this.code = code; }
}

export async function sha256(bytes) {
  if (!globalThis.crypto?.subtle) throw new DownloadError('digest-unavailable', '当前容器缺少 SHA-256 校验能力，尚未请求保存。');
  try {
    return [...new Uint8Array(await crypto.subtle.digest('SHA-256', bytes))].map(v => v.toString(16).padStart(2, '0')).join('');
  } catch { throw new DownloadError('digest-failed', '本地字节校验失败，尚未请求保存。'); }
}

/** M0 合成下载探针：按偏移及分块/整包哈希核验有界字节，读取成功不代表已请求保存。 */
export async function prepareDownload(call, expected, progress, isCurrent = () => true) {
  const assertCurrent = () => {
    if (!isCurrent()) throw new DownloadError('cancelled', '任务选择已变化，已取消旧下载。');
  };
  assertCurrent();
  const expectedBytes = expected?.downloadExpectedBytes;
  const expectedHash = expected?.downloadExpectedSha256;
  if (!Number.isSafeInteger(expectedBytes) || expectedBytes < 0 || expectedBytes > 1024 * 1024 ||
      typeof expectedHash !== 'string' || !/^[a-f0-9]{64}$/.test(expectedHash))
    throw new DownloadError('invalid-manifest', '下载验证信息无效，尚未读取或请求保存。');
  const chunks = []; let offset = 0; let result;
  do {
    assertCurrent();
    progress('download-start');
    assertCurrent();
    result = await call({ offset });
    assertCurrent();
    progress('chunk-received');
    if (!result || !Number.isSafeInteger(result.offset) || result.offset !== offset ||
        !Number.isSafeInteger(result.nextOffset) || !Number.isSafeInteger(result.totalBytes) ||
        result.totalBytes < 0 || result.totalBytes > 1024 * 1024 ||
        typeof result.data !== 'string' || result.data.length > 1400000 || typeof result.done !== 'boolean' ||
        typeof result.sha256 !== 'string' || !/^[a-f0-9]{64}$/.test(result.sha256) ||
        typeof result.chunkSha256 !== 'string' || !/^[a-f0-9]{64}$/.test(result.chunkSha256))
      throw new DownloadError('invalid-chunk', '下载字节块格式无效，尚未请求保存。');
    if (result.totalBytes !== expectedBytes || result.sha256 !== expectedHash)
      throw new DownloadError('checksum-mismatch', '下载长度或完整文件校验信息发生变化，尚未请求保存。');
    if (result.data.length % 4 !== 0 || !/^[A-Za-z0-9+/]*={0,2}$/.test(result.data))
      throw new DownloadError('invalid-chunk', '下载字节编码无效，尚未请求保存。');
    let bytes;
    try {
      const decoded = atob(result.data);
      if (btoa(decoded) !== result.data) throw new Error();
      bytes = Uint8Array.from(decoded, c => c.charCodeAt(0));
    }
    catch { throw new DownloadError('invalid-chunk', '下载字节编码无效，尚未请求保存。'); }
    if (result.nextOffset !== offset + bytes.length || result.nextOffset > result.totalBytes ||
        result.done !== (result.nextOffset === result.totalBytes) ||
        (!result.done && bytes.length === 0)) throw new DownloadError('invalid-chunk', '下载字节位置不一致，尚未请求保存。');
    if (await sha256(bytes) !== result.chunkSha256) throw new DownloadError('checksum-mismatch', '下载字节块校验失败，尚未请求保存。');
    assertCurrent();
    // 只有通过校验的块才前移偏移，零进展且未结束的响应已在上面拒绝。
    chunks.push(bytes); offset = result.nextOffset; progress('chunk-verified');
  } while (!result.done);
  const bytes = new Uint8Array(offset); let start = 0;
  for (const chunk of chunks) { bytes.set(chunk, start); start += chunk.length; }
  const hash = await sha256(bytes);
  if (offset !== expectedBytes || hash !== expectedHash)
    throw new DownloadError('checksum-mismatch', '下载长度或完整文件校验失败，尚未请求保存。');
  assertCurrent();
  return { bytes, hash, filename: 'codex-display-m0-local-download.zip' };
}
