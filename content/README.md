# Memoh Agent 平台学习笔记索引

> 基于 [`00_学习计划.md`](00_学习计划.md) 生成，覆盖 Memoh 作为**端到端 Agent 系统**的 13 个子系统（+1 篇计划外补的可观测性）。
> 每篇笔记：实际读源码、结论带 `文件:行号`、先讲法后代码、配验证问题、互链 `[[wikilink]]`。
> 源码根目录：`/Users/mervyn/workspaces/github/Memoh`

## 阅读路线

- **路线 A — Agent 内核深挖（推荐先走）**：01 → 02 → 03 → 04 → 05 → 06
- **路线 B — 平台基建深挖**：07 → 08 → 09 → 12 → 13
- **路线 C — 系统设计面试向（快速全局）**：01（入口） → 03 → 07 → 12 → 综合

## P0 — Agent 内核与核心设计

| # | 笔记 | 子系统 | 关键产出 |
|---|---|---|---|
| 01 | [Agent 内核](01_agent_内核.md) | Stream/Generate 入口、流式事件、Prompt 组装、循环检测、Guard 状态机、重试 | 一次 Stream 调用的数据流 |
| 02 | [工具系统](02_工具系统.md) | ToolProvider/ToolUsage 接口、`assembleTools` 按需注入、守卫测试 | 「工具用法住在工具里」设计动机 |
| 03 | [长期记忆](03_长期记忆.md) | Qdrant 向量 + BM25 稀疏 + LLM 抽取多 provider | 写入/检索完整链路 |
| 04 | [对话流编排](04_对话流编排.md) | flow resolver + pipeline 双路径 | flow vs pipeline 取舍 |

## P1 — Prompt、压缩、容器、渠道、MCP

| # | 笔记 | 子系统 | 关键产出 |
|---|---|---|---|
| 05 | [Prompt 工程与模式切换](05_prompt工程与模式切换.md) | 嵌入式 Markdown 模板、五种模式、bridge persona | 五种模式适用场景差异 |
| 06 | [上下文压缩](06_上下文压缩.md) | LLM 摘要压缩、原消息打标不删 | 触发时机与原消息处理 |
| 07 | [容器 / 工作空间](07_容器工作空间.md) | gRPC over UDS bridge、docker/containerd/apple 适配、resource_limits | 端到端时序图 |
| 08 | [多渠道适配](08_多渠道适配.md) | Adapter/Descriptor/Registry 抽象、telegram/feishu 对比、identities/acl | 统一抽象层与渠道差异 |
| 09 | [MCP 集成](09_mcp集成.md) | connections/OAuth/工具网关/会话存储、stdio 桥 | go-sdk 封装得失对照 |

## P2 — 调度、扩展、存储、桌面

| # | 笔记 | 子系统 | 关键产出 |
|---|---|---|---|
| 10 | [调度与自动化](10_调度与自动化.md) | heartbeat cron + schedule 定时 | 两种模式区别与数据模型 |
| 11 | [ACP / 插件 / 用户输入 / 备份](11_acp插件用户输入.md) | 外部 Agent 池、插件生命周期、ask_user、botbackup 四策略 | ACP 运行时池与 merge 策略 |
| 12 | [数据库双后端](12_数据库双后端.md) | PostgreSQL + SQLite 双轨迁移、sqlc 双份 query | 全量基线 + 增量 diff 约定 |
| 13 | [桌面端](13_桌面端.md) | Electron 本地 server、内嵌 Qdrant、打包 CLI | 桌面 vs server 数据隔离 |
| 17 | [可观测性](17_可观测性.md) | slog 日志 + /ping 健康检查 + per-bot runtime checks + containerd 指标 + hook 事件流 | 为什么没有 OTel/Prometheus |

## 跨笔记重要修正（子代理读源码时发现）

学习计划 `00_学习计划.md` 与实际源码的几处偏差，已在对应笔记中标注：

- **05**：bridge persona 模板实际是 `AGENTS.md / HEARTBEAT.md / MEMORY.md / PROFILES.md`，非计划写的 `TOOLS.md / SOUL.md / IDENTITY.md`。
- **03**：记忆 prompt 真实路径是 `internal/memory/memllm/prompts/memory_extract.md`，非 `internal/agent/prompts/memory_extract.md`。
- **08**：`internal/bind/` 实际不存在，绑定逻辑在 `internal/channelaccess/`。
- **09**：`cmd/mcp/` 目录不存在，stdio transport 实现在 `internal/handlers/mcp_stdio.go`，stdio binary 是用户配置的 MCP 服务器跑在 bot 容器里。

## 待办

- [ ] 通读各子目录 `AGENTS.md` 建立全局地图
- [ ] 跑通本地 dev（`mise run dev` / `mise run dev:sqlite`）体验完整对话
- [ ] 按 A/B/C 路线深挖，逐篇脱稿回答验证问题
