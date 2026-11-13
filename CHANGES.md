# CHANGES

基于 WorkBuddy 技能 **clipboard-history（复制找回 · 剪贴板历史）** v1.9.0（原创实现，MIT，Copyright (c) 2026 ELLA），于 2026-11-14 起进行以下移植改造：

## 保留（未改动）

- `scripts/clipboard_daemon.py`：守护进程核心逻辑原样复用——剪贴板轮询、敏感格式跳过（Clipboard Viewer Ignore 等）、来源进程标注、置顶/删除/清空、忽略集合防复活、端口自动降级（8765 基线 + PORT_FALLBACK）、令牌鉴权 HTTP API。版权行与注释保留。

## 新增（DSH 宿主层）

- `lib/index.js`：`defineTool` 三工具 `copyback_search` / `copyback_copy` / `copyback_manage`；守护进程按需拉起（runtime.json 就绪等待、健康探测、首次运行自动开启记录）；搜索结果有界化（每条 400 字符预览）。
- `cordis.patch.yml`、`package.json`（dsh bundle/compatibility 声明）、`test/contract.test.mjs`。

## 移除（不随 DSH 版分发）

- 浮动工具条 `clipboard_strip.py`（tkinter GUI）与网页工作台 `web/`：宿主场景由模型工具调用替代交互界面。
- WorkBuddy 会话钩子、`session_boot.py` 监督进程、WorkBuddy 看门狗（`--follow-workbuddy` 不传即不启用）。
- 全局热键依赖：守护进程仍会尝试注册 Win+Alt+V，失败不影响工具调用（`hotkey_ok` 仅状态位）。

## 隔离与并存

- 独立数据目录：Windows `%LOCALAPPDATA%/dsh-copyback`（macOS/Linux `~/.local/share/dsh-copyback`），可用 `DSH_COPYBACK_DATA` 覆盖。
- 独立端口基线：18765（冲突自动 18766～18775），与 WorkBuddy 版（8765 基线）互不干扰，可同时运行。
- 非 Windows 平台首启预置 `user_copy_only: false`（属主检测在 macOS/Linux 不可用，沿用默认值会导致全部跳过）；Windows 保持「只记前台主动复制」默认。

## 未改动

- LICENSE 原文与原始版权声明（ELLA，MIT）。
