# 11 — ACP / 插件 / 用户输入 / 备份

> **1 分钟叙事**：Memoh 不只是一个自己跑 LLM 的 Agent，它还想把「外部 Agent 进程」也纳入自己的治理体系。这四个子系统共同支撑一个能力——**让 Bot 的能力边界可扩展、可治理、可迁移**。
>
> - **ACP 运行时池**（acpagent/acpclient/acpprofile）回答：如何把 Claude Code、Codex 这些独立 Agent CLI 当作可池化的「运行时」来管理——冷启动、绑定到会话、空闲回收、跨会话复用，同时把它们的工具调用全部收进 Memoh 的审批/MCP 网关。
> - **plugins** 回答：如何让一个 Bot 像 IDE 装插件一样，按 manifest 声明式地装上一组 MCP 连接 + Skills，并管理它们的配置/OAuth/启停生命周期。
> - **userinput**（ask_user 工具）回答：Agent 在对话中途需要向用户提问时，如何把「阻塞等待一个结构化回答」做成一个可超时、可取消、跨进程一致的工具。
> - **botbackup** 回答：如何把一个 Bot 的全部资产（配置/模型/ACL/渠道/MCP/历史/工作区文件）打包成一个 zip，再以 preview/merge/replace/skip 四种策略导入到另一个 Bot，且不泄漏 ACP 密钥。
>
> 这四个子系统共享同一个治理思想：**外部能力无论从哪个入口进来（ACP 进程、插件 manifest、ask_user、备份导入），最终都要经过 Memoh 的工具网关和审批流，不能绕过**。

## 核心结论

1. **ACP 运行时 = OS 进程 + 协议状态，不持久化**：runtimeHandle 是一个 Agent 进程的唯一拥有者（session_pool.go:120-143），重启后冷启动即可，会话数据在 DB 里。这决定了池的设计是「内存态、可重建」。

2. **两层 idle 回收 + 每 bot 预算**：绑定到会话的 runtime 30 分钟空闲回收，未绑定的（预会话模型选择器）5 分钟回收，且每 bot 最多 4 个未绑定 runtime（session_pool.go:39-45）。这是防资源泄漏的三道闸。

3. **ACP 工具调用走「一次性授权消费」**：Agent 先发 RequestPermission，用户审批后授权被记成一条带 TTL 的 grant，随后 clientCallbacks 真正执行工具时消费这条 grant（client.go:568-610）。用户只看到一次审批，不是两次。

4. **三个 Agent Profile 用 init() 注册**：Codex / Claude Code / Hermes 各自定义命令、参数、托管字段、quirks（profile.go:171-329）。Claude Code 强制 pin session mode 为 "default"，否则 host 的 `~/.claude/settings.json` 会绕过审批（profile.go:230）。

5. **插件状态机是安装生命周期的骨架**：ready / needs_config / needs_auth / admin_required / disabled / uninstalled（types.go:11-17）。Install 时按 manifest 评估初始状态，SetEnabled 时重新评估并刷新 OAuth，Uninstall 保留记录，Purge 彻底删除。

6. **ask_user 是「DB 为真相源 + 进程内 Waiter」的阻塞工具**：CreatePending 写 DB，RegisterWaiter 在进程内注册，Submit/Cancel 通过 notifyResolved 唤醒等待者（service.go:78-102）。跨进程一致性靠 DB，进程内快速路径靠 Waiter。

7. **ask_user 的 flow.go 是共享状态机**：create → emit → wait → 超时则 cancel，四种 reason 可定制（flow.go:42-118）。DefaultWaitTimeout=10 分钟（flow.go:12）。

8. **botbackup 四种策略是 per-section 的**：skip（不导入）/ merge（upsert，保留目标已有）/ replace（先 clear 再导入）/ profile 恒导入（types.go:52-125）。create 模式失败有补偿——删除整个新建 Bot（import.go:524-533）。

9. **ACP 密钥在导出时 scrub、导入时再 scrub 一次**：ScrubMetadataForExport 删除敏感字段（profile.go:475-506），导出和导入各调一次，用户必须重新输入 API key（service.go:144-147 + import.go:488）。

## ACP：外部 Agent 运行时池（acpagent/acpclient/acpprofile）

### 池的设计：runtime vs session

`SessionPool` 是进程内唯一的 runtime 拥有者（session_pool.go:80-94）。两个索引：
- `runtimes map[string]*runtimeHandle`：runtimeID → handle
- `bySession map[string]string`：sessionID → runtimeID（二级索引）

