// 开发宿主只适配 MCP App 协议；轨迹界面和工具结果均来自正式代码。
const frame = document.querySelector('#app');
const surface = document.querySelector('#surface');
const status = document.querySelector('#status');
const token = JSON.parse(document.querySelector('#dev-token').textContent);
const colorScheme = matchMedia('(prefers-color-scheme: dark)');
let initialized = false;
let opening = false;

function showStatus(message, state = 'ready') {
  status.textContent = message;
  status.dataset.state = state;
}

function context() {
  const bounds = surface.getBoundingClientRect();
  return {
    theme: colorScheme.matches ? 'dark' : 'light',
    displayMode: 'fullscreen',
    availableDisplayModes: ['fullscreen'],
    containerDimensions: { width: Math.round(bounds.width), height: Math.round(bounds.height) },
    locale: navigator.language || 'zh-CN',
    timeZone: Intl.DateTimeFormat().resolvedOptions().timeZone || 'UTC',
  };
}

function send(message) {
  frame.contentWindow.postMessage({ jsonrpc: '2.0', ...message }, location.origin);
}

function rpcError(id, message, code = -32603) {
  send({ id, error: { code, message } });
}

async function call(name, args) {
  const response = await fetch('/rpc', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', 'X-Codex-Trace-Token': token },
    body: JSON.stringify({ name, arguments: args }),
    cache: 'no-store',
    credentials: 'omit',
    signal: AbortSignal.timeout(15000),
  });
  if (!response.ok) throw new Error('local-service-unavailable');
  const result = await response.json();
  if (!result || typeof result !== 'object' || Array.isArray(result)) throw new Error('invalid-local-response');
  return result;
}

async function open() {
  // initialized 通知可能重复；同一页面只建立一个观察会话。
  if (opening) return;
  opening = true;
  try {
    // 普通浏览器没有 Codex 当前聊天身份，始终走全局入口的任务选择器。
    const result = await call('open_codex_trace', {});
    send({ method: 'ui/notifications/tool-result', params: result });
    showStatus(result.isError ? '本地读取失败，请刷新重试。' : '已连接本地服务', result.isError ? 'error' : 'ready');
  } catch {
    showStatus('连接失败，请确认开发服务仍在运行，然后刷新。', 'error');
  }
}

window.addEventListener('message', async event => {
  // 只接受同源的正式界面 iframe，其他页面不能借宿主转发本地工具请求。
  if (event.source !== frame.contentWindow || event.origin !== location.origin) return;
  const message = event.data;
  if (!message || message.jsonrpc !== '2.0' || typeof message.method !== 'string') return;
  const { id, method, params } = message;
  const hasId = typeof id === 'string' || typeof id === 'number';
  if (method === 'ui/initialize') {
    if (!hasId) return;
    send({ id, result: {
      protocolVersion: params?.protocolVersion,
      hostInfo: { name: 'Codex Trace browser development host', version: '1.0.0' },
      hostCapabilities: { serverTools: {} },
      hostContext: context(),
    } });
    return;
  }
  if (method === 'ui/notifications/initialized') {
    if (!initialized) { initialized = true; void open(); }
    return;
  }
  if (method === 'ui/notifications/size-changed') return;
  if (method === 'tools/call') {
    if (!hasId) return;
    if (!initialized || typeof params?.name !== 'string') {
      rpcError(id, '开发宿主尚未就绪或请求无效。', -32600);
      return;
    }
    try {
      const result = await call(params.name, params.arguments ?? {});
      // 保留完整 MCP 信封，包括 isError 和私有 _meta，不将错误误报为成功。
      send({ id, result });
    } catch {
      showStatus('本地连接中断，请确认开发服务仍在运行。', 'error');
      rpcError(id, '本地开发服务连接失败，请刷新重试。');
    }
    return;
  }
  if (hasId) rpcError(id, '开发宿主不支持此请求。', -32601);
});

function updateContext() {
  if (initialized) send({ method: 'ui/notifications/host-context-changed', params: context() });
}
colorScheme.addEventListener('change', updateContext);
new ResizeObserver(updateContext).observe(surface);
document.querySelector('#refresh').addEventListener('click', () => location.reload());
setTimeout(() => {
  if (!initialized) showStatus('界面连接超时，请确认开发服务仍在运行，然后刷新。', 'error');
}, 15000);

// 先安装消息监听，再加载 SDK，避免丢失初始化请求。
frame.src = '/app.html';
