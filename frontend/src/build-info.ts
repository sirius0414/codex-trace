type BuildInfo = { version: string; buildId: string; sourceHash: string; builtAt: string };
declare const __CODEX_DISPLAY_BUILD__: BuildInfo;

// Vite 在打包时注入源码指纹；独立组件预览未经过打包，不能冒用正式构建标识。
export const buildInfo: BuildInfo = typeof __CODEX_DISPLAY_BUILD__ === 'undefined'
  ? { version: 'dev', buildId: '', sourceHash: '', builtAt: '' }
  : __CODEX_DISPLAY_BUILD__;

export const versionLabel = `v${buildInfo.version}${buildInfo.buildId ? ` · ${buildInfo.buildId}` : ''}`;
export const versionDescription = [`Codex Display ${versionLabel}`,
  buildInfo.sourceHash && `源码指纹：${buildInfo.sourceHash}`,
  buildInfo.builtAt && `构建时间：${buildInfo.builtAt}`].filter(Boolean).join('\n');