关键设计决策（session_pool.go:1-9 注释）：**runtime 是「first-class」的代码抽象和生命周期归属，但不持久化**。重启后下一次 prompt 直接冷启动新 runtime。会话本身在 DB 里，所以 runtime 丢了不丢对话历史。

### runtimeHandle：一个进程的唯一拥有者

runtimeHandle（session_pool.go:120-143）持有：
- 稳定身份：id、toolToken、botID、agentID、projectPath、runtimeOwnerAccountID
- `op sync.Mutex`：串行化该 runtime 上的所有操作（prompt/set-model/bind/close）
- `state sync.Mutex`：叶子锁，保护可变快照（session/status/lastActive/boundSession/active）

锁层级（session_pool.go:74-79 注释明确）：`p.mu`（池级 map）→ `handle.op`（操作串行化）→ `handle.state`（叶子）。**p.mu 可以在持有 handle.state 时获取（预算扫描），但反过来不行**。

### 三道资源闸

```
boundRuntimeIdleTimeout   = 30 min  // 绑定到会话的 runtime
unboundRuntimeIdleTimeout = 5 min   // 预会话模型选择器
maxUnboundRuntimesPerBot  = 4       // 每 bot 最多 4 个未绑定 runtime
```

（session_pool.go:39-45）

回收器 `StartReaper` 每分钟跑一次 `reapIdle`（session_pool.go:926-942）。`tryCloseIdle` 用 `TryLock` 保证不阻塞正在服务的 runtime（session_pool.go:1008-1025）。

### 租户门：owned()

`owned(botID, runtimeID)` 是所有 runtime 操作的唯一入口（session_pool.go:262-275）。**跨 bot 引用等同于 missing——零副作用、零存在性泄漏**。这是安全基础：一个 Bot 永远无法操纵另一个 Bot 的 runtime。

### 生命周期：Create → Bind → Prompt → Close

1. **CreateRuntime**（session_pool.go:279-333）：创建未绑定 runtime，先 `reapIdle`，再 `unboundBudgetLocked` 检查预算，超了就淘汰最旧的 idle runtime。
2. **BindRuntime**（session_pool.go:377-422）：把未绑定 runtime 绑定到新创建的 chat session。条件：未关闭、有 session、agentID/projectPath/owner 全匹配。失败返回 `ErrRuntimeBindRejected`，调用方回退冷启动。
3. **Prompt**（session_pool.go:534-563）：解析会话元数据 → `runtimeForSession`（找不到就冷启动并绑定）→ `promptOnHandle`。有 3 次重试，应对「resolve 后 handle 被并发销毁」的竞态。
4. **closeHandle**（session_pool.go:963-1002）：先标记 closed、取消 startCancel，再等 op 锁。这样卡在 ACP 审批或 ask_user 上的 prompt 能迅速 unwind。

### ACP 客户端：Session 与回调

`acpclient.Runner` 是入口（client.go:51-92）。`Run` 是单次交换的便捷方法，`StartSession` 创建持久会话。

`Session`（session.go:76-95）封装一个 ACP 会话，持有 proc（进程）、callbacks（ACP 回调实现）、conn（协议连接）。`PromptWithToolContextOptions`（session.go:631-712）是核心：
- 绑定请求级工具身份到 `clientCallbacks.setPromptState`
- 用 `promptMu` 保证同一 session 上 prompt 串行
- 用 `promptToken` 防止旧 prompt 的清理逻辑误清新 prompt

### 一次性授权消费

这是 ACP 工具审批的关键机制（client.go:423-610）：

1. Agent 调工具前先发 `RequestPermission`（client.go:423）
2. Memoh 走 `requireToolApproval` → 用户审批
3. 审批通过后 `rememberApprovalGrant` 存一条带 TTL（10分钟）的 grant（client.go:568-585）
4. Agent 随后调 client capability（如 WriteTextFile），`approveCallbackTool` 消费这条 grant（client.go:522-538）
5. 用户只看到一次审批，不是「permission + execution」两次

grant 的 key 按 toolName + 规范化 input 计算（client.go:612-646），确保 permission 和 callback 匹配同一个操作。

### Profile 注册与 quirks

三个 Profile 用 `init()` 注册（profile.go:171-175）：

