/** 将历史接口返回的多个 rollout 条目归并为唯一的逻辑 Thread 列表。 */
import type { ThreadSummary } from '../../../contracts/types';

/** 首次加载和搜索替换列表，分页追加；保留首次位置，同 ID 取更新时间更晚的元数据。 */
export function mergeThreadPage(previous: readonly ThreadSummary[], incoming: readonly ThreadSummary[], append = false): ThreadSummary[] {
  const threads = new Map<string, ThreadSummary>();
  for (const row of append ? [...previous, ...incoming] : incoming) {
    const current = threads.get(row.threadId);
    // 旧 rollout 可能出现在同页或后续页；未知时间、相同时间都不能覆盖已保留的记录。
    if (!current || (row.updatedAt ?? -Infinity) > (current.updatedAt ?? -Infinity)) {
      threads.set(row.threadId, row);
    }
  }
  return [...threads.values()];
}
