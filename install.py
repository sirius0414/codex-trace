#!/usr/bin/env python3
"""Install a verified, prebuilt Codex Trace release without a source checkout."""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import uuid

sys.path.insert(0, str(Path(__file__).absolute().parent / 'scripts'))
from platform_runtime import executable, state_root, venv_python, IS_WINDOWS
from package_plugin import locked_versions, verify_runtime
import update_plugin as update

MARKET = 'codex-trace'
SELECTOR = 'codex-trace@' + MARKET
update.MARKET, update.SELECTOR = MARKET, SELECTOR


def write_json(path, value):
    temporary = path.with_name(path.name + '.tmp')
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    os.replace(temporary, path)


def read_json(path, default=None):
    return json.loads(path.read_text(encoding='utf-8')) if path.exists() else default


def default_root():
    identity = hashlib.sha256(str(update.codex_home().resolve()).encode()).hexdigest()[:12]
    return state_root().parent / 'installations' / identity


def check_python(python):
    probe = "import sys,venv,ensurepip; from compression import zstd; assert sys.version_info >= (3,14); assert zstd.decompress(zstd.compress(b'ok')) == b'ok'"
    try:
        subprocess.run([str(python), '-I', '-X', 'utf8', '-c', probe], check=True, capture_output=True, timeout=30)
    except (OSError, subprocess.SubprocessError) as error:
        raise ValueError('需要 Python 3.14+（含 venv、pip 和 compression.zstd）。请安装 python.org 官方 Python，或用 --python 指定解释器。') from error


def find_python(value=None):
    candidates = [value] if value else [os.environ.get('CODEX_TRACE_PYTHON'), sys.executable, 'python3.14', 'python3', 'python']
    for candidate in filter(None, candidates):
        try:
            path = executable(candidate)
            check_python(path)
            return path
        except (ValueError, OSError):
            pass
    raise ValueError('找不到 Python 3.14+（含 venv、pip 和 compression.zstd）。安装 https://www.python.org/downloads/ 后重试；可用 --python 指定路径。')


def find_codex(value=None):
    candidates = [value] if value else [shutil.which('codex')]
    if not value and not IS_WINDOWS:
        candidates += ['/Applications/ChatGPT.app/Contents/Resources/codex-cli/CodexCLI.app/Contents/MacOS/codex', '/Applications/Codex.app/Contents/Resources/codex']
    for candidate in filter(None, candidates):
        try:
            path = executable(candidate)
            for args in (['--version'], ['plugin', 'add', '--help'], ['plugin', 'remove', '--help'], ['plugin', 'marketplace', 'list', '--help']):
                subprocess.run([str(path), *args], check=True, capture_output=True, timeout=30)
            return path
        except (ValueError, OSError, subprocess.SubprocessError):
            pass
    raise ValueError('找不到支持 plugin add/remove/marketplace 的 Codex。请安装或更新 Codex 桌面应用及 CLI，把 codex 加入 PATH，或用 --codex 指定原生程序（Windows: codex.exe）。')


def verify_bundle(bundle):
    record = read_json(bundle / 'release.json')
    if not record or record.get('schemaVersion') != 1 or not isinstance(record.get('files'), dict):
        raise ValueError('缺少有效 release.json；请解压完整发布包，不能使用源码目录安装。')
    actual = update.fingerprints(bundle)
    actual.pop('release.json', None)
    if actual != record['files']:
        raise ValueError('发布包文件校验失败；请重新下载并校验 SHA256SUMS 后完整解压。')
    manifest = read_json(bundle / 'plugin/.codex-plugin/plugin.json')
    if not manifest or manifest.get('name') != 'codex-trace' or manifest.get('version') != record.get('version'):
        raise ValueError('发布版本与插件清单不一致。')
    locked_versions(bundle / 'plugin/requirements.lock')
    return record


