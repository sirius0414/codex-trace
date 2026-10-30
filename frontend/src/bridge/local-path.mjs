/** 本地回执的跨平台语法校验；实际 home 边界、重解析点及文件身份由后端核对。
 * Windows 只接受本地盘符的原生绝对路径，网络共享和设备命名空间不属于导出契约。
 */
const CONTROL = /[\x00-\x1f\x7f]/;
const WINDOWS_FORBIDDEN = /[<>:"/\\|?*]/;
const WINDOWS_DEVICE = /^(?:con|prn|aux|nul|com[1-9¹²³]|lpt[1-9¹²³])(?:[ .]|$)/i;

function components(value, maximum) {
  if (typeof value !== 'string' || value.length > maximum || CONTROL.test(value)) return null;
  let parts;
  if (value.startsWith('/') && !value.includes('\\')) {
    parts = value.slice(1).split('/');
  } else if (/^[A-Za-z]:\\/.test(value)) {
    parts = value.slice(3).split('\\');
    // 禁止 Win32 会重新解释的拼写，避免 ADS、设备名和尾部点/空格绕过回执核对。
    if (parts.some(part => WINDOWS_FORBIDDEN.test(part) || /[ .]$/.test(part) || WINDOWS_DEVICE.test(part))) return null;
  } else return null;
  return parts.some(part => !part || part === '.' || part === '..') ? null : parts;
}

/** 合成探针允许不同本地目录，但回执 basename 必须逐字匹配服务器文件名。 */
export function isLocalOutputPath(value, filename, maximum = 8192) {
  const parts = components(value, maximum);
  return Boolean(parts && parts.at(-1) === filename);
}

/** 正式导出固定在 home 下的 Downloads/Codex Trace，不接受 UI 提供的目的地。 */
export function isExportReceiptPath(value, filename) {
  const parts = components(value, 4096);
  return Boolean(parts && parts.length >= 3 && parts.at(-3) === 'Downloads'
    && parts.at(-2) === 'Codex Trace' && parts.at(-1) === filename);
}
