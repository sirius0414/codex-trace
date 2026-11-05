# 更新跨平台授权快照

[English](README.md) | [简体中文](README.zh-CN.md)

发布构建只验证并复制 `manifest.json` 列出的授权原文，不依赖构建机安装了另一平台的 Python 包。`requirements.lock` 中的 `pywin32==311` 仅在 Windows 安装；授权清单始终覆盖 macOS 和 Windows 的全部锁定依赖。

按锁文件安装本机依赖后运行：

```sh
python scripts/third_party_licenses.py --write
python scripts/third_party_licenses.py
```

对于当前平台未安装的依赖，生成器会复用已有快照中名称、版本、平台条件都相符且所有文件校验通过的授权文本。新增或升级跨平台依赖时，需要先下载官方 wheel，再通过 `--wheelhouse` 读取其 METADATA 和授权文件。生成器不会安装或执行 wheel 内的代码，也不会自行联网。

例如在 macOS 上更新 Windows Python 3.14 的 pywin32 授权：

```sh
python -m pip download pywin32==311 --platform win_amd64 --python-version 3.14 --implementation cp --abi cp314 --only-binary=:all: --no-deps --dest build/license-wheels
python scripts/third_party_licenses.py --write --wheelhouse build/license-wheels
python scripts/third_party_licenses.py
```

pywin32 311 的官方 wheel 将授权放在 `adodbapi/`、`pythonwin/`、`win32/` 和 `win32com/`，而非 `dist-info/licenses/`。这些文件均按原字节保留；其中还包含 adodbapi 的独立授权条款。`manifest.json` 记录取证 wheel 的文件名与 SHA-256，不记录本机下载目录。wheel 不随产品发布包分发。

锁文件解析器仅接受精确版本，以及可选的 `sys_platform == 'win32'` 这类相等条件；其他 marker 表达式会明确报错，避免遗漏依赖。运行时校验只检查本平台适用的 pins，授权快照检查则保留所有平台。
