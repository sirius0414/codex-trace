import type { ThreadSnapshot, ThreadSummary, ViewState, ContentPart, ReaderMetadata } from '../../contracts/types';

/** UI 缓存已拼接正文与续读游标；跨请求的绑定/版本核验由状态层在写入前完成。 */
export type DetailPart = { text: string; nextCursor: string | null; completeness: string; sourceTruncated: boolean; readableBytes?: number | null; reader?: ReaderMetadata };
export type DetailData = { eventId: string; summary: string; parts: Partial<Record<ContentPart, DetailPart>>; explanation: string };
export type TraceUIProps = {
  snapshot: ThreadSnapshot | null;
  view: ViewState;
  trackingPaused: boolean;
  detail: DetailData | null;
  threads: ThreadSummary[];
  threadsMore: boolean;
  threadScope: 'user' | 'all';
  matchedIds: Set<string> | null;
  searching?: boolean;
  loading: boolean;
  detailLoading: boolean;
  error: string;
  exportMessage: string;
  exporting: boolean;
  onTask: (threadId: string) => void;
  onTaskSearch: (query: string) => void;
  onMoreThreads: () => void;
  onThreadScope: (scope: 'user' | 'all') => void;
  onView: (patch: Partial<ViewState>) => void;
  onTrackingPause: (paused: boolean) => void;
  onSelect: (eventId: string | null) => void;
  onAgentNavigate?: (groupId: string, eventId?: string) => void;
  onLoadEarlier: (groupId?: string) => void;
  onMoreDetail: (part: ContentPart, readAll?: boolean) => void;
  onExport: () => void;
  onRetry: () => void;
};
