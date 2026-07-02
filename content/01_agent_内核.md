# 01 — Agent 内核

> **1 分钟叙事**：Memoh 的 Agent 内核要回答一个核心问题——如何把一次「用户发消息 → LLM 思考 → 调工具 → 再思考 → … → 回复」的多轮 Agent 循环，可靠地流式吐给上层（IM/Discuss/Web），同时在出问题时优雅降级而不是卡死。
>
> 它的设计哲学是「**薄封装 + 重守卫**」：底层把 LLM 交互全权委托给 Twilight SDK（`a.client.StreamText` / `GenerateTextResult`），内核自己只做三件事——① 把 SDK 的流式 Part 翻译成业务事件推到 channel；② 在工具执行前后、模型调用前后插 Hook；③ 在文本/工具两条路径上做循环检测和重试。换句话说，内核不碰 prompt 渲染之外的业务逻辑，但它要保证：消费者断连不泄漏 goroutine、流中断能续跑、模型陷入循环能止损、终端事件哪怕 context 取消也能投递。这就是 `internal/agent/agent.go` 这 1500 行在做的事。

## 核心结论

1. **两个入口，一个内核**：`Stream()` 返回 `<-chan StreamEvent`（agent.go:67），`Generate()` 返回 `*GenerateResult`（agent.go:77）。两者分别走 `runStream` / `runGenerate`，但工具组装、循环检测、Hook 包装的逻辑是共享的。

2. **SDK 是真正的循环驱动者**：内核设置 `sdk.WithMaxSteps(-1)`（agent.go:770）让 SDK 无限步跑，多轮工具调用由 SDK 内部循环完成。内核只在外层做「流式 Part → 业务事件」的翻译。

3. **循环检测双轨**：文本走 n-gram 重叠检测（连续 3 次命中 abort），工具走 SHA256 去重（重复 5 次先警告、再犯 abort）。两套阈值常量都在 `sential.go:17-26`。

4. **重试分两段**：流启动失败用配置的 `RetryConfig`（默认 10 次，前 5 快速后 5 退避）；流中途出错走 `runMidStreamRetry`，用累积 messages 续跑（agent.go:1281）。

5. **终端事件必须送达**：即使用户取消，`EventAgentEnd`/`EventAgentAbort` 也用 `context.WithoutCancel(ctx)` + 5s 超时投递，否则消费者拿不到已累积的部分消息（agent.go:599-607）。

## 关键源码剖析

### 一、Stream / Generate 入口

`Agent` 结构体极简，只持有依赖（agent.go:24-31）：

```go
type Agent struct {
    client         *sdk.Client
    toolProviders  []tools.ToolProvider
    bridgeProvider bridge.Provider
    hookService    *hooks.Service
    logger         *slog.Logger
    limits         Limits
}
```

`Stream()` 的实现就是把 channel 交给后台 goroutine（agent.go:67-74）：

```go
func (a *Agent) Stream(ctx context.Context, cfg RunConfig) <-chan StreamEvent {
    ch := make(chan StreamEvent)
    go func() {
        defer close(ch)
        a.runStream(ctx, cfg, ch)
    }()
    return ch
}
```

`Generate()` 则同步返回（agent.go:77-79）。两者的关键差异在副作用收集方式：Stream 用 `streamEmitter` 把工具副作用事件直接推 channel（agent.go:151-153），Generate 用 `toolEventCollector` 收集后统一快照（agent.go:625-629、711-730）。

`runStream` 的整体骨架（agent.go:132-608）：

1. 建可取消的 `streamCtx`，defer 跑 turn-end/error Hook（132-146）
2. 组装工具（155-176）：`assembleTools` → `decorateReadMediaTools` → `WrapToolOutputLimits` → `wrapToolsWithHooks` → 再 `WrapToolOutputLimits`
3. 装循环检测守卫（178-199）
4. 装 `prepareStep` 钩子链（201-255）：readMediaState → InjectCh → ModelHook → midTaskPrune
5. 跑 `applyBeforeModelCallHook`（257-262）
6. 建 `buildGenerateOptions`（263）+ `WithOnStep` 回调（265-269）
7. **启动重试循环**（277-314）：调 `a.client.StreamText`，失败按 `isRetryableStreamError` 判断是否重试
8. **主事件循环**（316-534）：`for part := range streamResult.Stream` 翻译 SDK Part
9. 收尾（536-607）：合并 messages、算 usage、发终端事件

