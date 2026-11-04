/** 界面偏好独立于任务阅读状态；只持久化经过校验的显示选项，不保存任务内容。 */
export type Language = 'zh' | 'en';
export type UIStyle = 'classic' | 'soft' | 'terminal';
export type UIPreferences = { language: Language; uiStyle: UIStyle; showSpanSummary: boolean; showTimelineCursors: boolean; readerFontSize: number; readerWrap: boolean; readerLineNumbers: boolean };
type PreferenceStorage = Pick<Storage, 'getItem' | 'setItem'>;

export const UI_PREFERENCES_KEY = 'codex-trace.ui-preferences.v1';
export const uiStyles: readonly UIStyle[] = ['classic', 'soft', 'terminal'];
export const readerFontSizes = [13, 14, 15, 16, 18, 20] as const;
export const readerDefaults = { readerFontSize: 15, readerWrap: true, readerLineNumbers: false };

// 仅翻译客户端自己的固定异常；来源说明和任意后端错误继续按原文显示。
const clientErrors: Record<string, string> = {
  '本地工具读取的等待时间配置无效。': 'The local tool read timeout configuration is invalid.',
  '本地工具读取超时；宿主通道未在限定时间内返回。': 'The local tool read timed out. The host did not respond within the time limit.',
  '本地工具读取失败；宿主返回了协议或连接错误。': 'The local tool read failed due to a host protocol or connection error.',
  '本地响应缺少应用私有结果。': 'The local response is missing the application-private result.',
  '本地读取未完成，请重试；已有记录会保留。': 'The local read did not complete. Please retry; existing records are retained.',
  '详情起始分块不连续，已保留当前已加载内容。': 'The initial detail chunk is not contiguous. Loaded content is retained.',
  '详情分块不连续，已保留当前已加载内容。': 'The detail chunks are not contiguous. Loaded content is retained.',
  '详情记录已变化，已保留当前已加载内容并等待补读。': 'The detail record changed. Loaded content is retained while waiting to read the update.',
  '详情分块没有继续推进，已保留当前已加载内容。': 'The detail chunks made no progress. Loaded content is retained.',
  '增量记录批次不连续，保留已有内容并重试。': 'Incremental record batches are not contiguous. Existing content is retained; retry reading.',
  '宿主观察会话尚未建立。': 'The host observation session has not been established.',
};
export function localizeClientError(message: string, language: Language): string {
  return language === 'en' && Object.hasOwn(clientErrors, message) ? clientErrors[message] : message;
}

/** 首次使用跟随浏览器语言；存储不可用或旧值损坏时，仍能在当前页面切换。 */
export function readUIPreferences(storage: PreferenceStorage | null, languages: readonly string[] = []): UIPreferences {
  const defaults: UIPreferences = { language: languages[0]?.toLowerCase().startsWith('en') ? 'en' : 'zh', uiStyle: 'classic', showSpanSummary: false, showTimelineCursors: false, ...readerDefaults };
  try {
    const saved: unknown = JSON.parse(storage?.getItem(UI_PREFERENCES_KEY) ?? 'null');
    if (!saved || typeof saved !== 'object' || Array.isArray(saved)) return defaults;
    const value = saved as Partial<UIPreferences>;
    return {
      language: value.language === 'zh' || value.language === 'en' ? value.language : defaults.language,
      uiStyle: uiStyles.includes(value.uiStyle as UIStyle) ? value.uiStyle! : defaults.uiStyle,
      showSpanSummary: typeof value.showSpanSummary === 'boolean' ? value.showSpanSummary : defaults.showSpanSummary,
      showTimelineCursors: typeof value.showTimelineCursors === 'boolean' ? value.showTimelineCursors : defaults.showTimelineCursors,
      readerFontSize: readerFontSizes.some(size => size === value.readerFontSize) ? value.readerFontSize! : defaults.readerFontSize,
      readerWrap: typeof value.readerWrap === 'boolean' ? value.readerWrap : defaults.readerWrap,
      readerLineNumbers: typeof value.readerLineNumbers === 'boolean' ? value.readerLineNumbers : defaults.readerLineNumbers,
    };
  } catch { return defaults; }
}

export function saveUIPreferences(storage: PreferenceStorage | null, value: UIPreferences): void {
  try { storage?.setItem(UI_PREFERENCES_KEY, JSON.stringify({ language: value.language, uiStyle: value.uiStyle, showSpanSummary: value.showSpanSummary, showTimelineCursors: value.showTimelineCursors, readerFontSize: value.readerFontSize, readerWrap: value.readerWrap, readerLineNumbers: value.readerLineNumbers })); }
  catch { /* 嵌入式宿主可能禁用持久存储；偏好仍保留在 React 状态中。 */ }
}
