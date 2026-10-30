import { defineConfig } from 'vite';
import { createBuildInfo } from './scripts/build-info.mjs';

const buildInfo = createBuildInfo();

// 构建产物交给 package_plugin.py 内联：固定单份 IIFE 和 CSS，供离线 MCP 面板加载。
export default defineConfig({
  root: 'frontend',
  // Library builds preserve process.env by default; sandboxed app iframes have no Node global.
  define: { 'process.env.NODE_ENV': JSON.stringify('production'), '__CODEX_TRACE_BUILD__': JSON.stringify(buildInfo) },
  plugins: [{ name: 'codex-trace-build-info', generateBundle() {
    this.emitFile({ type: 'asset', fileName: 'build-info.json', source: JSON.stringify(buildInfo, null, 2) + '\n' });
  } }],
  build: {
    outDir: '../dist/ui',
    emptyOutDir: true,
    lib: { entry: 'src/main.tsx', formats: ['iife'], name: 'CodexTrace', fileName: () => 'app.js' },
    cssCodeSplit: false,
  },
});