`runGenerate` 结构对称，区别在于用 `GenerateTextResult` 一次性返回，循环检测放在 `WithOnStep` 回调里同步判断（agent.go:676-697）。

### 二、流式事件组装

事件类型别名集中在 `stream.go`，只是把 `event` 子包的类型 re-export（stream.go:9-39）。真正的组装逻辑在 `runStream` 的 type switch（agent.go:327-529）。

SDK 流式 Part 到业务 StreamEvent 的映射关系：

| SDK Part | 业务事件 | 行号 |
|---|---|---|
| `*sdk.TextStartPart` | `EventTextStart` | 331-334 |
| `*sdk.TextDeltaPart` | `EventTextDelta` + 写入 `allText` + 喂循环探测 | 336-345 |
| `*sdk.TextEndPart` | `EventTextEnd` + `EventProgress` | 347-359 |
| `*sdk.ReasoningStartPart` | `EventReasoningStart` | 361-364 |
| `*sdk.ReasoningDeltaPart` | `EventReasoningDelta` | 366-369 |
| `*sdk.ToolInputStartPart` | `EventToolCallInputStart`（轻量，无 Input） | 376-393 |
| `*sdk.StreamToolCallPart` | `EventToolCallStart`（带完整 Input） | 395-406 |
| `*sdk.ToolProgressPart` | `EventToolCallProgress` | 408-416 |
| `*sdk.ToolApprovalRequestPart` | `EventToolApprovalRequest` 或 `EventUserInputRequest` | 418-440 |
| `*sdk.StreamToolResultPart` | `EventToolCallEnd` + `EventProgress` | 442-458 |
| `*sdk.StreamToolErrorPart` | `EventToolCallEnd`（带 Error） | 465-481 |
| `*sdk.StreamFilePart` | `EventAttachment`（base64 data URL） | 483-497 |
| `*sdk.ErrorPart` | `EventError` + 可能触发 mid-stream 重试 | 499-522 |
| `*sdk.AbortPart` | `aborted = true` | 524-525 |

两个设计细节值得注意：

**消费者断连保护**——`sendEvent` 用 `select` 同时监听 channel 和 `ctx.Done()`（agent.go:123-130），这样消费者停止读取时，生产者不会阻塞在 channel 发送上导致 goroutine 泄漏：

```go
func sendEvent(ctx context.Context, ch chan<- StreamEvent, evt StreamEvent) bool {
    select {
    case ch <- evt:
        return true
    case <-ctx.Done():
        return false
    }
}
```

**ToolInputStartPart vs StreamToolCallPart 的双阶段**——前者先发一个只有 name + callID 的轻量事件让 UI 立刻渲染工具块占位，后者再补全 Input（agent.go:376-394 注释）。IM/Discuss 适配器不映射前者，避免重复「running」消息。

### 三、Prompt 组装

`prompt.go` 用 `//go:embed prompts/*.md` 把 prompt 模板编进二进制（prompt.go:18-19）。`init()` 时加载 8 个模板 + 2 个 include 片段（prompt.go:36-57）。

**5 种会话模式**对应 5 个模板，由 `selectModeTemplate` 分发（prompt.go:91-104）：

```go
func selectModeTemplate(sessionType string) string {
    switch sessionType {
    case sessionmode.Discuss:   return modeDiscussTmpl
    case sessionmode.Heartbeat: return modeHeartbeatTmpl
    case sessionmode.Schedule:  return modeScheduleTmpl
    case sessionmode.Subagent:  return modeSubagentTmpl
    default:                    return modeChatTmpl
    }
}
```

**include 机制**：模板里写 `{{include:_memory}}`、`{{include:_identities}}`，`init()` 时用正则替换成片段内容（prompt.go:34、68-80）。

**render 机制**：`{{key}}` → `vars[key]`，简单的 `strings.ReplaceAll` 循环（prompt.go:83-89）。

`GenerateSystemPrompt`（prompt.go:107-137）的组装顺序：

