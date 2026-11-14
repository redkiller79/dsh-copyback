# CHANGES

基于上游剪贴板历史守护进程 **clipboard-history** v1.9.0（原创实现，MIT，Copyright (c) 2026 飞哥 (FeiGe)），于 2026-11-14 起进行以下移植改造：

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

## 2026-11-14 启动自愈同步上游（v1.0.3）

- 背景：上游 WorkBuddy 技能 `clipboard-history` v1.9.1 修复「更新 / 重启宿主后工具条不启动」。该问题的两处具体成因（`SessionStart` 单点、浏览器无边框窗只试首次）都在 **WorkBuddy 宿主层**；本插件自 v1.0.1 起已剥离会话钩子 / 工具条 / 浏览器窗，**不受影响，守护进程无需改动**。
- 但同一类「首次拉起失败后就不再重试」的问题确实存在于本插件的宿主封装层 `lib/index.js` 的 `ensureDaemon()`：原来只 spawn 一次，20 秒不健康就抛错。v1.0.3 同步修复：
  - 改为 **2 轮尝试 × 10 秒**（总上限仍 20 秒），每轮重新 spawn；守护进程自带单实例锁，重复 spawn 不会叠加实例。
  - 监听子进程 `error` 事件（Python 缺失 / 被策略拦截）；此前该错误被完全吞掉，用户只看到一句裸超时。
  - 最终失败时给出可执行诊断：Python 解释器、数据目录、`runtime.json` 是否陈旧、端口范围、spawn 错误原因，以及 `DSH_COPYBACK_PYTHON` / `DSH_COPYBACK_PORT` 两个可用覆盖项。
  - 新增纯函数 `startupDiagnostics()` 与导出常量 `STARTUP_ATTEMPTS`，合同测试覆盖。
- 兼容性：三个工具、参数、数据格式、端口基线、数据目录均未变化；纯加固，升级无需迁移。

## 2026-11-14 许可信息更新（v1.0.2）

- LICENSE 版权人由 ELLA 更正为 **飞哥 (FeiGe)**，全文 MIT 条款不变；NOTICE.md / CHANGES.md / README.md / 守护进程文件头 / 插件头注释 / 自营索引 author 字段同步更正。
- README「上架状态」补充 awesome-dsh-plugin 收录申请（PR #15087），移除已失效的第三方索引申请记录；安装命令改用版本 tag 引用。

## 2026-11-14 二次剥离（v1.0.1，pure-DSH）

- 守护进程剥离全部上游宿主专用死代码（约 540 行）：WorkBuddy 进程探活/主窗定位、看门狗线程、会话钩子安装器、tkinter 探测、工具条拉起、浏览器 app 窗、全局热键监听、autostart 清理及对应 `/api/state` 字段与 CLI 子命令。
- 剥离后守护进程为纯后端：记录轮询、HTTP API、敏感格式过滤、端口降级；默认设置精简为 `recording` / `user_copy_only` 两项。
- 合同测试 6/6 通过；真实剪贴板冒烟（记录 → 搜索 → 复制回 → 置顶 → 删除）通过。

## 隔离

- 独立数据目录：Windows `%LOCALAPPDATA%/dsh-copyback`（macOS/Linux `~/.local/share/dsh-copyback`），可用 `DSH_COPYBACK_DATA` 覆盖。
- 独立端口基线：18765（冲突自动 18766～18775），可用 `DSH_COPYBACK_PORT` 覆盖。
- 非 Windows 平台首启预置 `user_copy_only: false`（属主检测在 macOS/Linux 不可用，沿用默认值会导致全部跳过）；Windows 保持「只记前台主动复制」默认。

## 未改动

- LICENSE 原文与原始版权声明（飞哥，MIT）。