| Agent | Command | SessionModeID | 特点 |
|---|---|---|---|
| Codex | `codex-acp` | — | 支持 api_key/oauth/self |
| Claude Code | `claude-agent-acp` | `default` | 强制走 ACP 审批；pin `effort=high` |
| Hermes | `hermes-acp` | — | ForceHTTPMCPServer；自定义 ToolQuirks |

Claude Code 的 `SessionModeID: "default"`（profile.go:230）注释解释：不 pin 的话，host 的 `~/.claude/settings.json` 里 `defaultMode: auto/acceptEdits` 会静默绕过 Memoh 审批流。

`ToolQuirks`（quirks.go:13-21）是「agent 措辞 → 规范工具身份」的唯一映射点。当 Agent 升级改了标题，调 profile 而不是改共享映射代码。

## plugins：插件生命周期

### Manifest 与状态机

Manifest（types.go:109-126）声明一个插件的所有资源：
- `MCPs`：MCP 连接（command/url/headers/env/auth_ref）
- `Skills`：引用型 skill（path）
- `BundledSkills`：内嵌型 skill（content 直接写入容器）
- `Variables` / `AuthRequirements`：配置变量与 OAuth 需求

状态机（types.go:11-17）：
```
ready → disabled → ready          (SetEnabled)
ready → uninstalled → (purge)     (Uninstall/Purge)
needs_config → ready              (补齐变量后 SetEnabled)
needs_auth → ready                (OAuth 完成后 RefreshOAuthStatus)
admin_required                    (OAuth client 未配置，需管理员)
```

### Install 流程（service.go:82-201）

1. `normalizeManifest`：规整 ID/Name/Version，补默认值
2. `evaluateInitialStatus`：按 manifest 评估初始状态（service.go:600-620）
   - 有 managed_oauth 且 OAuth client 不可用 → `admin_required`
   - 有 managed_oauth → `needs_auth`
   - 缺必需变量 → `needs_config`
   - 否则 → `ready`
3. 写 DB（`CreateBotPluginInstallation`）
4. 为每个 MCP 资源调 `mcpService.CreateManaged`，`active = enabled && auth != managed_oauth`
5. 为每个 Skill 写 `UpsertBotPluginResource`
6. `installBundledSkills`：把 BundledSkills 写入容器的 managed 目录，每个 skill 附 `.memoh-plugin-owner.json` 标记归属（service.go:461-498）

### 启停与卸载

- **SetEnabled(false)**（service.go:208-217）：`SetPluginConnectionsActive(false)` 停所有 MCP 连接 → 状态置 `disabled`
- **SetEnabled(true)**（service.go:219-248）：重新评估状态、刷新 OAuth、检查 ready、激活连接
- **Uninstall**（service.go:250-269）：停 MCP、删 bundled skills、删资源、状态置 `uninstalled`（保留记录）
- **Purge**（service.go:271-297）：Uninstall 基础上再 `DeleteBotPluginInstallation`，彻底删除

### Bundled Skills 的归属保护

`canDeletePluginSkill`（service.go:541-554）读 `.memoh-plugin-owner.json`，只有 installationID 匹配才允许删除。这防止插件 A 卸载时误删插件 B 写的同名 skill。

## userinput：ask_user 工具

### 问题模型

UIPayload v2（types.go:115-133）是唯一规范格式：
- 最多 4 个问题（`MaxQuestionsPerRequest = 4`）
- 三种 kind：`single_select` / `multi_select` / `text`
- select 类每题 2-20 个选项
- 问题/选项 ID 由 server 生成（`q1`/`q1_o1`，payload.go:77）

`ParseAskUserPayload`（payload.go:26-54）是唯一写入侧入口，严格校验：无别名、无推断、拒绝未知 key。

### 阻塞等待：DB 真相源 + 进程内 Waiter

`Service` 持有 `decision.Waiter[Request]`（service.go:33-44），这是进程内的快速通知通道。但跨进程一致性靠 DB 状态：

- `CreatePending`（service.go:104-217）：写 DB，返回 pending 请求
- `RegisterWaiter`（service.go:51-56）：在 announce 之前注册，否则即时回答会被误判为孤儿
- `WaitForResponse`（service.go:318-322）：`waiter.Await` 阻塞，DB 轮询作安全网（service.go:330-346）
- `Submit`（service.go:359-392）：`submittedResult` 严格校验每个问题必答 → 写 DB → `resolveAndNotify` 唤醒
- `Cancel`（service.go:394-417）：写取消结果 → 唤醒

