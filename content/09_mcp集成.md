# 09 — MCP 集成

> **1 分钟叙事**：Memoh 的 MCP 集成要回答的问题是——怎么把「外部 MCP 服务器」和「Memoh 原生工具」揉成一张 ACP 运行时能统一调用的工具面，同时不把手写 JSON-RPC 的坑再踩一遍。答案是 `github.com/modelcontextprotocol/go-sdk v1.5.0`（`go.mod:41`）做协议层：服务端用它把工具网关包成 Streamable HTTP MCP 服务器暴露给容器内的 ACP 运行时（`http_tools.go:135-154`）；客户端用它联邦外部 stdio/SSE/Streamable HTTP 三种 MCP 服务器（`mcp_federation_gateway.go:102-144`、`mcp_stdio.go:801`）。go-sdk 替 Memoh 扛掉了 JSON-RPC 2.0 的请求/响应关联、Streamable HTTP 的 SSE 帧解析、IOTransport 的行分隔帧、Tool/CallToolResult 的类型映射——这些恰好是手写最容易出 bug 的地方。但 go-sdk 不管业务，所以 Memoh 在它上面又搭了三层自己的东西：连接 CRUD（`connections.go`）、OAuth 全流程（`oauth.go`）、工具网关与会话上下文存储（`tool_gateway_service.go` / `tool_session_store.go`）。

## 核心结论

1. **一个 go-sdk，两个角色**：服务端 `BuildToolMCPServer` 用 `sdkmcp.NewServer` + `AddReceivingMiddleware` 把工具网关变成 MCP 服务器（`http_tools.go:135-154`）；客户端用 `sdkmcp.NewClient` + `StreamableClientTransport`/`SSEClientTransport`/`IOTransport` 联邦外部服务器（`mcp_federation_gateway.go:107-116`、`mcp_stdio.go:801`）。两条路径共用 `ToolSource` 接口（`tool_types.go:45-48`）。

2. **stdio 不是纯 SDK**：外部 stdio MCP 进程跑在 bot 容器里，Memoh 通过 gRPC `ExecStream` 把 stdin/stdout 桥到 `io.Pipe`，再交给 `sdkmcp.IOTransport` 做帧（`mcp_stdio.go:729-817`）。但 initialize 握手和请求/响应关联是**手写**的（`mcp_stdio.go:307-455`），没有用 `ClientSession.Connect`——因为 stdio 会话要长存复用，而 HTTP/SSE 路径每次 call 都新建 session（`mcp_federation_gateway.go:58-62` `defer session.Close()`）。

3. **OAuth 全手写**：`oauth.go` 868 行，实现了 MCP 规范的完整发现链——probe 401 → Protected Resource Metadata → Authorization Server Metadata → Dynamic Client Registration → PKCE → token exchange → 自动刷新。go-sdk 没有提供 OAuth 帮手，Memoh 用裸 `net/http` 撸。

4. **工具网关是联邦 + 缓存**：`ToolGatewayService` 聚合 `nativeSource`（Memoh 原生工具）和 `fedSource`（外部 MCP 连接）（`cmd/agent/app.go:740`），按 session 维度缓存 5 秒的 `ToolRegistry`（`tool_gateway_service.go:15`、`142-179`）。缓存键把 RuntimeToken/SessionToken 做 sha256 哈希（`tool_gateway_service.go:220-227`），避免明文落内存。

5. **stdio 沙箱化**：与 WeKnora 直接禁用 stdio「for security reasons」不同，Memoh 把 stdio MCP 进程关在 bot 的 containerd 容器里执行（`mcp_stdio.go:729-734`），命令经过 shell 转义（`mcp_stdio.go:542-575`）。安全边界是容器，不是传输层。

## 协议管理（connections/jsonrpc/service）

### connections.go —— 连接 CRUD 与 mcpServers 互转

`Connection` 结构体是连接的运行时投影（`connections.go:18-36`）。除了基本的 `Type`（stdio/http/sse）、`Config`、`Status`、`ToolsCache`，还有四个字段服务插件系统：