1. `buildBotInfoSection`：把 BotInfo 序列化成 JSON 塞进 `## Bot` 段（151-164）
2. `buildSkillsSection`：技能列表，按名排序（199-220）
3. `buildFileSections`：工作区文件，按字节/行数预算截断（222-259）
4. 拼接 `systemCommonTmpl + "\n\n" + selectModeTemplate(...)`（124）
5. `render` 填充 `home`、`currentTime`、`timezone`、各 section 占位符（126-136）

主 Agent 和 Subagent 的 section 组装不同（prompt.go:310-327）：主 Agent 含 `_memory` + identities + skills + files；Subagent 只有 identities。

另外两个独立函数：
- `GenerateSchedulePrompt`（167-179）：渲染定时任务触发消息
- `GenerateHeartbeatPrompt`（182-197）：渲染心跳触发消息，含 `lastHeartbeat` 和可选 checklist

**工具使用说明注入**：`assembleTools` 收集实现了 `tools.ToolUsage` 接口的 provider 的使用指南，拼成 `## Tool usage` 段，再由 `appendToolUsageToSystem` 插到 system prompt 里（agent.go:826-895、897-911）。插入位置有讲究：如果 system 里有 `## Workspace instruction files` 锚点，就插在它前面，否则追加末尾。

### 四、循环检测

循环检测分文本和工具两条线，常量集中在 `sential.go:17-32`：

```go
LoopDetectedStreakThreshold     = 3   // 文本连续命中 3 次 abort
LoopDetectedMinNewGramsPerChunk = 8
LoopDetectedProbeChars          = 256  // 探测分块大小
ToolLoopRepeatThreshold         = 5   // 工具重复 5 次触发
ToolLoopWarningsBeforeAbort     = 1   // 先警告 1 次再 abort
```

**文本循环：Sential + TextLoopGuard + TextLoopProbeBuffer 三层**

- `Sential`（sential.go:58-191）：n-gram 重叠检测器。维护一个 `windowSize`（默认 1000）的滑动窗口，窗口内 n-gram 存在 `historySet` 里。`Inspect(text)` 算新文本的 n-gram 与历史的匹配率，超过 `overlapThreshold`（默认 0.75）算 hit。n-gram 默认 size=10。
- `TextLoopGuard`（sential.go:203-246）：在 Sential 之上加连续命中计数。只有当 `NewGrams >= minNewGramsPerChunk`（默认 8）时才更新 streak——避免短文本误判。`streak >= consecutiveHitsToAbort`（默认 3）时 `Abort=true`。
- `TextLoopProbeBuffer`（sential.go:251-298）：把流式 delta 攒成 256 字符的块再喂给 Guard，减少高频调用。

流式路径在 `TextDeltaPart` 时 `Push`，在 `TextEndPart` / 工具调用开始时 `Flush`（agent.go:339、349、385、397）。命中后 `cancel(ErrTextLoopDetected)`（agent.go:190-191）。

**工具循环：ToolLoopGuard**

`ToolLoopGuard`（sential.go:331-407）的逻辑：

1. `computeToolLoopHash`：把 `{toolName, input}` 序列化后 SHA256（sential.go:491-499）
2. **剔除 volatile keys**：`defaultVolatileKeys`（sential.go:302-308）列出 toolcallid/requestid/traceid/sessionid/timestamp/createdat/updatedat/expiresat/nonce 等字段，`isVolatileKey` 还做后缀匹配（sential.go:434-448）。这样带时间戳的相同语义调用不会被误判为不同。
3. `Inspect`（sential.go:362-407）：相同 hash 累加 `repeatCount`；超过 `repeatThreshold`（5）时进入警告/abort 流程——`breachCount < warningsBeforeAbort`（1）时只 `warn` 并重置计数，否则 `abort`。

工具循环通过 `wrapToolsWithLoopGuard`（agent.go:1145-1176）包装 `tool.Execute`：abort 时返回 `ErrToolLoopDetected` 并把 callID 注册到 `toolAbortRegistry`；warn 时返回带 `__memoh_tool_loop_warning` 标记的警告结果。主循环在 `StreamToolResultPart` / `StreamToolErrorPart` 时 `Take(callID)` 消费，触发 `cancel(ErrToolLoopDetected)`（agent.go:443、459-463、467、477-481）。

