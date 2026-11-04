import { build } from 'vite';
import { createBuildInfo } from '../../../scripts/build-info.mjs';
import { fileURLToPath } from 'node:url';
await build({configFile:false,root:fileURLToPath(new URL('.',import.meta.url)),base:'./',define:{'process.env.NODE_ENV':JSON.stringify('production'),'__CODEX_TRACE_BUILD__':JSON.stringify(createBuildInfo())},build:{outDir:'../../../build/reader-preview',emptyOutDir:true}});
