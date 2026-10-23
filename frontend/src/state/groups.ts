/** 子 Agent 首屏读取调度；按观察绑定隔离请求，不依赖阅读偏好保存。 */
import type { ReadTrajectoryRequest, ThreadSnapshot, ViewState } from '../../../contracts/types';
import { sameBinding } from './binding.mjs';

/** 已选择或展开的已知子分组需要读取内容；仅选择后代不会隐式选择其祖先。 */
export function requestedChildGroups(snapshot: ThreadSnapshot, view: ViewState): string[] {
  const requested = new Set(view.expandedGroupIds);
  if (view.agentScope.kind === 'all') {
    for (const group of snapshot.groups) requested.add(group.groupId);
  } else if (view.agentScope.kind === 'selected') {
    for (const id of view.agentScope.groupIds) requested.add(id);
  } else if (view.agentScope.kind === 'group' && view.agentScope.groupId) {
    requested.add(view.agentScope.groupId);
    for (const id of requested) {
      for (const group of snapshot.groups) if (group.parentGroupId === id) requested.add(group.groupId);
    }
  }
  return snapshot.groups.filter(group => group.groupId !== snapshot.rootGroupId && requested.has(group.groupId))
    .map(group => group.groupId);
}

type Context = { snapshot: ThreadSnapshot; view: ViewState; isCurrent: () => boolean };
type Options = {
  capture: () => Context | null;
  read: (request: ReadTrajectoryRequest, signal: AbortSignal) => Promise<ThreadSnapshot>;
  commit: (snapshot: ThreadSnapshot) => void;
  failed: (failure: unknown) => void;
  rebuild: () => void;
};

/** 串行读取子任务首屏；空页和失败记忆只在当前绑定、来源代次内有效。 */
export class GroupReader {
  private options: Options;
  private key: string | null = null;
  private attempted = new Set<string>();
  private busy = false;
  private visible = true;
  private disposed = false;
  private rebuilding = false;
  private controller: AbortController | null = null;
  constructor(options: Options) { this.options = options; }
  private sync(context: Context | null) {
    const key = context ? JSON.stringify([context.snapshot.binding, context.snapshot.generation]) : null;
    if (key !== this.key) {
      this.controller?.abort(); this.key = key;
      this.attempted.clear(); this.rebuilding = false;
    }
  }
  update() {
    if (this.disposed) return;
    this.sync(this.options.capture());
    if (!this.busy && this.visible && !this.rebuilding) void this.drain();
  }
  retry() { this.attempted.clear(); this.rebuilding = false; this.update(); }
  cancel() { this.controller?.abort(); this.key = null; this.attempted.clear(); this.rebuilding = false; }
  setVisible(visible: boolean) {
    this.visible = visible;
    if (!visible) this.controller?.abort(); else this.update();
  }
  dispose() { this.disposed = true; this.cancel(); }
  private async drain() {
    if (this.busy) return;
    this.busy = true;
    try {
      while (!this.disposed && this.visible && !this.rebuilding) {
        const context = this.options.capture(); this.sync(context);
        if (!context || !context.isCurrent()) break;
        const groupId = requestedChildGroups(context.snapshot, context.view).find(id => !this.attempted.has(id)
          && !context.snapshot.events.some(event => event.groupId === id));
        if (!groupId) break;
        // 空页也代表已完成一次读取；失败等待显式重试，避免每次轮询更新都重复请求。
        this.attempted.add(groupId);
        const key = this.key, controller = new AbortController(); this.controller = controller;
        try {
          const result = await this.options.read({ binding: context.snapshot.binding, cursor: null,
            anchorEventId: null, groupId }, controller.signal);
          if (controller.signal.aborted || key !== this.key || !context.isCurrent()) continue;
          if (!sameBinding(result.binding, context.snapshot.binding)) continue;
          if (result.generation !== context.snapshot.generation) {
            this.rebuilding = true; this.options.rebuild(); break;
          }
          this.options.commit(result);
          // 同代次重建可能丢失本地子事件，但服务端仍报告已加载；以实际本地内容为准，
          // 只有空页需要保留完成标记，有内容的分组若在重建时丢失则允许补读。
          if (result.events.some(event => event.groupId === groupId)) this.attempted.delete(groupId);
        } catch (failure) {
          if (!controller.signal.aborted && key === this.key && context.isCurrent()) this.options.failed(failure);
        } finally {
          if (controller.signal.aborted && key === this.key) this.attempted.delete(groupId);
          if (this.controller === controller) this.controller = null;
        }
      }
    } finally { this.busy = false; }
  }
}