- `ManagedByPluginInstallationID` / `ManagedResourceKey`（`connections.go:30-31`）——由插件安装托管的连接，删除插件时级联（`DeleteByPlugin`，`connections.go:413-429`）。
- `Visible` / `Metadata`（`connections.go:32-33`）——控制是否对用户可见 + 任意元数据。

`inferTypeAndConfig`（`connections.go:550-587`）是连接类型的判官：有 `command` 就是 stdio，有 `url` 就看 `transport` 字段——`sse` 显式声明，否则默认 `http`（Streamable）。`command` 和 `url` 互斥，同时给就报错。

`Import` / `ExportByBot`（`connections.go:314-373`）实现了与标准 `mcpServers` 字典的双向互转——导入时按 name upsert，已存在的连接保留 `is_active`，新的默认启用；导出时按 `Type` 反向构造 `MCPServerEntry`。这是为了让用户可以直接贴 Claude Desktop 风格的 `mcpServers` JSON 进来。

`UpdateProbeResult`（`connections.go:504-527`）把 probe 到的工具列表和状态写回 DB——`ToolsCache` 字段存的就是 `[]ToolDescriptor` 的 JSON。

### service.go + jsonrpc.go —— JSON-RPC 类型与 payload 帮手

两个文件合起来是 JSON-RPC 2.0 的薄层。`service.go`（105 行）定义核心类型 `JSONRPCRequest`/`JSONRPCResponse`/`JSONRPCError`（`service.go:9-26`）和 payload 解析帮手：`NewToolCallRequest`/`RawStringID`（`service.go:28-47`）构造 `tools/call` 请求；`PayloadError`（`service.go:49-60`）从裸 payload 抽 error；`ResultError`（`service.go:62-75`）看 `result.isError` 标志；`StructuredContent`（`service.go:77-92`）优先取 `structuredContent`，回退把 `content[0].text` 当 JSON 解析；`ContentText`（`service.go:94-105`）抽首个 text content。

`jsonrpc.go`（19 行）是补丁：`IsNotification` 判 `ID` 为空且 method 前缀 `notifications/`（`jsonrpc.go:8-10`），`JSONRPCErrorResponse` 构造标准错误响应（`jsonrpc.go:12-18`）。这两个帮手主要服务 stdio 透传路径（`mcp_stdio.go:716-724`），HTTP/SSE 路径走 go-sdk 的类型不走这里。

## OAuth

`OAuthService`（`oauth.go:27-44`）持 `dbstore.Queries`、`*http.Client`（15s 超时）、`callbackURL`。callbackURL 在 `cmd/agent/app.go:706-717` 注入，默认 `http://<host>:<port>/oauth/mcp/callback`。

### 发现链（Discover，oauth.go:77-148）

三步走，对应 MCP 规范的 OAuth 发现流程：

1. **probe 401**（`probeForAuth`，`oauth.go:470-514`）：向 MCP 服务器 POST 一个 `initialize` JSON-RPC 请求（protocolVersion `2025-06-18`），期望拿到 `401 + WWW-Authenticate` 头。从 `WWW-Authenticate` 里抽 `resource_metadata` 和 `scope`（`extractWWWAuthParam`，`oauth.go:847-867`）。
2. **Protected Resource Metadata**（`fetchProtectedResourceMetadata`，`oauth.go:528-549`）：从 PRM 拿 `authorization_servers` 列表和 `scopes_supported`。如果 probe 没给 URL，`guessResourceMetadataURL`（`oauth.go:516-526`）按 `/.well-known/oauth-protected-resource` 猜。**降级**：PRM 拿不到时（注释点名 Linear），直接拿 server URL 的 origin 当 authorization server（`oauth.go:103-117`）。
3. **Authorization Server Metadata**（`fetchAuthServerMetadata`，`oauth.go:551-588`）：按 RFC 8414 试多个 well-known 候选——有 path 的 issuer（如 `https://github.com/login/oauth`）依次试 path 追加、path 插入 OIDC、path 插入 OAuth 五种组合（`oauth.go:563-577`）。注释提到这是为了适配 GitHub 这种带 path 的 issuer。

### 授权（StartAuthorization，oauth.go:182-286）

