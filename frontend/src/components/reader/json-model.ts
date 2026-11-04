/** JSON 阅读索引只接受完整正文；保留源码区间、重复字段和数字字面量，禁止重序列化替代原文。 */
import { detectReader } from './preview-model.ts';
import type { ReaderFormat } from '../../../../contracts/types';
export type JsonKind = 'object' | 'array' | 'string' | 'number' | 'boolean' | 'null';
export type JsonNode = {
  id: string;
  path: string;
  key: string;
  kind: JsonKind;
  start: number;
  end: number;
  depth: number;
  parent: string | null;
  index: number;
  value?: string;
  children?: JsonNode[];
};
export type JsonDocument = { text: string; root: JsonNode; nodes: Map<string, JsonNode>; duplicateKeys: boolean };
export type JsonParseResult = { document: JsonDocument; error?: never } | { document?: never; error: 'syntax' | 'limit' };
const pointer = (key: string) => key.replace(/~/g, '~0').replace(/\//g, '~1');

/** 资源上限导致回退原文，而不是修补JSON；字符串仅在此解码一次；字段值复制取源码，正文复制取已解码值。 */
export function parseJsonDocument(text: string): JsonParseResult {
  if (text.length > 8_000_000) return { error: 'limit' };
  let offset = 0, count = 0, duplicateKeys = false;
  const nodes = new Map<string, JsonNode>();
  const whitespace = () => { while (/[ \t\r\n]/.test(text[offset] ?? '\0')) offset++; };
  function readString(): string {
    const start = offset++;
    if (text[start] !== '"') throw new Error('syntax');
    while (offset < text.length) {
      const c = text[offset++];
      if (c === '"') return JSON.parse(text.slice(start, offset));
      if (c === '\\') offset++;
      else if (c.charCodeAt(0) < 32) throw new Error('syntax');
    }
    throw new Error('syntax');
  }
  function value(path: string, id: string, key: string, depth: number, parent: string | null, index: number): JsonNode {
    if (depth > 128 || ++count > 200_000) throw new Error('limit');
    whitespace();
    const start = offset, c = text[offset];
    const node: JsonNode = { id, path, key, kind: 'null', start, end: start, depth, parent, index };
    nodes.set(id, node);
    if (c === '{' || c === '[') {
      node.kind = c === '{' ? 'object' : 'array'; node.children = []; offset++; whitespace();
      const close = c === '{' ? '}' : ']';
      const occurrences = new Map<string, number>();
      if (text[offset] !== close) {
        for (;;) {
          whitespace();
          const childIndex = node.children.length;
          const childKey = c === '{' ? readString() : String(childIndex);
          if (c === '{') { whitespace(); if (text[offset++] !== ':') throw new Error('syntax'); }
          const occurrence = occurrences.get(childKey) ?? 0;
          occurrences.set(childKey, occurrence + 1);
          if (occurrence) duplicateKeys = true;
          const childPath = `${path}/${pointer(childKey)}`;
          // 对象以字段名及同名出现次序识别，前面新增其他字段不会丢失折叠；数组没有可靠身份，只保留索引。
          const childId = c === '{' ? `${id}/key:${pointer(childKey)}:${occurrence}` : `${id}/index:${childIndex}`;
          node.children.push(value(childPath, childId, childKey, depth + 1, id, childIndex));
          whitespace();
          if (text[offset] === close) break;
          if (text[offset++] !== ',') throw new Error('syntax');
        }
      }
      if (text[offset++] !== close) throw new Error('syntax');
    } else if (c === '"') {
      node.kind = 'string'; node.value = readString();
    } else {
      const match = /^(?:-?(?:0|[1-9]\d*)(?:\.\d+)?(?:[eE][+-]?\d+)?|true|false|null)/.exec(text.slice(offset));
      if (!match) throw new Error('syntax');
      offset += match[0].length;
      node.kind = match[0] === 'null' ? 'null' : /^(true|false)$/.test(match[0]) ? 'boolean' : 'number';
      node.value = match[0];
    }
    node.end = offset;
    return node;
  }
  try {
    const root = value('', '$', '$', 0, null, 0);
    whitespace();
    if (offset !== text.length) return { error: 'syntax' };
    return { document: { text, root, nodes, duplicateKeys } };
  } catch (error) {
    return { error: error instanceof Error && error.message === 'limit' ? 'limit' : 'syntax' };
  }
}

export function rawJsonValue(document: JsonDocument, node: JsonNode): string {
  return document.text.slice(node.start, node.end);
}
export function jsonAncestors(document: JsonDocument, node: JsonNode): JsonNode[] {
  const ancestors: JsonNode[] = [];
  let current = node;
  while (current.parent) {
    const parent = document.nodes.get(current.parent);
    if (!parent) break;
    ancestors.unshift(parent); current = parent;
  }
  return ancestors;
}
/** 命中限制只限制结果列表；每个已解析字段/值均参与搜索，返回真实总数。 */
export function searchJson(document: JsonDocument, query: string, limit = 200): { matches: JsonNode[]; total: number } {
  const normalized = query.trim().toLocaleLowerCase();
  if (!normalized) return { matches: [], total: 0 };
  const matches: JsonNode[] = [];
  let total = 0;
  for (const node of document.nodes.values()) {
    if ((node.parent !== null && node.key.toLocaleLowerCase().includes(normalized)) || node.value?.toLocaleLowerCase().includes(normalized)) {
      total++;
      if (matches.length < limit) matches.push(node);
    }
  }
  return { matches, total };
}

export function matchingField<T extends { path: string }>(fields: T[] | undefined, path: string): T | undefined {
  const segments = path.split('/');
  return fields?.find(field => field.path === path) ?? fields?.find(field => {
    const expected = field.path.split('/');
    return expected.length === segments.length && expected.every((part, index) => part === segments[index] || part === '*' && /^\d+$/.test(segments[index]));
  });
}

export type JsonRevealState = { expanded?: Record<string, boolean>; limits?: Record<string, number>; offsets?: Record<string, number> };
/** 定位只打开目标父链，并移动有界值窗口；保留无关的用户折叠状态，不为末尾命中挂载整个数组。 */
export function revealJsonNode(document: JsonDocument, node: JsonNode, state: JsonRevealState, page = 100) {
  const expanded = { ...state.expanded }, limits = { ...state.limits }, offsets = { ...state.offsets };
  for (const item of [...jsonAncestors(document, node), node]) {
    expanded[item.id] = true;
    if (item.parent) {
      const start = offsets[item.parent] ?? 0, limit = limits[item.parent] ?? page;
      if (item.index < start || item.index >= start + limit) {
        offsets[item.parent] = Math.floor(item.index / page) * page;
        limits[item.parent] = page;
      }
    }
  }
  return { expanded, limits, offsets, selected: node.id };
}

/** 字段定义优先于语法识别；text 仅说明文本载体，字段名和转义后的源码均不参与语言猜测。 */
export function jsonStringPresentation(node: JsonNode, field?: { format?: string; language?: string }) {
  const text = node.kind === 'string' ? node.value ?? '' : '';
  const formats = ['text', 'markdown', 'code', 'json', 'diff'];
  const metadata = field?.format && formats.includes(field.format)
    ? { format: field.format as ReaderFormat, language: field.language, origin: 'field-definition' } : undefined;
  const detected = node.kind === 'string' ? detectReader(text, metadata) : 'text';
  // 内层 JSON 作为字符串正文高亮，绝不递归 JSON.parse 或替换字面反斜杠转义。
  return {
    expandable: node.kind === 'string' && (node.parent === null || text.length > 120 || /[\r\n]/.test(text) || detected !== 'text'),
    mode: (detected === 'markdown' ? 'markdown' : detected === 'text' ? 'text' : 'code') as 'text' | 'markdown' | 'code',
    language: detected === 'diff' ? 'diff' : detected === 'json' ? 'json' : field?.language,
  };
}
