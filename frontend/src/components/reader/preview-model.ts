/** Preview 只解释已读正文；来源完整性与分页互不推导，识别不改写任何字符。 */
import type { ContentPart, ReaderMetadata, TraceEvent } from '../../../../contracts/types';
import type { DetailPart } from '../../ui-types';
import { detectCodeLanguage, frontmatterEnd } from './reader-model.ts';
export type ReaderMode = 'auto' | 'text' | 'markdown' | 'code' | 'json' | 'diff';

export function readableComplete(value: DetailPart | undefined) {
  if (!value) return false;
  const bytes = new TextEncoder().encode(value.text).length;
  return !value.nextCursor && (value.readableBytes == null || bytes >= value.readableBytes);
}

/** 来源确定的结构/语言优先；text 仅说明载体，不阻止识别 cat 等输出中的 Markdown/代码。
 * 同一规则可用于 JSON 已解码的字符串；不得按字段名猜语言或反复解码转义。
 */
export function detectReader(text: string, metadata?: ReaderMetadata): Exclude<ReaderMode, 'auto'> {
  if (metadata && metadata.format !== 'text') return metadata.format;
  if (metadata?.language && detectCodeLanguage('', metadata.language).language) return 'code';
  const sample = text.slice(0, 32_768).replace(/\r\n?/g, '\n');
  // 日志的 [INFO] 不构成 JSON；对象/数组仅选择读取尝试，分页及解析失败仍由正文视图兜底。
  if (/^\s*\{/.test(sample) || /^\s*\[\s*(?:[\[\{"\d\-\]]|true\b|false\b|null\b|$)/.test(sample)) return 'json';
  if (/^(?:\*\*\* Begin Patch|diff --git |--- .+\n\+\+\+ )/m.test(sample)) return 'diff';
  if (frontmatterEnd(sample) || /^ {0,3}(?:`{3,}|~{3,})[^\n]*$/m.test(sample)) return 'markdown';
  // 先识别有明确语法的脚本，防止 Python/Bash 的 # 注释误变成 Markdown 标题。
  if (detectCodeLanguage(sample, metadata?.language).language) return 'code';
  if (/^ {0,3}#{1,6}\s+\S/m.test(sample)
    || /\n\s*\|?\s*:?-{3,}:?\s*\|/.test(sample)
    || /^ {0,3}>\s+\S/m.test(sample)
    || (sample.match(/^ {0,3}(?:[-*+] |\d+\. )\S/gm)?.length ?? 0) >= 2
    || /\[[^\]\n]+\]\(https?:\/\/[^)\s]+\)/.test(sample)
    || /\*\*[^*\n]+\*\*/.test(sample)) return 'markdown';
  return 'text';
}
export function previewParts(event: TraceEvent, parts: Partial<Record<ContentPart, DetailPart>>) {
  const available = (part: ContentPart) => Boolean(parts[part]) || event.content.some(ref => ref.part === part);
  const tool = event.kind === 'toolCall' || event.kind === 'toolResult' || Boolean(event.toolName);
  const order: ContentPart[] = event.status?.value === 'failed' ? ['error', 'input', 'output'] : event.kind === 'toolResult' ? ['output', 'input', 'error'] : ['input', 'output', 'error'];
  return order.filter(part => available(part) || tool && part === 'output');
}
export type TextHit = {part: ContentPart; start: number; end: number; before: string; match: string; after: string};
/** 查询仅覆盖本次已读正文。限制结果列表，不限制查找的文本范围；返回总数以避免隐瞒命中。 */
export function searchPreview(parts: Partial<Record<ContentPart, DetailPart>>, query: string, limit = 100) {
  const hits: TextHit[] = [];
  let total = 0;
  if (!query) return { hits, total };
  // 保留原文偏移；Unicode lowercasing 可能改变长度，因此用转义后的正则而非转换全文。
  const escaped = query.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
  const regex = new RegExp(escaped, 'giu');
  for (const part of ['input', 'output', 'error'] as const) {
    const text = parts[part]?.text ?? '';
    for (const match of text.matchAll(regex)) {
      total++;
      if (hits.length < limit) {
        const start = match.index!, end = start + match[0].length;
        hits.push({ part, start, end, before: text.slice(Math.max(0, start - 45), start), match: match[0], after: text.slice(end, end + 70) });
      }
    }
  }
  return { hits, total };
}