client_id 解析走四级优先链（`oauth.go:177-181` 注释）：用户传入 → DB 里已存 → DCR 注册 → 报错。

- **DCR**（`registerClient`，`oauth.go:772-810`）：按 RFC 7591 POST 到 `registration_endpoint`，`token_endpoint_auth_method: "none"`（PKCE-only）。注册成功把 client_id/secret 存 DB。
- **PKCE**（`oauth.go:814-825`）：`generateCodeVerifier` 32 字节随机 base64url，`computeCodeChallenge` 是 `SHA256(verifier)` 的 base64url（S256）。
- 拼授权 URL 时带 `resource` 参数（`oauth.go:275-277`）——这是 MCP 规范要求的，让授权服务器知道在为哪个资源签 token。

### 回调与刷新

`HandleCallback`（`oauth.go:289-335`）按 state 查 PKCE 状态，用 `exchangeCode`（`oauth.go:615-666`）换 token。`parseTokenResponse`（`oauth.go:670-712`）先试 JSON，失败回退 form-encoded——注释点名 GitHub 默认返回 form-encoded。

`GetValidToken`（`oauth.go:338-387`）是调用时注入 Bearer 的入口：过期前 30s 自动 refresh，refresh 失败且无 refresh_token 才报错。`MCPFederationGateway.connectionHTTPClient`（`mcp_federation_gateway.go:197-234`）调用它，把 `Authorization: Bearer <token>` 通过 `staticHeaderRoundTripper` 注到每个出站请求。

## 工具网关（tool_gateway_service/tool_registry/tool_session_store）

### tool_types.go —— 三件套

- `ToolSessionContext`（`tool_types.go:13-33`）：请求级身份载体，19 个字段。`RuntimeToken`/`SessionToken` 标 `json:"-"` 防止序列化泄漏。`CanRequestUserInput`/`CanListUserInput`/`IsSubagent`/`RuntimeActive`/`SupportsImageInput` 是能力位。
- `ToolDescriptor`（`tool_types.go:36-40`）：`Name`/`Description`/`InputSchema`，MCP `tools/list` 的标准项。
- `ToolSource` 接口（`tool_types.go:45-48`）：`ListTools(ctx, session)` + `CallTool(ctx, session, name, args)`。两个实现：`federation.Source`（外部 MCP）和 `NativeToolSource`（Memoh 原生工具）。
- `BuildToolSuccessResult`/`BuildToolErrorResult`（`tool_types.go:60-99`）：构造标准 MCP 工具结果——success 带 `structuredContent` + 同步生成 `content[0].text` 回退；error 带 `isError:true`。

### tool_registry.go —— 名字去重表

`ToolRegistry`（`tool_registry.go:16-73`）是 `map[string]registryItem`，`registryItem` 持 `ToolSource` + `ToolDescriptor`。`Register` 同名拒绝（`tool_registry.go:40-42`），`List` 按名字字典序返回。这是工具网关聚合多个 source 后的去重层——重复工具名在 `getRegistry` 里被 warn 并跳过（`tool_gateway_service.go:166-168`）。

### tool_gateway_service.go —— 联邦 + 缓存 + 限流

`ToolGatewayService`（`tool_gateway_service.go:25-33`）持 `sources []ToolSource`、`cacheTTL`（默认 5s）、`cache map[string]cachedToolRegistry`。

核心是 `getRegistry`（`tool_gateway_service.go:142-179`）：

1. 按 `toolRegistryCacheKey(session)` 查缓存——缓存键拼了 BotID/ChatID/RuntimeID/SessionID/StreamID 等全部身份字段，`RuntimeToken`/`SessionToken` 走 sha256（`tool_gateway_service.go:220-227`）。
2. miss 时遍历所有 source `ListTools`，逐个 `registry.Register`，失败的 source warn 后 continue（`tool_gateway_service.go:159-170`）——单点故障不拖垮全局。
3. 写回缓存。

