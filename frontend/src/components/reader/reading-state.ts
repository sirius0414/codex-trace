/** 只在当前页面会话内保存阅读偏好/位置；不把正文、查询或工具内容写入宿主持久化 ViewState。 */
import type { JsonReaderState } from './JsonReader';
import type { RichReaderState } from './RichReader';
import type { ContentPart, TraceEvent } from '../../../../contracts/types';
import type { DetailPart } from '../../ui-types';
import { previewBodyPresence, previewCollapseDefaults, type ReaderMode, type TextHit } from './preview-model.ts';
export type BlockReadingState = { collapsed?: boolean; defaultCollapsed?: boolean; source?: boolean; mode?: ReaderMode; language?: string; json?: JsonReaderState; rich?: RichReaderState; sourceLimit?: number; codeLines?: number; commandLines?: number; patchFiles?: Record<string, {collapsed?:boolean;lines?:number}> };
export type ReadingState = { expanded: boolean; query: string; blocks: Record<string, BlockReadingState>; hit?: TextHit };
const states = new Map<string, ReadingState>();
const positions = new Map<string, number>();
export const initialReadingState = (): ReadingState => ({ expanded: false, query: '', blocks: {} });
/** 每块首次确认存在时锁定默认值；占位不锁定，分页、重读和实时刷新不重置阅读选择。 */
export function initializePreviewState(event: TraceEvent, parts: Partial<Record<ContentPart, DetailPart>>, state: ReadingState): ReadingState {
  let blocks = state.blocks;
  for (const [part, defaultCollapsed] of Object.entries(previewCollapseDefaults(event, parts))) {
    if (blocks[part]?.defaultCollapsed !== undefined) continue;
    // 暂定另一块存在时可先折叠，但须等它确认非空后再锁定，避免最终只剩唯一正文仍被折叠。
    if (defaultCollapsed && previewBodyPresence(event, parts, part === 'input' ? 'output' : 'input') === 'pending') continue;
    if (blocks === state.blocks) blocks = { ...blocks };
    blocks[part] = { ...blocks[part], defaultCollapsed };
  }
  return blocks === state.blocks ? state : { ...state, blocks };
}
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
