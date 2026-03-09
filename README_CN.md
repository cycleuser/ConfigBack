# ConfigBack

**跨 Linux、macOS、Windows 平台备份和恢复开发者配置文件。**

ConfigBack 是一个单文件 Python 工具，帮助你在不同机器之间迁移开发环境配置。支持备份 pip/PyPI 设置、conda 环境、npm/yarn 配置、git 设置和 SSH 配置到一个可移植的归档文件中。

## 功能特性

- **跨平台**: 支持 Linux、macOS 和 Windows
- **一体化归档**: 基于 ZIP 的备份，按类别组织
- **CLI + GUI**: 完整的命令行界面和基于 tkinter 的图形界面
- **可选加密**: AES 加密 (Fernet) + PBKDF2 密钥派生
- **选择性备份/恢复**: 可选择特定类别
- **安全恢复**: 覆盖前自动备份现有文件 (`.bak`)
- **试运行模式**: 预览恢复操作而不实际修改文件
- **Conda 环境导出**: 自动导出 conda 环境规格

## 支持的配置

| 类别 | ID | 文件 |
|------|-----|------|
| PyPI / pip | `pip` | `pip.conf` / `pip.ini`、`.pypirc`（令牌和凭据）|
| Conda | `conda` | `.condarc`、conda 环境导出（YAML）|
| npm / yarn | `npm` | `.npmrc`、`.yarnrc`、`.yarnrc.yml` |
| Git | `git` | `.gitconfig`、`.gitignore_global` |
| SSH | `ssh` | `~/.ssh/config`、`~/.ssh/known_hosts`、私钥（需显式启用）|

## 安装

```bash
# 基本安装
pip install configback

# 带加密支持
pip install configback[encryption]
```

从源码安装：

```bash
git clone https://github.com/cycleuser/configback.git
cd configback
pip install .
```

## 快速开始

```bash
# 备份所有配置
configback backup

# 加密备份
configback backup --encrypt

# 仅备份特定类别
configback backup -c pip,git,ssh

# 查看备份内容
configback list configback_myhost_20260306_143000.zip

# 恢复前先试运行
configback restore configback_myhost_20260306_143000.zip --dry-run

# 实际恢复
configback restore configback_myhost_20260306_143000.zip

# 启动图形界面
configback gui
```

## CLI 参考

### `configback backup`

创建配置文件的备份归档。

| 参数 | 说明 |
|------|------|
| `-o`, `--output` | 输出文件路径（默认: `configback_{主机名}_{时间戳}.zip`）|
| `-e`, `--encrypt` | 加密备份归档 |
| `-p`, `--password` | 加密密码（省略则交互式输入）|
| `--include-keys` | 包含 SSH 私钥 |
| `-c`, `--categories` | 逗号分隔的类别列表: `pip,conda,npm,git,ssh` |

**输出**: 一个 `.zip` 文件（加密时为 `.zip.enc`），包含选定的配置文件和一个 `manifest.json` 元数据文件。

**示例**:
```bash
$ configback backup -c pip,git -o my_configs.zip
ConfigBack v1.0.0 - Backup
Categories: pip, git

  Backed up: pip/pip.conf
  Backed up: pip/.pypirc
  Backed up: git/.gitconfig
  Backed up: git/.gitignore_global

Backup complete: my_configs.zip
  Files: 4  Size: 2.3 KB

Success!
```

### `configback restore`

从备份归档恢复配置文件。

| 参数 | 说明 |
|------|------|
| `FILE` | 备份归档路径（必填，位置参数）|
| `-p`, `--password` | 解密密码（如归档已加密则自动检测）|
| `-c`, `--categories` | 逗号分隔的要恢复的类别 |
| `--dry-run` | 显示将要恢复的内容，但不实际修改 |
| `--force` | 跳过确认，强制覆盖现有 conda 环境 |

**输出**: 将文件恢复到平台对应的位置。覆盖前会将现有文件备份为 `.bak.{时间戳}` 后缀。

**示例**:
```bash
$ configback restore my_configs.zip --dry-run
ConfigBack v1.0.0 - Restore
[DRY-RUN MODE]

Archive from: linux (myworkstation)
Created: 2026-03-06T14:30:00+00:00
  [DRY-RUN] pip/pip.conf -> /home/user/.config/pip/pip.conf (exists)
  [DRY-RUN] pip/.pypirc -> /home/user/.pypirc (exists)
  [DRY-RUN] git/.gitconfig -> /home/user/.gitconfig (exists)

Done!
```

