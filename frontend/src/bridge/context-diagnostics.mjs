/** M0 仅按上下文字段名去重诊断；实际主题/显示模式等值仍由宿主回调正常更新。 */
export class ContextDiagnosticGate {
  constructor() { this.lastSignature = null; }

  shouldReport(activationId, context) {
    if (!activationId) return false;
    const signature = JSON.stringify([activationId, Object.keys(context).sort()]);
    if (signature === this.lastSignature) return false;
    // 发送前记下签名，诊断 RPC 自身也可能触发上下文事件，防止递归上报。
    this.lastSignature = signature;
    return true;
  }
}