`CallTool`（`tool_gateway_service.go:95-132`）的两段式查找是亮点：先查缓存注册表，miss 则 `force=true` 强制重建注册表再查一次（`tool_gateway_service.go:106-114`）。找不到返回 `BuildToolErrorResult("tool not found")` 而非 Go error——让 MCP 客户端看到的是工具级错误而非传输级错误。

每个结果都过 `limitResult` → `LimitToolResult`（`result_limit.go:12-28`）：超限的 structured content 按 `MaxBytes/3` 预算反复剪到 `*3/4`（`result_limit.go:43-64`），text 走 `LimitString`。这是 [[06_上下文压缩]] 的工具侧延伸。

### tool_session_store.go —— 长生命周期会话上下文

`ToolSessionContextStore`（`tool_session_store.go:12-27`）解决一个具体矛盾：ACP MCP 会话是长生命周期的，HTTP 头在 agent 进程启动时就固定了，但每次 prompt 的 `ToolSessionContext` 会变。所以 store 按 `botID\x00sessionID` 存最新上下文（`tool_session_context_key`，`tool_session_store.go:191-198`），工具调用时 `Merge` 叠加（`tool_session_store.go:107-122`）。

`MergeToolSessionContext`（`tool_session_store.go:223-280`）是唯一的合并函数——string 非空覆盖，bool sticky-true（一旦 true 不回退）。注释强调「a new field only needs to be wired up here」——所有上下文合并都走这一处。

`ToolStreamEvent`（`tool_session_store.go:31-48`）+ `AppendToolEvent`/`RegisterToolEventSink`（`tool_session_store.go:146-189`）是工具调用的流式事件回放通道：`http_tools.go:181-219` 在 tools/call 前后 `recordToolEvent`，sink 注册后事件实时推给 UI。`ToAgentStreamEvent`（`tool_session_store.go:50-87`）把 MCP 事件翻译成 agent 层的 `event.StreamEvent`，复用 [[04_对话流编排]] 的事件总线。

### federation/source.go —— 外部 MCP 的 ToolSource 实现

`Source`（`federation/source.go:49-83`）把 `Gateway` 接口（HTTP/SSE/Stdio 三对 List/Call 方法）适配成 `ToolSource`。自带 5s 缓存（`federation/source.go:16`）和 60s 调用超时（`federation/source.go:20`）。

`buildToolsAndRoutes`（`federation/source.go:155-238`）的命名空间策略值得记：

- 每个 connection 的工具加前缀 `<connection_name>_<tool_name>`（`federation/source.go:218-222`），前缀经 `sanitizePrefix` 小写化、非法字符替 `_`（`federation/source.go:247-265`）。
- description 前加 `[<connection_name>]` 标识来源（`federation/source.go:223-227`）。
- 重名（跨 connection 或撞保留名）时追加 `_2`/`_3`（`federation/source.go:165-178`）。保留名由 `IsBuiltInToolName` 判定（`cmd/agent/app.go:738`）——原生工具优先。

`CallTool` miss 时先 `ListTools` 刷新路由缓存再查（`federation/source.go:111-121`），与 `ToolGatewayService` 的两段式查找同构。

## stdio transport binary（cmd/mcp）

**诚实说明**：`cmd/mcp/` 目录在当前代码库中**不存在**。`cmd/` 下只有 `agent`、`bridge`、`gen-bridge-mtls`、`memoh`、`synccaps` 五个二进制（`cmd/agent`、`cmd/memoh` 等）。stdio transport 的实现全部在 `internal/handlers/mcp_stdio.go`，没有独立的 stdio 代理二进制。

实际的「stdio transport binary」是**用户配置的 MCP 服务器命令本身**（如 `npx -y @modelcontextprotocol/server-filesystem /data`），它跑在 bot 的 containerd 容器里。Memoh agent 通过两条 HTTP 路由管理它：

- `POST /bots/{bot_id}/mcp-stdio`（`CreateMCPStdio`，`mcp_stdio.go:618-670`）：在容器里 `ExecStream` 启动命令，probe `tools/list`，返回 `connection_id` + 工具名列表。
- `POST /bots/{bot_id}/mcp-stdio/{connection_id}`（`HandleMCPStdio`，`mcp_stdio.go:684-727`）：JSON-RPC 透传——notification 走 `notify`（202），request 走 `call`，错误包成 `-32603`。