@contextmanager
def locked_root(root):
    root.mkdir(parents=True, exist_ok=True)
    if root.is_symlink() or getattr(root.lstat(), 'st_file_attributes', 0) & 0x400:
        raise ValueError('安装根目录不能是符号链接。')
    for name in ('.install.lock', 'owner.json', 'owner.json.tmp', 'journal.json', 'current.json', 'transactions'):
        path = root / name
        if path.is_symlink() or (path.exists() and getattr(path.lstat(), 'st_file_attributes', 0) & 0x400):
            raise ValueError('安装管理文件不能是符号链接或重解析点。')
    marker = root / 'owner.json'
    expected = {'application': 'codex-trace', 'codexHome': str(update.codex_home().resolve())}
    previous = read_json(marker)
    if previous is not None and previous != expected:
        raise ValueError('安装根目录属于其他 CODEX_HOME；请使用独立 --root。')
    if previous is None and any(p.name not in {'.install.lock', 'owner.json.tmp'} for p in root.iterdir()):
        raise ValueError('请选择空的安装目录或此前由此安装器管理的目录。')
    handle = (root / '.install.lock').open('a+b')
    acquired = False
    try:
        if IS_WINDOWS:
            import msvcrt
            handle.seek(0); handle.write(b'0'); handle.flush(); handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        acquired = True
        # Recheck after locking; concurrent installers must never share a journal.
        previous = read_json(marker)
        if previous is not None and previous != expected:
            raise ValueError('安装目录绑定了其他 CODEX_HOME。')
        write_json(marker, expected)
        yield
    except OSError as error:
        if not acquired:
            raise RuntimeError('无法获取安装锁；另一个安装进程可能正在运行，请等待后重试。') from error
        raise
    finally:
        if acquired:
            if IS_WINDOWS:
                import msvcrt
                handle.seek(0); msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()


def create_runtime(python, runtime, requirements, log):
    # -I 忽略 PYTHONUTF8；显式启用 UTF-8，让 Windows 管道/日志支持中文安装路径。
    subprocess.run([str(python), '-I', '-X', 'utf8', '-m', 'venv', str(runtime)], check=True, timeout=180)
    target = venv_python(runtime)
    with log.open('w', encoding='utf-8') as stream:
        subprocess.run([str(target), '-I', '-X', 'utf8', '-m', 'pip', 'install', '--disable-pip-version-check', '--no-input', '--no-deps', '--requirement', str(requirements)], stdout=stream, stderr=subprocess.STDOUT, check=True, timeout=600)
        subprocess.run([str(target), '-I', '-X', 'utf8', '-m', 'pip', 'check'], stdout=stream, stderr=subprocess.STDOUT, check=True, timeout=60)
    return verify_runtime(target, locked_versions(requirements))


def verify_backend(python, package):
    """Load the packaged backend from an isolated Python process before registration."""
    probe = "import sys;sys.path.insert(0,sys.argv[1]);import codex_trace.app"
    with (package.parent / 'verification.log').open('w', encoding='utf-8') as log:
        subprocess.run([str(python), '-I', '-X', 'utf8', '-B', '-c', probe, str(package / 'backend')], check=True, stdout=log, stderr=subprocess.STDOUT, timeout=60)


