import { defineConfig } from 'vite';
import { createBuildInfo } from './scripts/build-info.mjs';

const buildInfo = createBuildInfo();

export default defineConfig({
  root: 'frontend',
  // Library builds preserve process.env by default; sandboxed app iframes have no Node global.
  define: { 'process.env.NODE_ENV': JSON.stringify('production'), '__CODEX_DISPLAY_BUILD__': JSON.stringify(buildInfo) },
  plugins: [{ name: 'codex-display-build-info', generateBundle() {
    this.emitFile({ type: 'asset', fileName: 'build-info.json', source: JSON.stringify(buildInfo, null, 2) + '\n' });
  } }],
  build: {
    outDir: '../dist/ui',
    emptyOutDir: true,
    lib: { entry: 'src/main.tsx', formats: ['iife'], name: 'CodexDisplay', fileName: () => 'app.js' },
    cssCodeSplit: false,
  },
});