`startContainerdMCPCommandSession`（`mcp_stdio.go:729-818`）是桥的核心：

1. `buildShellCommand`（`mcp_stdio.go:542-565`）把 command + args + env + cwd 拼成 shell 命令串，每个参数 `escapeShellArg`（`mcp_stdio.go:567-575`）——单引号转义防注入。
2. 通过 `manager.MCPClient` 拿 gRPC 客户端，`client.ExecStream` 起双向流。
3. `io.Pipe` 造三对管道：stdin→execStream.SendStdin、execStream.Recv→stdout/stderr。
4. `sdkmcp.IOTransport{Reader: stdout, Writer: stdin}`（`mcp_stdio.go:801-804`）做 JSON-RPC 行帧，`transport.Connect` 拿到 `sdkmcp.Connection`。
5. `stderr` 起独立 goroutine 滚动记录最后 8 行（`mcpStderrTail`，`mcp_stdio.go:62-87`），错误时拼进 error message 帮排障（`errorWithStderr`，`mcp_stdio.go:89-98`）。

**为什么不直接用 `ClientSession.Connect`**：stdio 会话要跨多次工具调用复用（`mcpStdioSession` 缓存在 `h.mcpStdioSess`，`mcp_stdio.go:661-663`），而 `ClientSession` 的生命周期管理对长存会话不友好。所以 Memoh 手写了 `mcpSession` 状态机（`mcp_stdio.go:100-107`）：`None → Initializing → Initialized → Ready`，`ensureInitialized`（`mcp_stdio.go:307-377`）保证只在首次调用时发 `initialize` + `notifications/initialized`，后续直接复用。并发请求通过 `pending map[string]chan *sdkjsonrpc.Response`（`mcp_stdio.go:53`）关联——比 fastclaw 那把整把锁（见下节）强。

## 与手写 MCP 对照

`/Users/mervyn/workspaces/github/` 下有多个手写 MCP 客户端项目。以 `fastclaw/internal/mcp/`（不依赖 go-sdk）为对照：

| 维度 | fastclaw（手写） | Memoh（go-sdk） |
|---|---|---|
| JSON-RPC 类型 | 手写 `jsonRPCRequest/Response`（`client.go:13-31`） | go-sdk `sdkjsonrpc.Request/Response/ID` + Memoh 薄封装 `tool_types.go:9-26` |
| 协议版本 | `2024-11-05`（`stdio.go:71`） | `2025-06-18`（`mcp_stdio.go:382`、`oauth.go:476`） |
| 请求关联 | `sendRequest` 持 `mu.Lock` 全程，int ID 自增，scan 循环匹配（`stdio.go:76-124`）——**不可并发** | `pending map` 按 ID 分发，`readLoop` 单线程读（`mcp_stdio.go:140-174`）——可并发 |
| initialize 握手 | 只发 `initialize`，不发 `notifications/initialized`（`stdio.go:69-73`） | 完整状态机，`Initialized → Ready` 间补发 notification（`mcp_stdio.go:333-354`） |
| stdio 进程 | 本地 `exec.Command`（`stdio.go:38`） | 容器内 gRPC `ExecStream`（`mcp_stdio.go:739`） |
| HTTP/SSE | `http.go` 另写，未看 | go-sdk `StreamableClientTransport`/`SSEClientTransport`（`mcp_federation_gateway.go:111,130`） |
| OAuth | 无 | 868 行全流程（`oauth.go`） |
| stderr 处理 | 直接 `c.cmd.Stderr = os.Stderr`（`stdio.go:60`） | `mcpStderrTail` 滚动 8 行 + 拼进 error（`mcp_stdio.go:62-98`） |

**go-sdk 的得**：

