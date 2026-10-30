/** npm 开发入口选择已有 Python；保留 argv，支持中文与空格目录，不经过 shell。 */
import { existsSync } from 'node:fs';
import { spawnSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';

const windows = process.platform === 'win32';
const root = fileURLToPath(new URL('../', import.meta.url));
const venv = fileURLToPath(new URL(windows ? '../.venv/Scripts/python.exe' : '../.venv/bin/python', import.meta.url));
const [script, ...arguments_] = process.argv.slice(2);
if (!script || !/^[A-Za-z0-9_-]+\.py$/.test(script)) throw new Error('Expected a repository Python script name');
const candidates = process.env.CODEX_TRACE_PYTHON
  ? [[process.env.CODEX_TRACE_PYTHON]]
  : [...(existsSync(venv) ? [[venv]] : []), ...(windows ? [['py', '-3.14'], ['python']] : [['python3']])];
let result;
for (const command of candidates) {
  result = spawnSync(command[0], [...command.slice(1), fileURLToPath(new URL(script, import.meta.url)), ...arguments_],
    { cwd: root, stdio: 'inherit', shell: false, env: { ...process.env, PYTHONUTF8: '1' } });
  if (result.error?.code !== 'ENOENT') break;
}
if (result?.error) {
  console.error(`Unable to start Python: ${result.error.message}. Set CODEX_TRACE_PYTHON to an existing interpreter.`);
  process.exitCode = 1;
} else process.exitCode = result?.status ?? 1;