### `configback list`

显示备份归档的内容。

| 参数 | 说明 |
|------|------|
| `FILE` | 备份归档路径（必填，位置参数）|
| `-p`, `--password` | 解密密码 |

**示例**:
```bash
$ configback list my_configs.zip
ConfigBack Archive: my_configs.zip
  Version:   1.0.0
  Created:   2026-03-06T14:30:00+00:00
  Platform:  linux
  Hostname:  myworkstation
  Encrypted: False

  [Git]
    .gitconfig                                    1.2 KB
    .gitignore_global                             0.3 KB
  [PyPI / pip]
    pip.conf                                      0.2 KB
    .pypirc                                       0.5 KB

  Total: 4 files
```

### `configback gui`

启动图形用户界面。

GUI 提供三个选项卡：
- **Backup（备份）**: 选择类别、设置输出路径、可选加密
- **Restore（恢复）**: 浏览归档文件、选择类别、试运行选项
- **List（列表）**: 浏览和检查归档内容的树形视图

![选择项目来进行备份](./images/0-select.png)


![选择项目来进行恢复](./images/1-restore.png)

### 全局选项

| 参数 | 说明 |
|------|------|
| `--version` | 显示版本号 |
| `-v`, `--verbose` | 启用详细/调试输出 |

## 归档格式

备份归档是标准 ZIP 文件，结构如下：

```
archive.zip
├── manifest.json
├── pip/
│   ├── pip.conf
│   └── .pypirc
├── conda/
│   ├── .condarc
│   └── envs/
│       ├── base.yml
│       └── myenv.yml
├── npm/
│   ├── .npmrc
│   └── .yarnrc
├── git/
│   ├── .gitconfig
│   └── .gitignore_global
└── ssh/
    ├── config
    └── known_hosts
```

`manifest.json` 包含：
- `configback_version`: 创建归档的工具版本
- `timestamp`: 创建时间（ISO 8601）
- `platform`: 源操作系统（`linux`、`darwin`、`win32`）
- `hostname`: 源机器名
- `encrypted`: 是否加密
- `categories`: 类别 -> 已归档文件路径的映射

## 加密

使用 `--encrypt` 时，归档使用以下方式加密：
- **算法**: AES-128-CBC（通过 Fernet）
- **密钥派生**: PBKDF2-HMAC-SHA256，480,000 次迭代
- **盐值**: 16 字节，每个归档随机生成

加密文件有 `CFGBAK01` 魔术头用于自动检测。

需要安装 `cryptography` 包：
```bash
pip install cryptography
```

## 跨平台路径映射

ConfigBack 自动映射不同平台间的配置文件路径：

| 配置 | Linux | macOS | Windows |
|------|-------|-------|---------|
| pip 配置 | `~/.config/pip/pip.conf` | `~/Library/Application Support/pip/pip.conf` | `%APPDATA%\pip\pip.ini` |
| .pypirc | `~/.pypirc` | `~/.pypirc` | `%USERPROFILE%\.pypirc` |
| .condarc | `~/.condarc` | `~/.condarc` | `%USERPROFILE%\.condarc` |
| .npmrc | `~/.npmrc` | `~/.npmrc` | `%USERPROFILE%\.npmrc` |
| .gitconfig | `~/.gitconfig` | `~/.gitconfig` | `%USERPROFILE%\.gitconfig` |
| SSH 配置 | `~/.ssh/config` | `~/.ssh/config` | `%USERPROFILE%\.ssh\config` |

## 安全说明

- **SSH 私钥** 默认不包含。需要显式使用 `--include-keys`。
- 加密归档使用强密钥派生（PBKDF2，480k 次迭代）。
- 在共享系统上避免通过 `--password` 传递密码（在进程列表中可见）。请使用交互式提示。
- `.pypirc` 文件可能包含 PyPI 上传令牌。请妥善处理备份文件。

## 发布到 PyPI

包含上传脚本：

```bash
# Linux / macOS
./upload_pypi.sh

# Windows
upload_pypi.bat

# 先上传到 TestPyPI
./upload_pypi.sh --test
```

## 许可证

MIT 许可证。详见 [LICENSE](LICENSE)。