- 帧解析（IOTransport 行帧、StreamableHTTP 的 SSE 帧）不用自己写。
- 类型映射（`Tool`/`CallToolResult`/`CallToolParamsRaw`）有 SDK 兜底，`ConvertGatewayCallResultToSDK`（`http_tools.go:277-290`）靠 marshal/unmarshal 桥接。
- 服务端中间件（`AddReceivingMiddleware`，`http_tools.go:152`）让 tools/list、tools/call 的分发集中在一处。
- Streamable HTTP 的 `Stateless: true`（`http_tools.go:98`）直接对齐 Memoh 无状态网关的语义。

**go-sdk 的失 / Memoh 仍要自己写的**：

- **stdio 会话生命周期**：SDK 的 `ClientSession` 假设短生命周期，长存复用要手写状态机。
- **OAuth**：SDK 不提供 MCP OAuth 发现，Memoh 全手写。
- **容器内 exec 桥**：SDK 的 `IOTransport` 只认 `io.Reader/Writer`，把 gRPC 流变成 pipe 是 Memoh 自己的事。
- **错误信息丰富度**：SDK 的 `sdkjsonrpc.Error` 只给 code/message，Memoh 自己往上拼 stderr tail。

`cagent/pkg/mcp/server.go` 也用 go-sdk（`cagent/go.mod:46`，v1.2.0），但只做服务端，没有联邦客户端和 OAuth——Memoh 的覆盖面更全。`WeKnora/internal/mcp/` 完全手写且禁用 stdio（`manager.go` 注释），与 Memoh 的「沙箱内拥抱 stdio」路线相反。

## 设计动机与取舍

1. **服务端无状态（`Stateless: true`）**：Memoh 的工具网关不维护 MCP 会话状态，每次请求独立。会话上下文走 `ToolSessionContextStore` 在 Memoh 内部传递，不依赖 MCP 协议的会话 ID。这让水平扩展无负担，但要求每次请求都带全身份头（`http_tools.go:14-32` 的 `X-Memoh-*` 头）。

2. **stdio 跑在容器里而非宿主**：直接 exec 本地进程最快，但安全风险高（命令注入、文件系统访问）。Memoh 选择把命令发到 bot 的 containerd 容器里执行（`mcp_stdio.go:739`），用容器的文件系统/网络隔离做边界。代价是每次调用要走 gRPC，多一跳延迟。

3. **5 秒缓存 + force 重建**：工具列表不常变，5 秒缓存（`tool_gateway_service.go:15`、`federation/source.go:16`）让高频对话不反复 probe 外部服务器。但调用 miss 时 force 重建（`tool_gateway_service.go:106-114`）保证新装的连接能被即时发现——牺牲一点一致性换可用性。

4. **OAuth 降级链**：PRM 拿不到试 origin（`oauth.go:103-117`）；ASM well-known 试 5 种 path 组合（`oauth.go:563-577`）；client_id 走四级优先链含 DCR（`oauth.go:177-181`）；token 响应先 JSON 后 form-encoded（`oauth.go:670`）。每一步都有注释点名是哪个真实服务逼出来的（Linear、GitHub）——这是「跑过生产」的代码特征。

5. **工具命名空间前缀**：联邦工具加 `<conn_name>_` 前缀（`federation/source.go:218`）而非用 MCP 的 server 名字段，是因为 Memoh 要把所有连接的工具拍平到一个全局注册表里，前缀是唯一的去重手段。原生工具用 `IsBuiltInToolName` 占保留名（`cmd/agent/app.go:738`），优先级高于联邦工具。

6. **RuntimeToken/SessionToken 不进缓存键明文**：`hashCacheKeySecret`（`tool_gateway_service.go:220-227`）sha256 后再拼，防止内存 dump 泄漏 token。`ToolSessionContext` 里这两个字段也标 `json:"-"`（`tool_types.go:17,24`）。

## 验证问题

1. **go-sdk 封装得失**：对照 `fastclaw/internal/mcp/stdio.go` 的 `sendRequest`（全程持锁、不可并发）与 Memoh `mcp_stdio.go` 的 `pending map` + `readLoop`（可并发）。如果不用 go-sdk 的 `IOTransport`，Memoh 要自己写多少行帧解析？go-sdk 省掉的是哪类 bug，留下的是哪类自己必须处理的逻辑（握手状态机、会话复用）？

