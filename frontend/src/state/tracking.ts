/** 显式跟踪暂停独立于阅读位置；暂停使本轮所有快照请求失效，继续时建立新一轮读取。 */
export class TrackingGate {
  paused = false;
  private controller = new AbortController();
  get signal() { return this.controller.signal; }

  /** 重复设置保持当前请求；继续只开放新请求，已取消的信号不会复活。 */
  setPaused(paused: boolean) {
    if (paused === this.paused) return;
    this.paused = paused;
    if (paused) this.controller.abort();
    else this.controller = new AbortController();
  }
}
