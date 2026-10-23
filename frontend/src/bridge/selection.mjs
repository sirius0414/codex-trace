/** M0 诊断异步结果绑定界面选择代次；不能用共享服务进程身份代替某个界面的当前选择。 */
export class SelectionGuard {
  constructor() { this.epoch = 0; this.threadId = null; this.activationId = null; }
  select(threadId, activationId = this.activationId) {
    this.epoch += 1;
    this.threadId = threadId;
    this.activationId = activationId;
    return this.capture();
  }
  capture() { return { epoch: this.epoch, threadId: this.threadId, activationId: this.activationId }; }
  accepts(value) {
    return value.epoch === this.epoch && value.threadId === this.threadId && value.activationId === this.activationId;
  }
}
