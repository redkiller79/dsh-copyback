# CHANGES

基于上游剪贴板历史守护进程 **clipboard-history** v1.9.0（原创实现，MIT，Copyright (c) 2026 ELLA），于 2026-11-14 起进行以下移植改造：

## 保留（未改动）

- `scripts/clipboard_daemon.py` 核心逻辑——剪贴板轮询、敏感格式跳过（Clipboard Viewer Ignore 等）、来源进程标注、置顶/删除/清空、忽略集合防复活、端口自动降级（基线 + PORT_FALLBACK）、令牌鉴权 HTTP API。版权行保留。

## 新增（DSH 宿主层）

- `lib/index.js`：`defineTool` 三工具 `copyback_search` / `copyback_copy` / `copyback_manage`；守护进程按需拉起（runtime.json 就绪等待、健康探测、首次运行自动开启记录）；搜索结果有界化（每条 400 字符预览）。
- `cordis.patch.yml`、`package.json`（dsh bundle/compatibility 声明）、`test/contract.test.mjs`。

## 移除（不随 DSH 版分发）

- 浮动工具条 `clipboard_strip.py`（tkinter GUI）、网页工作台 `web/` 与无边框浏览器窗口：宿主场景由模型工具调用替代交互界面。
- WorkBuddy 会话钩子（SessionStart）、`session_boot.py` 监督进程、WorkBuddy 进程看门狗（`--follow-workbuddy`）。
- 全局热键（RegisterHotKey / Win+Alt+V）与 `summon` 通道：无界面即无唤出目标。
- 登录自启（autostart）清理逻辑与 `install-hook` / `uninstall-hook` / `strip` / `open` / `summon` CLI 子命令。

## 2026-11-14 二次剥离（v1.0.1，pure-DSH）

- 守护进程剥离全部上游宿主专用死代码（约 540 行）：WorkBuddy 进程探活/主窗定位、看门狗线程、会话钩子安装器、tkinter 探测、工具条拉起、浏览器 app 窗、全局热键监听、autostart 清理及对应 `/api/state` 字段与 CLI 子命令。
- 剥离后守护进程为纯后端：记录轮询、HTTP API、敏感格式过滤、端口降级；默认设置精简为 `recording` / `user_copy_only` 两项。
- 合同测试 6/6 通过；真实剪贴板冒烟（记录 → 搜索 → 复制回 → 置顶 → 删除）通过。

## 隔离

- 独立数据目录：Windows `%LOCALAPPDATA%/dsh-copyback`（macOS/Linux `~/.local/share/dsh-copyback`），可用 `DSH_COPYBACK_DATA` 覆盖。
- 独立端口基线：18765（冲突自动 18766～18775），可用 `DSH_COPYBACK_PORT` 覆盖。
- 非 Windows 平台首启预置 `user_copy_only: false`（属主检测在 macOS/Linux 不可用，沿用默认值会导致全部跳过）；Windows 保持「只记前台主动复制」默认。

## 未改动

- LICENSE 原文与原始版权声明（ELLA，MIT）。
