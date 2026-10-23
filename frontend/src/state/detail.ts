import type { EventDetail } from '../../../contracts/types';

/** Refresh at least the already displayed byte window before replacing its DOM text. */
export async function readDetailWindow(read: (cursor: string | null) => Promise<EventDetail>, byteLength: number): Promise<EventDetail> {
  let result = await read(null);
  const chunk = result.chunks[0];
  if (!chunk) return result;
  if (chunk.byteOffset !== 0) throw new Error('详情起始分块不连续，已保留当前已加载内容。');
  let text = chunk.text, bytes = new TextEncoder().encode(text).length, cursor = chunk.nextCursor;
  const seen = new Set<string>();
  let last = chunk;
  while (cursor && bytes < byteLength) {
    if (seen.has(cursor) || seen.size >= 1024) throw new Error('详情分块不连续，已保留当前已加载内容。');
    seen.add(cursor);
    const page = await read(cursor), next = page.chunks[0];
    if (!next || page.eventId !== result.eventId || page.generation !== result.generation || page.eventRevision !== result.eventRevision
      || next.part !== chunk.part || next.byteOffset !== bytes) throw new Error('详情记录已变化，已保留当前已加载内容并等待补读。');
    const added = new TextEncoder().encode(next.text).length;
    if (!added && next.nextCursor) throw new Error('详情分块没有继续推进，已保留当前已加载内容。');
    text += next.text; bytes += added; cursor = next.nextCursor; last = next;
  }
  return { ...result, chunks: [{ ...last, text, byteOffset: 0 }] };
}
