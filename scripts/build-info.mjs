import { createHash } from 'node:crypto';
import { readdirSync, readFileSync, statSync } from 'node:fs';
import { join } from 'node:path';
import { fileURLToPath } from 'node:url';

const projectRoot = fileURLToPath(new URL('..', import.meta.url));

/** 用源码内容标识安装版本；builtAt 单独记录，重复构建不会因此改变 buildId。 */
export function createBuildInfo(root = projectRoot) {
  const files = [];
  function collect(relative) {
    if (statSync(join(root, relative)).isDirectory()) {
      for (const name of readdirSync(join(root, relative))) {
        if (name !== '__pycache__' && name !== '.DS_Store') collect(`${relative}/${name}`);
      }
    } else if (!relative.endsWith('.pyc')) files.push(relative);
  }
  // Hash actual source bytes, including uncommitted edits. Generated bundles,
  // timestamps, local documents, and Git state do not change the source identity.
  for (const source of ['frontend/src', 'backend/codex_display', 'contracts', 'plugin',
    'package.json', 'package-lock.json', 'requirements.lock', 'tsconfig.json',
    'vite.config.ts', 'scripts/build-info.mjs', 'scripts/package_plugin.py']) collect(source);
  const hash = createHash('sha256');
  for (const file of files.sort()) {
    const bytes = readFileSync(join(root, file));
    // 路径、长度和内容共同参与哈希，防止不同文件划分拼接成相同的输入序列。
    hash.update(`${file}\0${bytes.length}\0`).update(bytes);
  }
  const sourceHash = hash.digest('hex');
  return { version: JSON.parse(readFileSync(join(root, 'package.json'), 'utf8')).version,
    buildId: sourceHash.slice(0, 8), sourceHash, builtAt: new Date().toISOString() };
}
