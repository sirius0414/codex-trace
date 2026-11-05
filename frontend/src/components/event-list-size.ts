/** 列表尺寸变化独立于用户滚动；scroll 与 ResizeObserver 谁先到达都只处理一次。 */
export class ListSizeTracker {
  private height: number | null = null;

  /** 返回尺寸变化后的自动定位目标；null 表示首次测量或高度未变，不覆盖主动阅读。 */
  measure(height: number, scrollHeight: number, following: boolean, readingTop: number): number | null {
    const previous = this.height;
    this.height = height;
    if (previous == null || previous === height) return null;
    const max = Math.max(0, scrollHeight - height);
    return following ? max : Math.max(0, Math.min(max, readingTop));
  }
}