def prepare(bundle, transaction, python, codex, record):
    package = transaction / 'plugin'
    shutil.copytree(bundle / 'plugin', package)
    runtime_dir = transaction / 'runtime'
    runtime = create_runtime(python, runtime_dir, package / 'requirements.lock', transaction / 'pip.log')
    target = venv_python(runtime_dir)
    launch = {'mcpServers': {'codex-trace': {'command': str(target), 'args': ['-m', 'codex_trace.app'], 'cwd': './backend', 'env': {
        'CODEX_HOME': str(update.codex_home()), 'CODEX_TRACE_CODEX_EXECUTABLE': str(codex),
        'CODEX_TRACE_STATE_ROOT': str(state_root()), 'CODEX_TRACE_EXPORT_ROOT': str(Path.home() / 'Downloads/Codex Trace'),
        'PYTHONDONTWRITEBYTECODE': '1', 'PYTHONUTF8': '1'}}}}
    write_json(package / '.mcp.json', launch)
    write_json(package / 'runtime.json', {'schemaVersion': 1, 'minimumPython': '3.14', 'runtimePython': str(target), 'appServerExecutable': str(codex), 'runtime': runtime, 'runtimeBundled': False, 'startupNetworkInstallation': False, 'platform': sys.platform, 'entrypoint': 'codex_trace.app',
        'uiSha256': hashlib.sha256((package / 'ui/trajectory-v1.html').read_bytes()).hexdigest(),
        'requirementsSha256': hashlib.sha256((package / 'requirements.lock').read_bytes()).hexdigest(),
        'thirdPartyManifestSha256': hashlib.sha256((package / 'third-party-manifest.json').read_bytes()).hexdigest()})
    verify_backend(target, package)
    marketplace = transaction / 'marketplace'
    subprocess.run([str(python), '-X', 'utf8', '-B', str(bundle / 'scripts/prepare_marketplace.py'), '--package', str(package), '--output', str(marketplace)], check=True, capture_output=True, timeout=60)
    catalog_path = marketplace / '.agents/plugins/marketplace.json'
    catalog = read_json(catalog_path)
    catalog['name'], catalog['interface']['displayName'] = MARKET, 'Codex Trace'
    write_json(catalog_path, catalog)
    write_json(transaction / 'verification.json', {'passed': True, 'backendImport': True, 'runtime': runtime})
    write_json(transaction / 'update-plan.json', {'schemaVersion': 1, 'platform': sys.platform, 'codexHome': str(update.codex_home()),
        'python': str(target), 'codex': str(codex), 'files': update.fingerprints(package),
        'catalogSha256': hashlib.sha256(catalog_path.read_bytes()).hexdigest()})


def apply(transaction):
    # CLI must run outside a user's project, where project config could override
    # the intended CODEX_HOME. Reuse existing cache + MCP verification unchanged.
    update.ROOT = transaction
    return update.install(transaction)


def ensure_owned_market(root, codex):
    update.ROOT = root
    entries = update.cli_json(codex, 'plugin', 'marketplace', 'list', '--json')['marketplaces']
    entry = next((e for e in entries if e['name'] == MARKET), None)
    if entry:
        location = Path(entry['root']).resolve()
        if not update.is_local_marketplace(entry) or not location.is_relative_to((root / 'transactions').resolve()):
            raise ValueError('同名 codex-trace 市场不是此安装器管理的来源；为避免影响现有插件，未作修改。')


def restore(root, journal, codex):
    target = journal.get('previous') or journal['target']
    transaction = Path(target)
    if not transaction.resolve().is_relative_to((root / 'transactions').resolve()):
        raise ValueError('恢复记录指向安装目录之外。')
    if not (transaction / 'update-plan.json').is_file():
        journal['phase'] = 'aborted'
        write_json(root / 'journal.json', journal)
        return {'recovered': True, 'installed': False, 'message': '准备阶段未完成，原安装未修改；可重新安装。'}
    ensure_owned_market(root, codex)
    # Recovery uses the verified executable stored in the transaction plan.
    result = apply(transaction)
    write_json(root / 'current.json', {**result, 'transaction': str(transaction)})
    journal['phase'] = 'recovered'
    write_json(root / 'journal.json', journal)
    return {**result, 'recovered': True}


def install(bundle: Path, root: Path, python: Path, codex: Path) -> dict:
    bundle, root = bundle.resolve(), root.absolute()
    record = verify_bundle(bundle)
    check_python(python)
    with locked_root(root):
        journal = read_json(root / 'journal.json', {})
        if journal.get('phase') in ('preparing', 'installing', 'recovery-required', 'uninstalling'):
            raise ValueError('检测到未完成的操作；请先运行 install.py recover（使用相同 --root / CODEX_HOME）。')
        ensure_owned_market(root, codex)
        previous = read_json(root / 'current.json', {})
        transaction = root / 'transactions' / uuid.uuid4().hex
        transaction.mkdir(parents=True)
        journal = {'phase': 'preparing', 'previous': previous.get('transaction'), 'target': str(transaction), 'codexHome': str(update.codex_home()), 'version': record['version']}
        write_json(root / 'journal.json', journal)
        try:
            prepare(bundle, transaction, python, codex, record)
            journal['phase'] = 'installing'
            write_json(root / 'journal.json', journal)
            result = apply(transaction)
            write_json(root / 'current.json', {**result, 'transaction': str(transaction)})
            journal['phase'] = 'complete'
            write_json(root / 'journal.json', journal)
            return result
        except (Exception, KeyboardInterrupt) as error:
            phase = journal['phase']
            journal['phase'] = 'failed' if phase == 'preparing' else 'recovery-required'
            journal['error'] = type(error).__name__
            journal['failedPhase'] = phase
            journal['detail'] = str(error)
            write_json(root / 'journal.json', journal)
            if phase == 'installing' and journal['previous']:
                try:
                    restore(root, journal, codex)
                except (Exception, KeyboardInterrupt) as recovery_error:
                    journal['phase'] = 'recovery-required'
                    journal['recoveryError'] = type(recovery_error).__name__
                    write_json(root / 'journal.json', journal)
            raise RuntimeError(f'安装未完成。状态：{root / "journal.json"}；诊断目录：{transaction}（pip.log / verification.log）。修复后运行 recover 或重新 install；旧运行环境和导出文件均保留。') from error


