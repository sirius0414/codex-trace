/** JSON 阅读排版只插入缩进；键和值均引用源码区间，复制与条目搜索始终使用原文。
 * 长值折叠与显示窗口分离；搜索按源码位置打开父链并移动窗口，不挂载前面所有值。 */
import { jsonAncestors, type JsonDocument, type JsonNode } from './json-model.ts';
export const JSON_READING_LIMITS = { longValue: 600, longLines: 12, preview: 70, rows: 400, characters: 100_000 } as const;
export type JsonReaderState = { expanded?: Record<string, boolean>; rowOffset?: number; sourceOffsets?: Record<string, number> };
export type JsonRow = { node: JsonNode; close?: boolean; folded?: boolean; comma: boolean };
export function isLongJsonValue(document: JsonDocument, node: JsonNode) {
  if (!node.parent || !['string', 'object', 'array'].includes(node.kind)) return false;
  // 字符数与原文行数统一用于三种值，根值始终可读。
  return node.end - node.start > JSON_READING_LIMITS.longValue
    || document.text.slice(node.start, node.end).split('\n', JSON_READING_LIMITS.longLines + 2).length > JSON_READING_LIMITS.longLines;
}
export function jsonRows(document: JsonDocument, state: JsonReaderState): JsonRow[] {
  const rows: JsonRow[] = [];
  function visit(node: JsonNode, comma: boolean) {
    const folded = isLongJsonValue(document, node) && !state.expanded?.[node.id];
    rows.push({ node, folded, comma: folded || !node.children?.length ? comma : false });
    if (!folded && node.children?.length) {
      node.children.forEach((child, index) => visit(child, index < node.children!.length - 1));
      rows.push({ node, close: true, comma });
    }
  }
  visit(document.root, false);
  return rows;
}
export function revealJsonHit(document: JsonDocument, state: JsonReaderState, hit: { start: number; end: number }): JsonReaderState | null {
  // 横跨结构/空白的查询使用原文定位，避免美化排版伪造命中。
  const node = [...document.nodes.values()].find(node =>
    !node.children && node.start <= hit.start && node.end >= hit.end
    || node.keyStart !== undefined && node.keyStart <= hit.start && node.keyEnd! >= hit.end);
  if (!node) return null;
  const expanded = { ...state.expanded };
  for (const item of [...jsonAncestors(document, node), node]) expanded[item.id] = true;
  const next = { ...state, expanded };
  const index = jsonRows(document, next).findIndex(row => row.node.id === node.id);
  return { ...next, rowOffset: index,
    sourceOffsets: { ...state.sourceOffsets, [hit.start < node.start ? `key:${node.id}` : node.id]: Math.max(0, hit.start - (hit.start < node.start ? node.keyStart! : node.start) - 200) } };
}