### 五、Guard 状态机

`guard_state.go` 文件名容易误导——它实际放的是两个辅助结构：`toolAbortRegistry` 和 `toolEventCollector`。

**`toolAbortRegistry`**（guard_state.go:9-52）：一个 set，`Add(toolCallID)` 登记、`Take(toolCallID)` 消费并删除、`Any()` 查是否有待处理。它的作用是跨 goroutine 传递「这个工具调用该被 abort」的信号——循环检测在工具 Execute 里触发，但 abort 动作要等主循环收到结果时才执行。

**`toolEventCollector`**（guard_state.go:54-114）：非流式 `Generate` 路径专用。工具副作用事件（attachment/reaction/speech）在工具执行时产生，但没有 channel 可推，就先收集到这里，`runGenerate` 结束时 `CloseAndSnapshot` 取出再分拣到 `GenerateResult`（agent.go:711-730）。

另外还有一个 `loopAbortState`（agent.go:1508-1537）——`Generate` 路径专用，记录循环检测触发的 error，因为 `Generate` 不像 Stream 有 channel 推事件，需要一个共享变量把 abort 原因带出来。

### 六、重试策略

`retry.go` 定义重试配置和判断逻辑。

**默认配置**（retry.go:33-40）：

```go
func DefaultRetryConfig() RetryConfig {
    return RetryConfig{
        MaxAttempts:  10,
        FastAttempts: 5,
        BaseDelay:    1 * time.Second,
        MaxDelay:     30 * time.Second,
    }
}
```

前 5 次（`attempt < FastAttempts`）零延迟快速重试，后 5 次指数退避 `base * 2^(attempt-fast)`，封顶 30s，加 `[delay/2, delay)` 的 jitter（retry.go:78-93）。

**可重试错误判定** `isRetryableStreamError`（retry.go:43-73）：

| 错误类型 | 是否重试 | 行号 |
|---|---|---|
| `context.Canceled` / `context.DeadlineExceeded` | **不重试**（先判，因为 DeadlineExceeded 也满足 net.Error） | 49-51 |
| `net.Error`（网络层） | 重试 | 53-56 |
| HTTP 429 | 重试 | 59-61 |
| 含 "rate limit" / "rate_limit" | 重试 | 62-64 |
| `api error 5XX` | 重试 | 65-67 |
| connection reset / refused / EOF | 重试 | 68-71 |
| 其他 | 不重试 | 72 |

**两个重试入口**：

1. **流启动失败**（agent.go:277-314）：`a.client.StreamText` 返回 error。重试前发 `EventRetry` 事件让上层感知，用 `sleepWithContext` 等待（可被取消）。
2. **流中途出错**（agent.go:511-522 → `runMidStreamRetry` 1281-1478）：收到 `*sdk.ErrorPart` 且 `isRetryableStreamError` 为真时触发。关键设计：先用累积的 `prevResult.Messages` 重新 `buildGenerateOptions` 再 `StreamText`，并把新流的事件继续往原 channel 推；最后把 `prevResult.Messages` 和 `retryResult.Messages` 合并，保证历史不丢（agent.go:1466-1471）。

## 设计动机与取舍

**为什么用 SDK 的 MaxSteps(-1) 而不是自己写循环？**——把多轮工具调用的状态机交给 Twilight SDK，内核只做事件翻译和守卫。好处是 SDK 已经处理了「assistant 消息带 tool_call → 执行 → tool_result → 再 assistant」的消息拼装，内核不用重复造轮子。代价是内核必须信任 SDK 的 `StreamResult.Messages` 和 `Steps` 字段。

**为什么文本和工具循环用不同算法？**——文本循环用 n-gram 重叠率（连续 3 次命中），因为文本「卡住」时往往是整段重复，n-gram 能捕捉；工具循环用 hash 去重（重复 5 次），因为工具调用的「相同」语义需要剔除时间戳等 volatile 字段后才能判定。两套阈值都是可调的，但默认值偏保守——宁可多跑几轮也不轻易 abort。

**为什么 mid-stream 重试要合并 messages？**——SDK 的 `StreamResult.Messages` 只包含本次 `StreamText` 调用产生的消息。如果不合并，重试前已经产生的 assistant 文本和工具调用就会丢失，用户会看到回复「断片」。

