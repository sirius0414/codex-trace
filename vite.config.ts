import { defineConfig } from 'vite';

export default defineConfig({
  root: 'frontend',
  // Library builds preserve process.env by default; sandboxed app iframes have no Node global.
  define: { 'process.env.NODE_ENV': JSON.stringify('production') },
  build: {
    outDir: '../dist/ui',
    emptyOutDir: true,
    lib: { entry: 'src/main.tsx', formats: ['iife'], name: 'CodexDisplay', fileName: () => 'app.js' },
    cssCodeSplit: false,
  },
});