2. **四个组件职责边界**：用一句话说清 `ConnectionService`（连接元数据的 CRUD + mcpServers 互转）、`OAuthService`（连接的认证流程 + token 刷新）、`ToolGatewayService`（运行时工具联邦 + 缓存 + 限流）、`ToolSessionContextStore`（长生命周期会话身份的存取 + 事件回放）各自管什么。它们之间没有循环依赖——`ToolGatewayService` 依赖 `ToolSource`（含 federation.Source），federation.Source 依赖 `Gateway` 接口（由 `MCPFederationGateway` 实现），`MCPFederationGateway` 依赖 `OAuthService` 和 `ContainerdHandler`。画出这个依赖图。

3. **stdio 握手状态机**：`mcpSessionInitState` 有四个状态（`mcp_stdio.go:100-107`）。问：首次 `call` 一个 stdio 连接时，状态如何从 `None` 走到 `Ready`？如果 `initializeHandshake` 成功但 `sendInitializedNotification` 失败，状态停在 `Initialized`——下一次 `call` 会怎么走（`ensureInitialized` 的 `case mcpSessionInitStateInitialized` 分支，`mcp_stdio.go:333-354`）？

4. **OAuth 降级**：给一个 MCP 服务器 URL，它的 PRM 端点返回 404，且 issuer 是 `https://github.com/login/oauth`。`Discover` 会怎么走？最终 `fetchAuthServerMetadata` 会试哪 5 个候选 URL（`oauth.go:563-577`）？为什么需要 path 追加和 path 插入两种策略？

5. **工具命名冲突**：两个外部 MCP 连接都暴露 `search` 工具，连接名分别是 `Google` 和 `Bing`。`buildToolsAndRoutes`（`federation/source.go:155-238`）最终给出的工具名是什么？如果 `google_search` 恰好是 Memoh 保留名（`IsBuiltInToolName` 返回 true），会怎么重命名（`federation/source.go:165-178`）？

## 待学

- `cmd/mcp/` 不存在——是否有计划做独立的 stdio 代理二进制？当前架构下没必要，但如果要在无容器的轻量部署里用 stdio MCP，可能需要。
- go-sdk v1.5.0 的 `StreamableHTTPHandler` 内部如何处理 SSE 重连？`MaxRetries: -1`（`mcp_federation_gateway.go:114`）是无限重试还是禁用重试？需读 SDK 源码。
- `NativeToolSource`（`agenttools` 包）如何把 Memoh 原生工具适配成 `ToolSource`？它的 `ToolProvider` 列表（`cmd/agent/app.go:770-789`）有 19 个 provider，映射逻辑未看。
- OAuth 的 `resource` 参数（`oauth.go:275-277`）在 token exchange 时也带（`oauth.go:626-628`）——这是 RFC 8707 的资源指示器，部分授权服务器会用它签发 audience-bound token。Memoh 是否遇到过 audience 不匹配的 token 被拒？
- `ToolStreamEvent` 的 `tool_approval_request` / `user_input_request` 类型（`tool_session_store.go:38-47`）如何与 [[11_acp插件用户输入]] 的交互流程衔接？`ToAgentStreamEvent` 把它们翻译成 `event.ToolApprovalRequest`/`event.UserInputRequest`，但 sink 注册时机和 UI 消费路径未追。

## Connections

- [[02_工具系统]] —— MCP 集成是 Memoh 工具系统的对外协议层，`ToolSource` 接口是两者的契合点；`ToolGatewayService` 聚合的 `nativeSource` 就是原生工具系统暴露给 MCP 的入口。
- [[07_容器工作空间]] —— stdio MCP 进程跑在 containerd 容器里，`startContainerdMCPCommandSession` 通过 `manager.MCPClient` 拿 gRPC 客户端、`ExecStream` 在容器内执行命令，安全边界由容器提供。
- [[11_acp插件用户输入]] —— 插件通过 `CreateManaged`（`connections.go:208-264`）托管 MCP 连接，`ManagedByPluginInstallationID` 实现级联删除；`ToolStreamEvent` 的 `user_input_request`/`tool_approval_request` 与插件用户输入流程共享事件通道。
