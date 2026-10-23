/** Deduplicate field-name diagnostics only; always apply the actual host context. */
export class ContextDiagnosticGate {
  constructor() { this.lastSignature = null; }

  shouldReport(activationId, context) {
    if (!activationId) return false;
    const signature = JSON.stringify([activationId, Object.keys(context).sort()]);
    if (signature === this.lastSignature) return false;
    // Mark before sending: the diagnostic RPC may itself trigger a context event.
    this.lastSignature = signature;
    return true;
  }
}