def recover(root: Path, codex: Path) -> dict:
    with locked_root(root):
        journal = read_json(root / 'journal.json', {})
        if journal.get('phase') == 'uninstalling':
            return _uninstall(root, codex)
        if journal.get('phase') not in ('preparing', 'installing', 'recovery-required', 'failed'):
            return {'recovered': True, 'message': '无需恢复。'}
        return restore(root, journal, codex)


def _uninstall(root, codex):
    ensure_owned_market(root, codex)
    current = read_json(root / 'current.json', {})
    journal = read_json(root / 'journal.json', {})
    write_json(root / 'journal.json', {**journal, 'phase': 'uninstalling'})
    update.ROOT = root
    update.cli_json(codex, 'plugin', 'remove', SELECTOR, '--json')
    entries = update.cli_json(codex, 'plugin', 'marketplace', 'list', '--json')['marketplaces']
    if any(e['name'] == MARKET for e in entries):
        update.run([str(codex), 'plugin', 'marketplace', 'remove', MARKET])
    (root / 'current.json').unlink(missing_ok=True)
    write_json(root / 'journal.json', {'phase': 'uninstalled', 'previous': current.get('transaction')})
    return {'uninstalled': True, 'exportsPreserved': True, 'runtimesPreserved': True}


def uninstall(root: Path, codex: Path) -> dict:
    if not (root / 'owner.json').exists():
        return {'uninstalled': True, 'message': '此安装目录没有受管理的安装。'}
    with locked_root(root):
        if read_json(root / 'journal.json', {}).get('phase') == 'uninstalled':
            return {'uninstalled': True, 'exportsPreserved': True, 'runtimesPreserved': True}
        return _uninstall(root, codex)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', nargs='?', default='install', choices=['install', 'update', 'recover', 'uninstall', 'status'])
    parser.add_argument('--bundle', type=Path, default=Path(__file__).absolute().parent)
    parser.add_argument('--root', type=Path, default=default_root())
    parser.add_argument('--python')
    parser.add_argument('--codex')
    args = parser.parse_args()
    try:
        if sys.platform not in ('darwin', 'win32'):
            raise ValueError('发布安装器支持 macOS 和 Windows；Linux/WSL 暂不作为支持平台。')
        root = args.root.expanduser().absolute()
        if args.command == 'status':
            result = {'root': str(root), 'current': read_json(root / 'current.json'), 'journal': read_json(root / 'journal.json')}
        else:
            codex = find_codex(args.codex)
            if args.command in ('install', 'update'):
                result = install(args.bundle, root, find_python(args.python), codex)
            elif args.command == 'recover':
                result = recover(root, codex)
            else:
                result = uninstall(root, codex)
        # CLI JSON 保持 ASCII，成功结果中的中文路径不能因 Windows 管道编码而误报失败。
        print(json.dumps(result, ensure_ascii=True, indent=2))
        return 0
    except (OSError, ValueError, RuntimeError, KeyError, subprocess.SubprocessError) as error:
        print(f'Codex Trace: {error}', file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print('安装已中断；请运行 recover 检查并恢复。', file=sys.stderr)
        return 130


if __name__ == '__main__':
    raise SystemExit(main())