`CanRespond`（service.go:68-76）揭示一个重要区分：ACP/MCP 请求由进程内 waiter 消费，所以「DB 有 pending 行」不够，还得「本进程有 waiter」。UI 只对当前进程能响应的请求展示操作。

### 共享状态机：RunFlow

`flow.go:42-118` 是 native tool runtime 共享的 ask_user 状态机：

```
CreatePending
  ├─ !interactive → Cancel(非交互原因)
  └─ interactive → RegisterWaiter → emit
                      ├─ emit 失败 → Cancel(未投递原因)
                      └─ WaitForRegisteredResponse(超时 waitTimeout)
                            ├─ 成功 → emit(resolved)
                            ├─ 超时 → Cancel(超时原因)
                            └─ 其他错误 → Cancel(中止原因)
```

四个 reason 字段（NonInteractiveReason/UndeliveredReason/TimeoutReason/AbortReason）可由调用方定制，默认值在 flow.go:136-141。

### ACP runtime 关闭时的清理

`CancelPendingForSession`（service.go:419-450）批量取消一个 session 的所有 pending 请求。`SessionPool.closeHandle` 在关闭 runtime 时调它（session_pool.go:1088-1095），确保卡在 ask_user 上的 prompt 不会悬空。

## botbackup：Bot 导入导出（preview/merge/replace/skip）

### Section 与策略

10 个 Section（types.go:20-32）：profile（恒导入）/ settings / models / acl / channels / mcp / schedules / email / history / assets / workspace。

三种策略（types.go:55-59）：
- **skip**：不导入该 section
- **merge**：upsert，保留目标已有项（默认）
- **replace**：先 clear 目标，再导入

`strategyFor`（types.go:108-120）的回退逻辑：nil map → 全 merge；section 不在 map 里或映射到 skip → skip；其他非 replace → merge。

### Export 流程（service.go:129-249）

1. `pauseBotForExport`：暂停 bot（isActive=false），defer 恢复
2. `collect`：收集 profile/settings/acl/channels/mcp/schedules/email/dependencies/history
3. `scrubBotACPSecretsForBackup`：调 `acpprofile.ScrubMetadataForExport` 删除 ACP 托管密钥，加 warning
4. 写 zip：每条 entry 用 `io.TeeReader` 同时算 sha256 校验和
5. workspace 以 `workspace/data.tar.gz` 单条目原样存入，用 `zip.Store`（已压缩，避免双重压缩）（service.go:592-610）

### Import 流程（import.go:461-541）

1. `decodeBundle`：透明解密（支持 passphrase 加密）
2. `loadManifest`：校验 schema version
3. `scrubImportedProfileACPSecrets`：再 scrub 一次，加 warning
4. `importDependencies`：providers/models/search/fetch/memory/email 是全局幂等资源，按名字复用，**不回滚**
5. `restoreBot`：create 模式新建 bot；overwrite 模式用目标 bot
6. 若 overwrite 且有 ACP runtime → `closeBotACPRuntimes`（import.go:856-873）
7. `applyRestore`：按 section 依次恢复

### applyRestore 的策略执行（import.go:567-656）

每个 section 的通用模式：
```go
if opts.strategyFor(section) == StrategyReplace {
    s.clearXxx(ctx, targetBotID)  // 先清空目标
}
s.restoreXxx(ctx, targetBotID, state)  // 再导入
```

`restoreSettings`（import.go:875-989）更精细：跳过的组回退到目标 bot 当前值。例如只导 models 不导 settings，则 language/timezone/acl 等保持目标原值，只换模型 ID。

### create 模式的补偿

create 模式下任何 section 失败都是 fatal，触发补偿——删除整个新建 bot（import.go:524-533）。因为新建 bot 没有历史数据可保留，部分导入只会留下残缺状态。overwrite 模式则把 item 级失败降级为 warning（import.go:97-103）。

## 设计动机与取舍

1. **runtime 不持久化 vs session 持久化**：ACP Agent 进程状态复杂（stdio/JSON-RPC/思考链），持久化代价高且易出错。Memoh 选择「runtime 易失、session 持久」，重启代价是下一次 prompt 冷启动 2-3 秒，可接受。

2. **一次性授权消费 vs 双重审批**：ACP 协议设计上 permission 和 execution 是两步，但用户视角是一次决定。`approvalGrantKey` 用规范化 input 做匹配键，确保 grant 只被对应操作消费。TTL 10 分钟防止 grant 永久悬空。

