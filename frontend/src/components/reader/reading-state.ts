/** 只在当前页面会话内保存阅读偏好/位置；不把正文、查询或工具内容写入宿主持久化 ViewState。 */
import type { JsonReaderState } from './JsonReader';
import type { RichReaderState } from './RichReader';
import type { ReaderMode, TextHit } from './preview-model';
export type BlockReadingState = { collapsed?: boolean; source?: boolean; mode?: ReaderMode; language?: string; json?: JsonReaderState; rich?: RichReaderState; sourceLimit?: number; codeLines?: number; commandLines?: number; patchFiles?: Record<string, {collapsed?:boolean;lines?:number}> };
export type ReadingState = { expanded: boolean; query: string; blocks: Record<string, BlockReadingState>; hit?: TextHit };
const states = new Map<string, ReadingState>();
const positions = new Map<string, number>();
export const initialReadingState = (): ReadingState => ({ expanded: false, query: '', blocks: {} });
export function readReadingState(key: string) { return states.get(key) ?? initialReadingState(); }
export function saveReadingState(key: string, state: ReadingState) {
  states.delete(key); states.set(key, state);
  if (states.size > 150) states.delete(states.keys().next().value!);
}
export function readPosition(key: string) { return positions.get(key) ?? 0; }
export function savePosition(key: string, position: number) {
  positions.delete(key); positions.set(key, position);
  if (positions.size > 600) positions.delete(positions.keys().next().value!);
}
