#!/usr/bin/env python3
"""Real CLI/venv lifecycle smoke test. All writes use a disposable user profile.

Use --wheelhouse for an offline test; its wheels are copied out of the checkout.
Without it pip downloads the release's exact dependencies. No real task is read.
"""
from __future__ import annotations
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import tomllib
import zipfile
from platform_runtime import executable


def smoke(archive, codex, wheelhouse=None):
    checks = []
    with tempfile.TemporaryDirectory(prefix='codex-trace-release-smoke-') as temp:
        root = Path(temp).resolve()
        home = root / 'user home 测试'
        home.mkdir()
        codex_home = home / '.codex'
        codex_home.mkdir()
        (codex_home / 'config.toml').write_text('model = "unrelated-setting-preserved"\n', encoding='utf-8')
        # An unrelated plugin and synthetic session must survive every operation.
        sentinels = {codex_home / 'sessions/rollout.jsonl': b'{"synthetic":true}\n',
            codex_home / 'plugins/cache/unrelated/plugin/1.0/sentinel': b'other plugin',
            home / 'Downloads/Codex Trace/export.zip': b'user exported file'}
        for path, data in sentinels.items():
            path.parent.mkdir(parents=True, exist_ok=True); path.write_bytes(data)
        with zipfile.ZipFile(archive) as source:
            source.extractall(root / 'download')
        bundle = next((root / 'download').iterdir())
        env = dict(os.environ, HOME=str(home), USERPROFILE=str(home), CODEX_HOME=str(codex_home),
            LOCALAPPDATA=str(home / 'AppData/Local'), APPDATA=str(home / 'AppData/Roaming'), PYTHONUTF8='1', PYTHONDONTWRITEBYTECODE='1')
        env.pop('PYTHONPATH', None)
        env.pop('PYTHONHOME', None)
        if wheelhouse:
            shutil.copytree(wheelhouse, root / 'wheels')
            env.update(PIP_NO_INDEX='1', PIP_FIND_LINKS=str(root / 'wheels'))
        python = str(Path(getattr(sys, '_base_executable', sys.executable)).absolute())
        destination = home / 'installation'
        command = [python, '-B', str(bundle / 'install.py')]
        options = ['--root', str(destination), '--python', python, '--codex', str(codex)]
        def run(mode, *, expected=0, overrides=None):
            result = subprocess.run([*command, mode, *options], cwd=root, env={**env, **(overrides or {})}, capture_output=True, text=True, encoding='utf-8', timeout=900)
            if result.returncode != expected:
                details = result.stdout + result.stderr
                for log in destination.glob('transactions/*/*.log'):
                    details += '\n' + log.name + '\n' + log.read_text(encoding='utf-8', errors='replace')[-6000:]
                raise AssertionError(f'{mode}: exit {result.returncode}\n{details}')
            for path, data in sentinels.items():
                assert path.read_bytes() == data, f'Unrelated data changed: {path.name}'
            assert tomllib.loads((codex_home / 'config.toml').read_text(encoding='utf-8'))['model'] == 'unrelated-setting-preserved'
            checks.append(mode + ('-expected-failure' if expected else ''))
        # A failed download cannot register anything, and can be retried.
        run('install', expected=1, overrides={'PIP_NO_INDEX': '1', 'PIP_FIND_LINKS': str(root / 'missing-wheels')})
        run('recover')
        run('install')
        current = json.loads((destination / 'current.json').read_text(encoding='utf-8'))
        initial = current['transaction']
        verifier = Path(__file__).absolute().with_name('verify_install.py')
        runtime = json.loads((Path(current['installedPath']) / 'runtime.json').read_text(encoding='utf-8'))
        verification = root / 'mcp-verification.json'
        verified = subprocess.run([runtime['runtimePython'], '-B', str(verifier), '--package', current['installedPath'],
            '--cache-copy-only', '--out', str(verification)], cwd=root, env=env, capture_output=True, text=True, encoding='utf-8', timeout=120)
        assert verified.returncode == 0, verified.stdout + verified.stderr
        assert json.loads(verification.read_text(encoding='utf-8'))['passed']
        checks.append('existing-full-mcp-and-export-verifier')
        assert (Path(current['installedPath']) / 'backend/codex_trace/app.py').is_file()
        run('install')
        # A synthetic next version tests the real Codex version/cache transition.
        manifest_path = bundle / 'plugin/.codex-plugin/plugin.json'
        manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
        manifest['version'] = '999.0.0'
        manifest_path.write_text(json.dumps(manifest), encoding='utf-8')
        release_path = bundle / 'release.json'
        release = json.loads(release_path.read_text(encoding='utf-8'))
        release['version'] = manifest['version']
        release['files']['plugin/.codex-plugin/plugin.json'] = hashlib.sha256(manifest_path.read_bytes()).hexdigest()
        release_path.write_text(json.dumps(release), encoding='utf-8')
        run('update')
        current = json.loads((destination / 'current.json').read_text(encoding='utf-8'))
        assert current['version'] == '999.0.0'
        before = current.copy()
        run('update', expected=1, overrides={'PIP_NO_INDEX': '1', 'PIP_FIND_LINKS': str(root / 'missing-wheels')})
        assert json.loads((destination / 'current.json').read_text(encoding='utf-8')) == before
        run('recover')
        # Recovery is independent of the downloaded archive and source checkout.
        bundle.rename(root / 'unavailable-download')
        current = json.loads((destination / 'current.json').read_text(encoding='utf-8'))
        cache = Path(current['installedPath'])
        launch = json.loads((cache / '.mcp.json').read_text(encoding='utf-8'))['mcpServers']['codex-trace']
        server_env = {**env, **launch['env']}
        probe = subprocess.Popen([launch['command'], *launch['args']], cwd=cache / launch['cwd'], env=server_env,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding='utf-8')
        request = {'jsonrpc':'2.0','id':1,'method':'initialize','params':{'protocolVersion':'2025-03-26','capabilities':{},'clientInfo':{'name':'release-smoke','version':'1'}}}
        stdout, stderr = probe.communicate(json.dumps(request) + '\n', timeout=30)
        assert any(json.loads(line).get('id') == 1 and 'result' in json.loads(line) for line in stdout.splitlines()), stderr
        checks.append('mcp-start-without-download-or-checkout')
        # Restore only the downloaded installer so the public uninstall entry runs.
        (root / 'unavailable-download').rename(bundle)
        run('status')
        run('uninstall')
        run('uninstall')
        assert Path(initial).is_dir(), 'Old runtime must remain for live sessions'
    return {'passed': True, 'platform': sys.platform, 'checks': checks,
        'realVenv': True, 'realDependencies': True, 'realCodex': True, 'isolatedUserProfile': True,
        'syntheticUpdateVersion': '999.0.0'}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--archive', type=Path, required=True)
    parser.add_argument('--codex', default='codex')
    parser.add_argument('--wheelhouse', type=Path)
    parser.add_argument('--report', type=Path, required=True)
    args = parser.parse_args()
    try:
        report = smoke(args.archive.resolve(), executable(args.codex), args.wheelhouse.resolve() if args.wheelhouse else None)
    except Exception as error:
        report = {'passed': False, 'platform': sys.platform, 'error': str(error)}
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(json.dumps(report, ensure_ascii=False))
    return 0 if report['passed'] else 1

if __name__ == '__main__':
    raise SystemExit(main())
