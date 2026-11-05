# Updating the cross-platform license snapshot

[English](README.md) | [简体中文](README.zh-CN.md)

Release builds only verify and copy the original license texts listed in `manifest.json`; they do not require Python packages for another platform to be installed on the build machine. The `pywin32==311` entry in `requirements.lock` is installed only on Windows, while the license inventory always covers all locked dependencies for macOS and Windows.

After installing the current platform's dependencies from the lockfiles, run:

```sh
python scripts/third_party_licenses.py --write
python scripts/third_party_licenses.py
```

For dependencies not installed on the current platform, the generator reuses license texts from the existing snapshot only when the name, version, and platform condition match and every file passes verification. When adding or upgrading cross-platform dependencies, first download the official wheel, then use `--wheelhouse` to read its METADATA and license files. The generator does not install or execute code from the wheel, and does not access the network itself.

For example, to update the pywin32 license snapshot for Windows Python 3.14 from macOS:

```sh
python -m pip download pywin32==311 --platform win_amd64 --python-version 3.14 --implementation cp --abi cp314 --only-binary=:all: --no-deps --dest build/license-wheels
python scripts/third_party_licenses.py --write --wheelhouse build/license-wheels
python scripts/third_party_licenses.py
```

The official pywin32 311 wheel stores licenses under `adodbapi/`, `pythonwin/`, `win32/`, and `win32com/`, rather than `dist-info/licenses/`. These files are preserved byte for byte, including adodbapi's separate license terms. `manifest.json` records the source wheel's filename and SHA-256, not its local download directory. The wheel is not distributed with the product release package.

The lockfile parser accepts only exact versions and optional equality conditions such as `sys_platform == 'win32'`. Other marker expressions produce an explicit error to avoid missing dependencies. Runtime verification checks only pins applicable to the current platform; license snapshot checks retain all platforms.
