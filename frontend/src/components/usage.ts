/** 用量仅展示来源快照；轮次/请求与任务累计独立，缓存不重复相加。 */
import type { UsageMetric, UsageSnapshot } from '../../../contracts/types';

type Language = 'zh' | 'en';

function count(value: UsageMetric | undefined, language: Language, compact = false) {
  if (value?.availability !== 'recorded') return '—';
  return new Intl.NumberFormat(language === 'en' ? 'en-US' : 'zh-CN', compact
    ? { notation: 'compact', compactDisplay: 'short', maximumFractionDigits: 1 }
    : {}).format(value.value);
}

/** 只选当前作用域的最新快照；跨 session 保留提示，不把多个范围求和。 */
export function usagePresentation(values: UsageSnapshot[] | undefined, language: Language = 'zh', turn = false) {
  const scoped = (values ?? []).filter(item => item.scope === (turn ? 'turn' : 'request'));
  const usage = scoped.reduce<UsageSnapshot | null>((last, item) => !last || item.order > last.order ? item : last, null);
  const available = usage != null && [usage.input, usage.output, usage.cachedInput].some(value => value.availability === 'recorded');
  const en = language === 'en';
  if (!usage) return {
    text: turn ? en ? 'Usage not recorded' : '用量未记录' : '—',
    title: en ? turn ? 'No turn usage was recorded.' : 'No independent model usage was recorded for this entry. Tool output size does not measure model usage.'
      : turn ? '此轮未记录用量。' : '本条目未记录独立模型用量；工具输出长度不能换算成模型消耗。', available: false,
  };
  const multiple = new Set(scoped.map(item => item.epoch)).size > 1;
  const input = count(usage.input, language, true), output = count(usage.output, language, true);
  const cached = count(usage.cachedInput, language, true);
  return {
    text: `${turn ? `${en ? 'In' : '入'} ${input} · ${en ? 'Out' : '出'} ${output} · ${en ? 'Cache' : '缓'} ${cached}` : `${input} / ${output}`}${multiple ? ' *' : ''}`,
    title: [en ? 'Tokens' : 'Token 用量', `${en ? 'Input' : '输入'}: ${count(usage.input, language)}`,
      `${en ? 'Output' : '输出'}: ${count(usage.output, language)}`, `${en ? 'Cached input' : '缓存输入'}: ${count(usage.cachedInput, language)}`,
      usage.cacheSemantics === 'includedInInput' ? en ? 'Cached tokens are included in input.' : '缓存包含在输入中，不重复相加。' : '',
      en ? 'Recorded usage; this does not measure current context occupancy.' : '已记录消耗，不代表当前上下文占用。',
      multiple ? en ? 'Multiple sessions: showing the latest scope, without summing.' : '多个 session：展示最新范围，彼此未求和。' : '', usage.coverage].filter(Boolean).join('\n'),
    available,
  };
}
