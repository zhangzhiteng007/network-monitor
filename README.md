# 网络监控中心 NetMonitor

Windows 11 单文件绿色便携网络工具：免安装、不写注册表、不依赖运行库，拷贝即用。

![screenshot](docs/screenshot.png)

当前版本：见 `VERSION` · [更新日志](CHANGELOG.md)

## 功能

| 页签 | 能力 |
|---|---|
| 网卡与 IP | 实时显示所有网卡状态、IPv4/IPv6 地址、前缀、默认网关、跃点数、链路速率、上下行流量；支持一键内外网优先切换 |
| IP 连通检测 | 自定义设备名 + IP 逐个/批量添加，ICMP 探测在线状态与延迟，并检测「Web 可达」（DNS → TCP 443 → HTTPS 状态码），支持批量导入文本或 CSV，每 10 秒自动检测 |
| 端口状态 | 全部 TCP/UDP 端口的本地/远端地址、状态、PID、进程名；支持搜索、协议筛选、只显示监听、右键复制 |
| 端口探活 | 自定义 `主机:端口` 目标做 TCP 探测，记录延迟，目标持久化 |

## 快速开始

### 方式一：下载 exe（推荐）

在 [Releases](../../releases) 页面下载最新的 `网络监控中心_vX.Y.Z.exe`，双击运行即可，无需安装。

### 方式二：从源码运行

需要 Python 3.10+（Windows）：

```bash
pip install -r requirements.txt
python src/netmon.py
```

### 方式三：打包成单文件 exe

```bash
pip install -r requirements.txt
python build.py 1.3.2   # 传入目标版本号
```

产物在 `dist/网络监控中心.exe`，并自动归档到 `dist/archive/vX.Y.Z/`。

## 版本管理

| 文件 | 作用 |
|---|---|
| `VERSION` | 当前版本号，唯一版本源 |
| `CHANGELOG.md` | 每个版本的变更记录（人可读） |
| `build.py` | 一键构建：同步版本号 → 关闭旧进程 → 打包 → 归档到 `dist/archive/vX.Y.Z/` |
| `.git` | 源码版本库，每个发布版本打标签 |

版本号规则：`主版本.次版本.修订号`（修复 +修订号 / 新功能 +次版本 / 架构变更 +主版本）。

### 发布一个新版本

```bash
# 1. 改完代码后，在 CHANGELOG.md 顶部加一段本版本变更

# 2. 构建（自动把 VERSION 和源码里的 APP_VER 一起改成 1.3.0）
python build.py 1.3.0

# 3. 提交并打标签
git add -A
git commit -m "feat: v1.3.0 一句话说明"
git tag -a v1.3.0 -m "v1.3.0 摘要"

# 4. 查看历史
git log --oneline --decorate
```

构建产物：
- `dist/网络监控中心.exe` —— 最新版，直接使用
- `dist/archive/v1.3.0/网络监控中心_v1.3.0.exe` —— 按版本归档，可随时回退

### 回退到历史版本

```bash
git checkout v1.2.0          # 查看旧源码
dist/archive/v1.2.0/网络监控中心_v1.2.0.exe   # 或直接用归档的旧 exe
```

## 目录结构

```
├─ src/
│  ├─ netmon.py      主程序（界面、线程、业务逻辑）
│  ├─ netinfo.py     底层采集：IP Helper API / netsh / psutil / ICMP
│  └─ icon.ico       程序图标
├─ tools/
│  ├─ make_icon.py   生成图标
│  ├─ shot.py        窗口截图（开发调试用）
│  └─ preview.py     按页签预览（开发调试用）
├─ docs/             文档资源（截图）
├─ dist/             构建产物与历史版本归档（不入库）
├─ build.py          构建脚本
├─ requirements.txt  依赖清单
├─ VERSION           版本号
├─ CHANGELOG.md      变更日志
└─ LICENSE           MIT 许可证
```

## 配置文件

exe 同目录的 `config.json`（若目录不可写则回退到 `%LOCALAPPDATA%\NetMonitor\`），保存：

- `interval`：刷新间隔（秒）
- `switch`：内外网识别规则（内网网卡名/IP、外网网卡名、优先/非优先跃点值）
- `ip_list`：IP 检测列表（设备名、IP、备注）
- `ip_auto_check`：是否每 10 秒自动检测
- `probes`：端口探活目标

删除该文件即恢复默认设置。

## 注意事项

- **修改跃点数 / 内外网切换**需要管理员权限；非管理员运行时界面右上角显示「受限模式」，点击可 UAC 提权重启。
- **IP 连通检测与端口探活**不需要管理员权限。
- 单文件 exe 由 PyInstaller 打包，个别杀毒软件可能误报，添加信任即可。
- IP 检测使用 ICMP，若目标主机防火墙禁 Ping，会显示离线但业务端口可能仍可用（可用「端口探活」进一步确认）。

## 注意事项

- **修改跃点数 / 内外网切换**需要管理员权限；非管理员运行时界面右上角显示「受限模式」，点击可 UAC 提权重启。
- **IP 连通检测与端口探活**不需要管理员权限。
- 单文件 exe 由 PyInstaller 打包，个别杀毒软件可能误报，添加信任即可。
- IP 检测使用 ICMP，若目标主机防火墙禁 Ping，会显示离线但业务端口可能仍可用（可用「端口探活」进一步确认）。
- 运行时生成的 `config.json` 含本机网络信息，已加入 `.gitignore`，请勿提交到公共仓库。

## 许可证

[MIT](LICENSE)
