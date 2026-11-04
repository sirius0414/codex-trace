/** 仅解包来源确认的 MCP 文本结果；未知字段、重复键、非文本块或结构化结果保持完整 JSON。 */
import type { ReaderMetadata } from '../../../../contracts/types';
import { parseJsonDocument } from './json-model.ts';
import type { JsonNode } from './json-model.ts';
export function protocolBody(text: string, metadata?: ReaderMetadata): string | null {
  if (metadata?.origin !== 'mcpToolCall.result') return null;
  const document = parseJsonDocument(text).document;
  if (!document || document.duplicateKeys || document.root.kind !== 'object') return null;
  const fields = document.root.children ?? [];
  if (fields.some(node => !['content', 'isError'].includes(node.key))) return null;
  const error = fields.find(node => node.key === 'isError');
  if (error && (error.kind !== 'boolean' || error.value !== 'false')) return null;
  const content = fields.find(node => node.key === 'content');
  if (content?.kind !== 'array' || !content.children?.length) return null;
  const bodies: string[] = [];
  for (const block of content.children) {
    if (block.kind !== 'object' || block.children?.length !== 2) return null;
    const type = block.children.find(node => node.key === 'type');
    const body: JsonNode | undefined = block.children.find(node => node.key === 'text');
    const definition = metadata.fields?.find(field => field.path === body?.path && field.scope === 'protocol' && field.format === 'text');
    if (type?.value !== 'text' || body?.kind !== 'string' || !definition) return null;
    bodies.push(body.value!);
  }
  return bodies.join('\n\n');
}