3. **插件 Bundled Skills 写容器而非 DB**：Skills 是文件型资源（SKILL.md + 附属文件），写容器让 Agent 直接可读，不需要 DB→文件的中转。代价是容器不可达时无法安装——`installBundledSkills` 直接报错（service.go:466-468）。

4. **ask_user 用 DB 而非纯内存 channel**：Agent 调 ask_user 后会阻塞等待，如果只走内存 channel，进程崩溃就丢请求。DB 持久化 + Waiter 快速通知是「可靠 + 快」的折中。`resolvedAfterContextDone`（service.go:348-357）处理「resolution 在通知前到达」的竞态。

5. **botbackup 的 section 粒度而非全量**：用户常只想迁移部分数据（如只搬历史不搬 MCP）。per-section 策略让导入是组合式的，`strategyFor` 的 nil→全 merge 默认让「不指定 = 安全合并」。

6. **ACP 密钥双重 scrub**：导出时 scrub 防泄漏，导入时再 scrub 防止「备份被篡改注入密钥」。用户必须重新输入，不信任备份里的密钥字段。

## 验证问题

1. **说清 ACP 运行时池如何管理 Claude Code/Codex**：SessionPool 用 runtimeHandle 拥有进程，bound/unbound 两种 idle 回收（30/5 分钟），每 bot 最多 4 个 unbound runtime。Prompt 时 `runtimeForSession` 找不到就冷启动并绑定。跨 bot 引用通过 `owned()` 租户门零泄漏。三个 Profile 用 init() 注册，Claude Code 强制 pin session mode "default" 防止 host 配置绕过审批。

2. **说清 ask_user 如何在对话中等待用户回答**：Agent 调 ask_user → `RunFlow` 走 `CreatePending`（写 DB）→ `RegisterWaiter`（进程内注册）→ `emit`（推 UI）→ `WaitForRegisteredResponse`（阻塞，默认 10 分钟超时）。用户回答走 `Submit` → `submittedResult` 严格校验 → 写 DB → `resolveAndNotify` 唤醒等待者。超时/中止走 `Cancel`。ACP runtime 关闭时 `CancelPendingForSession` 批量清理。

3. **说清 botbackup 四种 merge 策略**：skip（不导入）/ merge（upsert，保留目标已有，默认）/ replace（先 clearXxx 再导入）/ profile（恒导入，不可选）。`strategyFor` 处理 nil map→全 merge、缺失 key→skip。create 模式失败有补偿删 bot，overwrite 模式 item 失败降级 warning。

4. **ACP 一次性授权消费为什么需要 TTL**：用户审批后 grant 被记下，但 Agent 可能迟迟不执行（崩溃、改主意）。TTL 10 分钟（client.go:34, 583）防止 grant 永久占用，也防止 Agent 后来突然消费一个用户早已忘记的授权。

5. **插件 Bundled Skills 为什么要带 `.memoh-plugin-owner.json`**：多个插件可能装同名 skill（如都叫 "search"）。卸载插件 A 时，`canDeletePluginSkill` 读 owner 文件确认 installationID 匹配才删，避免误删插件 B 的 skill（service.go:541-554）。

## 待学

- `acpclient/process.go`：bridgeProcess 如何启动 Agent CLI 进程、管理 stdio、处理容器后端的进程注入
- `acpclient/events.go`：ACP 协议事件到 Memoh StreamEvent 的映射，特别是 thinking block 的处理
- `acpclient/hermes_config.go` / `codex_config.go`：托管模式如何写 Agent 的配置文件（CODEX_HOME / Claude settings）
- `botbackup/secure/secure.go`：passphrase 加密的实现（Age? AES-GCM?）
- `plugins/payload.go` / `flow.go`：插件安装的 HTTP API 层和流程编排
- `toolapproval` 包：ACP 审批和插件 OAuth 依赖的审批服务，与 ask_user 的 Waiter 是否共享基础设施

## Connections

- [[02_工具系统]] — ACP 工具调用、插件 MCP 连接、ask_user 最终都汇聚到 Memoh 的工具网关和审批流
- [[09_mcp集成]] — 插件安装的核心动作就是创建托管 MCP 连接；ACP runtime 通过 HTTP MCP bridge 暴露 Memoh 工具
- [[07_容器工作空间]] — 插件 Bundled Skills 写入容器 managed 目录；botbackup 的 workspace section 是容器 /data 的 tar.gz