**为什么终端事件用 WithoutCancel？**——如果用原 ctx，用户一取消 `sendEvent` 就走 `ctx.Done()` 分支返回 false，消费者永远收不到 `EventAgentEnd`/`EventAgentAbort`，也就拿不到已经累积的 `streamResult.Messages`，只能用合成占位符兜底。用 `context.WithoutCancel` + 5s 超时既保证投递，又防止消费者彻底失联时 goroutine 永久阻塞（agent.go:599-607）。

**为什么 prepareStep 要串这么多层？**——`readMediaState.prepareStep` → `InjectCh` → `wrapPrepareStepWithModelHook` → `midTaskPrune`，每层各司其职：媒体解析、消息注入、模型前 Hook、上下文裁剪。串联而非合并是为了让每层可独立测试、可按需关闭。

## 验证问题

1. **画图题**：画出一次 `Stream()` 调用从入口到 `EventAgentEnd` 的完整数据流，标出 goroutine 边界、channel 方向、context 取消传播路径。
2. **事件题**：SDK 的 `ToolInputStartPart` 和 `StreamToolCallPart` 分别映射到什么业务事件？为什么拆成两阶段？IM 适配器和 Web UI 的处理差异是什么？
3. **循环检测题**：工具循环检测如何剔除 volatile keys？默认剔除哪些字段？如果不剔除 `timestamp`，会导致什么后果？文本循环检测的 streak 为什么要求 `NewGrams >= 8` 才更新？
4. **重试题**：流启动失败和流中途出错的重试有什么区别？`runMidStreamRetry` 为什么要先 drain 旧流再读 `prevResult.Messages`？为什么 `context.Canceled` 不重试但 `net.Error` 重试？
5. **守卫题**：`toolAbortRegistry` 解决什么跨 goroutine 问题？为什么不能直接在工具 Execute 里 `cancel()`？`Generate` 路径为什么还要额外加一个 `loopAbortState`？

## 待学

- `internal/agent/event/` 子包：StreamEvent 的完整字段定义、序列化逻辑。
- `internal/agent/tools/`：`ToolProvider` 接口、`StreamEmitter`、`WrapToolOutputLimits`、`decorateReadMediaTools` 的实现——这是 [[02_工具系统]] 的内容。
- `internal/agent/background/`：`Manager` 和 `RunningTasksSummary`，后台任务如何注入 system prompt。
- `internal/agent/sessionmode`：会话模式的完整定义和切换逻辑。
- `internal/hooks/`：Hook 服务的具体执行引擎、`Decision` 枚举、hook 脚本格式。
- `internal/prune/`：`pruneOldToolResults` 用的文本裁剪策略细节。
- Twilight SDK（`github.com/memohai/twilight-ai/sdk`）：`StreamText` / `GenerateTextResult` 内部如何驱动多轮、`StepResult` 结构、`PrepareStep` 何时调用。
- prompt 模板文件（`prompts/*.md`）：`system_common.md`、`mode_chat.md` 等的实际内容——这关系到 [[05_prompt工程与模式切换]]。
- `internal/agent/sessionmode` 与心跳/调度子系统的交互——这关系到 [[04_对话流编排]]。

## Connections

- [[02_工具系统]] — `assembleTools`、`ToolProvider`、`StreamEmitter`、`wrapToolsWithLoopGuard` 都依赖 tools 子包；工具审批流程（`markApprovalTools`、`wrapApprovalHandlerWithHooks`）是两者的核心交汇点。
- [[03_长期记忆]] — `buildMainAgentSections` 注入 `_memory` 片段；`applyBeforeModelCallHook` 的 `AppendContext` 机制是记忆注入到对话的运行时通道。
- [[04_对话流编排]] — `selectModeTemplate` 的 5 种模式、`GenerateSchedulePrompt` / `GenerateHeartbeatPrompt`、`InjectCh` 消息注入机制，都由上层编排器调用。
- [[05_prompt工程与模式切换]] — `prompts/*.md` 模板内容、include 片段、render 占位符的完整语义；`appendToolUsageToSystem` 的插入位置策略。
