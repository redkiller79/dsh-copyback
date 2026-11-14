# NOTICE

## 本包构成

- **复用部分**：`scripts/clipboard_daemon.py` —— 本地剪贴板历史守护进程（记录、轮询、敏感格式过滤、置顶/删除/清空、端口自动降级），来自开源 WorkBuddy 技能 **clipboard-history（复制找回 · 剪贴板历史）**，MIT 许可证，Copyright (c) 2026 飞哥 (FeiGe)。原许可证全文保留在包根 `LICENSE`，未删改版权行。
- **新增部分**：`lib/index.js`（DSH 宿主插件封装、`defineTool` 工具注册）、`cordis.patch.yml`、`package.json`、`README.md`、`test/`。由本仓库作者编写，MIT。

## 改动说明（详见 CHANGES.md）

1. 把原技能「随 WorkBuddy 启动、浮动工具条交互」的使用方式，封装为 DSH 原生 `ctx.tools.register` 工具，使模型可直接调用「找回 / 复制回 / 管理」三类能力。
2. 插件使用**独立数据目录与独立端口基线**（`%LOCALAPPDATA%/dsh-copyback`，端口 18765 起），与 WorkBuddy 版互不冲突、可并存。
3. 精简为无 GUI：不含浮动工具条（tkinter）、网页工作台与 WorkBuddy 会话钩子；守护进程由插件按需拉起。
4. 搜索结果做上下文裁剪（每条仅回传预览与元信息），复制全文走 `copyback_copy` 写入系统剪贴板。

## 第三方依赖

| 包 | 许可证 | 用途 |
| --- | --- | --- |
| `@deepseek-ai/cordis` | MIT | DSH 插件上下文（peer） |
| `@deepseek-ai/dsh-tools` | MIT | `defineTool` 工具定义（peer） |
| Python 3 | PSF License | 运行包内守护进程脚本（外部运行时，非 npm 依赖） |

本包 `dependencies` 为空，无安装期生命周期脚本，无原生构建产物。

## 权属

未抹除上游版权声明。上游 MIT 许可允许复制、修改、发布、分发与商用；本文件用于保留来源与举证链。
