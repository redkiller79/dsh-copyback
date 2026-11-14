# dsh-copyback

DeepSeek Harness（DSH）插件：把「复制找回 · 剪贴板历史」技能封装成模型可调用的工具。本机后台记录你复制过的文字，之后随时让模型帮你找回：搜一搜、把某条重新放回剪贴板直接粘贴。

> **隐私边界**：数据只存本机（`%LOCALAPPDATA%/dsh-copyback` 或 `~/.local/share/dsh-copyback`），不上传、不联网、不读凭据；浏览器/密码管理器标记的敏感内容（Clipboard Viewer Ignore 等）自动跳过；Windows 默认只记前台应用里主动的复制动作，后台程序偷偷写入不收录。

## 能力

| 工具名 | 作用 |
| --- | --- |
| `copyback_search` | 搜索/回看剪贴板历史：关键词、条数、按日筛选（今天/昨天/具体日期）；返回时间、来源程序、置顶标记与内容预览 |
| `copyback_copy` | 把某条历史重新写入系统剪贴板，用户可直接 Ctrl+V / Cmd+V 粘贴 |
| `copyback_manage` | 管理：置顶/取消置顶、删除单条、一键清空、开始/暂停记录、查看运行状态 |

示例对话：

```
我十分钟前复制过一段服务器地址，找出来帮我放回剪贴板
把今天下午复制的那几条链接列出来，标一下哪条是置顶的
先暂停剪贴板记录，我要复制密码
```

## 安装

需要 Node.js >= 22、Python 3、DeepSeek Harness 0.2.x。**无需 npm。**

### 方式一：插件市场「发现」标签页（推荐）

1. 打开 DSH「插件」→「实验性插件」→ 启用「自定义插件索引」；
2. 进入「发现」→「添加索引源」，填入本插件官方索引：

   ```
   https://redkiller79.github.io/dsh-copyback/index.json
   ```

3. 在卡片上确认来源（`redkiller79/dsh-copyback`，版本固定在发布 commit）后安装。

### 方式二：官方 CLI 直接安装

```sh
# 从 GitHub 源码安装（纯 JS 无构建步骤，开箱即用；可锁定版本 tag）
dsh plugin --profile web add github:redkiller79/dsh-copyback#v1.0.2
# 或安装 Release 附带的构建 tarball
dsh plugin --profile web add "https://github.com/redkiller79/dsh-copyback/releases/download/v1.0.2/dsh-copyback-1.0.2.tgz"
```

安装后重启 `dsh web`（或桌面端），插件以 `dsh-copyback` 入口挂载；可用 `dsh --profile web --dump-config` 确认已加载。

卸载：

```sh
dsh plugin --profile web remove dsh-copyback
```

## 运行方式

- 插件按需拉起本地守护进程（包内自带 `scripts/clipboard_daemon.py`），只监听 `127.0.0.1`，端口基线 **18765**（被占自动 18766～18775）。守护进程为无界面纯后端：记录、HTTP API 与敏感格式过滤，不含任何 GUI。
- 首次调用任何工具时自动开启记录；之后沿用你上次的选择（用 `copyback_manage` 的 `start_recording` / `stop_recording` 切换，重启后保持）。
- 记录上限 200 条，超出自动淘汰最旧的未置顶条目；置顶永久保留。单条正文最长 20000 字符。

## 外部依赖

- **Python 3**（运行时外部依赖）：守护进程由子进程拉起，不联网。Windows 默认找 `python`、macOS/Linux 默认找 `python3`，可用环境变量 `DSH_COPYBACK_PYTHON` 指定。
- `@deepseek-ai/cordis` ^4.1.0、`@deepseek-ai/dsh-tools` >=0.0.1-rc.1：均为 **peerDependencies**，由 DSH 宿主提供；本包无 `dependencies`、无 `preinstall` / `install` / `postinstall` / `prepare` 生命周期脚本。
- 环境变量（可选）：`DSH_COPYBACK_PORT`（端口基线，默认 18765）、`DSH_COPYBACK_DATA`（数据目录）、`DSH_COPYBACK_PYTHON`（解释器路径）。

## 权限范围（如实声明）

| 类别 | 范围 |
| --- | --- |
| 剪贴板 | 轮询读取系统剪贴板文本用于历史记录（敏感格式跳过）；`copyback_copy` 会**写入**系统剪贴板 |
| 文件读写 | 仅在本插件数据目录下读写 `items.json` / `settings.json` / `runtime.json` |
| 网络 | 无（守护进程只监听 127.0.0.1 本机回环） |
| 命令执行 | 仅以子进程方式运行包内自带的 Python 守护进程，参数固定，不经过 shell |
| 凭据 | 无 |

**主要风险**：剪贴板记录天然涉及敏感文本。插件靠三层措施缓解——敏感标记格式跳过、Windows 前台复制判定、本地明文存储不出机器；如需彻底停止，用 `copyback_manage` 的 `stop_recording`，或直接删除数据目录。存储为本地明文 JSON，未做加密（与上游技能一致，如需更强保护请使用系统级加密 BitLocker/EFS）。

## 兼容性

| 项 | 声明 |
| --- | --- |
| DSH | `>=0.2.1-rc.1 <0.3.0`（声明范围，未逐版本实机验收） |
| Profile | `web` / `desktop` |
| 系统 | Windows（完整能力）；macOS（依赖 `pbpaste`/`pbcopy`）；Linux（依赖 `wl-paste`/`wl-copy`，Wayland 会话需已安装 wl-clipboard） |
| Node.js | >= 22 |
| Python | 3.x |

上述为作者声明，非第三方市场对全部版本的端到端验收结论。

## 上架状态

DeepSeek Harness 的「发现」标签页读取的是**自定义插件索引**（官方格式 1 的 JSON，挂在静态主机上）——DSH 不内置任何索引源，也没有中心化审核市场。本插件按此机制直接上架：

1. **自营官方索引（无需 npm）**：https://redkiller79.github.io/dsh-copyback/index.json —— 本仓库 `docs/` 目录经 GitHub Pages 发布，版本来源固定在发布 commit，图标随索引同源托管；
2. **awesome-dsh-plugin 收录申请**：已向社区目录 https://github.com/awesome-dsh-plugin/awesome-dsh-plugin 提交 PR（[#15087](https://github.com/awesome-dsh-plugin/awesome-dsh-plugin/pull/15087)，`Add redkiller79/dsh-copyback (tools)`）；
3. **GitHub 仓库**带 `dsh-plugin` topic，社区插件目录可自动发现；Release v1.0.2 附构建 tarball；
4. npm 包 `dsh-copyback` 为**可选**渠道（非必须；截至 2026-11-14 未发布）。

## 来源与许可

守护进程脚本来自开源 WorkBuddy 技能 `clipboard-history`（复制找回 · 剪贴板历史，MIT，Copyright (c) 2026 飞哥 (FeiGe)），本包新增 DSH 宿主插件封装层。详见 [NOTICE.md](./NOTICE.md) 与 [CHANGES.md](./CHANGES.md)、[LICENSE](./LICENSE)。
