# 客户端配置

[首页](../README.zh-CN.md) · [English](clients.md)

请先在 [WebUI](webui.zh-CN.md) 添加账号，并在「密钥管理」创建客户端密钥。下文将 `YOUR_GATEWAY_API_KEY` 替换为该推理密钥，`MODEL_ID` 替换为对外模型 ID，并按部署调整地址与端口。

## 通用设置

| 协议 | Base URL |
|------|----------|
| OpenAI Chat / Responses | `http://127.0.0.1:8787/v1` |
| Anthropic Messages | `http://127.0.0.1:8787` |

国内/国际、CLI/WorkBuddy 账号共用这些地址；后端自动选路，无需 `/cn` 或 `/intl` 前缀。客户端使用「密钥管理」创建的推理密钥或外部管理密钥，不能使用 WebUI 管理 Cookie。

列出可用模型：

```bash
curl http://127.0.0.1:8787/v1/models \
  -H 'Authorization: Bearer YOUR_GATEWAY_API_KEY'
```

使用响应中或 WebUI 里的 ID；各账号支持的模型不一定相同。可在界面中配置对外别名。Anthropic 模型名不会被自动猜测或映射。

## API key 与授权

普通密钥支持命名、再次查看、复制、轮换、启停，以及有效期、IP/CIDR 白名单、每分钟请求数和上游模型 ID 限制。管理密钥不受限制，只在创建或轮换时显示一次，库中仅保存哈希。外部管理密钥在界面中只读。所有密钥都只能调用 `/v1/*`，不能访问 `/admin/*`；普通密钥不能读取 `/v1/dashboard/billing/*`，管理密钥和显式匿名部署可以。

| 状态 / 错误码 | 含义 |
|---------------|------|
| 401 `api_key_required` / `invalid_api_key` | 未提供密钥、密钥未知，或数据库管理密钥已被外部密钥覆盖 |
| 401 `api_key_disabled` / `api_key_expired` | 密钥已停用或过期 |
| 403 `ip_not_allowed` / `model_not_allowed` | 客户端 IP 或解析后的上游模型不在密钥允许范围内 |
| 403 `admin_key_required` | 普通密钥尝试读取账号池 billing |
| 429 `rpm_exceeded` | 超过密钥的 60 秒滑动窗口限制；按 `Retry-After` 等待 |

OpenAI 响应使用 `error.type` 和 `error.code`；Anthropic 使用 `authentication_error`、`permission_error` 或 `rate_limit_error`。没有任何密钥且 `OPEN_NOAUTH=false` 时返回 `api_key_required`；显式开启匿名模式后，缺失或占位 token 都会放行，同时开放 billing。

## Codex CLI

将以下内容合并进 `~/.codex/config.toml`，不要覆盖已有配置。可直接复制的版本见 [`examples/codex-codebuddy.example.toml`](../examples/codex-codebuddy.example.toml)：

```toml
[model_providers.workbuddy]
name = "WorkBuddy (via local converter)"
base_url = "http://127.0.0.1:8787/v1"
wire_api = "responses"
env_key = "CODEBUDDY2API_KEY"

[profiles.workbuddy]
model = "MODEL_ID"
model_provider = "workbuddy"
```

```bash
export CODEBUDDY2API_KEY='YOUR_GATEWAY_API_KEY'
codex --profile workbuddy "your task"
```

Codex 使用 `/v1/responses`。运行时上下文与真实指令分开处理；请求超限返回 HTTP 413，而不会静默截断最新的用户请求。

## Responses 投影

Responses 投影是服务端设置，客户端地址保持不变。默认使用 balanced；如需完全关闭 Responses 投影可设 passthrough。生成内容与工具内容的裁剪上限见 [Responses 投影](advanced.zh-CN.md#responses-投影)。

## Claude Code / CC Switch

```bash
export ANTHROPIC_BASE_URL=http://127.0.0.1:8787
export ANTHROPIC_AUTH_TOKEN='YOUR_GATEWAY_API_KEY'
export ANTHROPIC_MODEL='MODEL_ID'
claude
```

- Claude Code、Anthropic SDK 与 CC Switch 的 Anthropic 提供方使用不含 `/v1/messages` 的 Base URL；SDK 会自行追加路径。只有明确要求完整端点的客户端才应包含 `/v1/messages`。
- `POST /v1/messages` 会转换为上游 Chat Completions，保留原生工具调用与推理内容。
- `--desensitize` 控制 WorkBuddy 固定 CLI 模板的适配，默认关闭，已发布镜像中启用。保留更完整指令等选项见[进阶参考](advanced.zh-CN.md)。

## 其他 OpenAI 兼容客户端

Cherry Studio、ZCode、LobeChat、NextChat、Open WebUI 或自研 SDK 客户端，统一使用上面的 Base URL、API key 与模型 ID。

生成端点为 `POST /v1/chat/completions`、`POST /v1/responses` 与 `POST /v1/messages`。需要完整 JSON 时显式设 `stream: false`，需要 SSE 时设 `stream: true`。

流式策略是服务端设置，不是客户端请求字段。默认 `compatible` 保持 Responses 及带工具 Chat/Messages 的聚合行为；`realtime` 让三个协议都增量发送。两种模式的非流式请求均为经过校验的 JSON。配置、部分输出错误及不再自动重生成工具参数等限制见[流式模式](advanced.zh-CN.md#流式模式)。

## 联网搜索与抓取

搜索与抓取端点，上游为 agenttool 的 `/v1/search` 与 `/v1/webfetch`：不调用模型，不产生 token 计费。

| 客户端 | 配置 |
|--------|------|
| MCP 客户端（Claude Code、Codex CLI 等） | Streamable-HTTP 服务器 `http://127.0.0.1:8787/v1/mcp`，暴露 `web_search` 与 `web_fetch` 工具 |
| REST 客户端 | `POST /v1/search`，请求体 `{"query": "...", "max_results": 5}`；`POST /v1/fetch`，请求体 `{"url": "...", "prompt": "...", "max_length": 20000}` |

```bash
claude mcp add --transport http web-search http://127.0.0.1:8787/v1/mcp \
  --header "Authorization: Bearer YOUR_API_KEY"
codex mcp add web-search --url http://127.0.0.1:8787/v1/mcp --bearer-token-env-var CODEBUDDY2API_KEY

curl http://127.0.0.1:8787/v1/search -H "Authorization: Bearer YOUR_API_KEY" \
  -d '{"query": "rust edition guide", "max_results": 5}'

curl http://127.0.0.1:8787/v1/fetch -H "Authorization: Bearer YOUR_API_KEY" \
  -d '{"url": "https://example.com/guide", "prompt": "summarize"}'
```

- 与其他 `/v1` 端点使用同一个 API key。
- `POST /v1/search` 校验 `query` 与 `max_results`（1–10），上游结果条目原样返回：`{"object": "search.results", "query", "total_results", "results": [{title, url, snippet, ...}]}`。
- `POST /v1/fetch` 校验 `url`（http/https）及可选的 `prompt`、`max_length`（1–100000）；由上游抓取页面并整理为 markdown：`{"object": "fetch.result", "url", "title", "content", ...}`。指向内网的 URL（`localhost`、私有、链路本地等非公网 IP 地址，以及 `.local`／`.internal` 主机名）会在发起上游请求前被拒绝，REST 端点返回 400，MCP `web_fetch` 工具与网关执行的 `web_fetch` 服务端工具同样适用。该检查只看字面主机名，不检测仅经 DNS 解析到内网地址的域名。
- 在推理请求里声明服务端联网工具——Anthropic 的 `web_search_*`／`web_fetch_*` 类型或 OpenAI 的 `web_search`／`web_search_options`——网关会改为在 agenttool 端点自行执行，并返回协议原生块（`server_tool_use` + `web_search_tool_result`／`web_fetch_tool_result`，或 `web_search_call`）。每次工具执行多产生一轮计费的模型往返；支持 `max_uses`、域名过滤与 `search_context_size`，`user_location` 忽略。
- 搜索结果不会在多轮对话中丢失：模型能看到每条结果的标题、链接与摘要；输出的 `web_search_result` 块把摘要放进网关自有的 `encrypted_content` 值（前缀 `cb2api:`），`web_search_call` 项则带 `action.sources`。客户端把这些项回传（`server_tool_use`、`web_search_tool_result`、`web_fetch_tool_result`、`web_search_call`）时，网关会以普通文本上下文（而非工具调用）还原给模型；其他 `encrypted_content`（例如 Anthropic 的真实密文）会被忽略。带日期的 OpenAI 类型（如 `web_search_preview_2025_03_11`、`web_search_2025_08_26`）与不带日期的写法同样生效。
- 网关执行的工具不会产生独立的审计记录：每次执行在触发它的请求中记为一条 `server_tool` 尝试（`model` 为 `web-search`／`web-fetch`，含结果、错误码、profile、凭证、尝试次数与耗时），该请求仍保留自己的模型、profile 与结果；其用量为所有上游轮次之和。
- 网关按请求选号，并按网关 failover 配置做一次换号重试；上游确定性拒绝原样返回。

## 媒体生成

图像与视频端点，上游为 `/v2/images/*` 与 `/v2/videos/*`；每次请求按上游积分计费。

- `POST /v1/images/generations`：`{"prompt", "model?", "size?", "n?"（≤4）, "quality?", "response_format?"}` → `{"created", "data": [{url | b64_json, revised_prompt?}], "usage"?}`。prompt 原样下发，支持多行（≤32000 字符）；gemini 族忽略 `n` 与 `quality`，与官方 CLI 一致。
- `POST /v1/images/edits`：OpenAI 风格 JSON：`{"prompt", "images": [{"image_url": "https://… | data:…"}], "model?", "size?", "input_fidelity?"}`；不支持 multipart。图片引用（含视频 `image_url`）最长 20 MiB 文本，可直接传 `data:` URI。
- `POST /v1/videos`：`{"prompt?", "image_url?", "seconds?"（整数或数字字符串）, "resolution?"（"768P"|"2K"）, "aspect_ratio?", "enable_audio?", "model?"}` → `{"id", "object": "video", "status": "queued", …}`；用 `GET /v1/videos/{id}` 轮询，`GET /v1/videos/{id}/content` 获取完成的视频。
- 图片默认保存到本地。未配置资源访问地址（`media_public_base_url`，WebUI「媒体生成」页）时返回 `b64_json`，不生成公开链接；显式 `response_format=b64_json` 则不保存图片。视频始终落盘。配置地址后可返回 `url` 和视频链接；资源经 `/v1/media/{images|videos}/{id}` 公开读取，带不可变缓存头。
- 普通密钥只能轮询和获取自己创建的视频任务，其他密钥的任务 ID 与不存在一样返回 404。公开 `/v1/media/...` 地址依赖不可猜测的资源 ID，停用或删除创建密钥不会撤销资源，直到媒体 TTL 到期。
- 默认模型按族固定：文生图 `hunyuan-image-v3.0`、图生图 `hunyuan-image-v2.0-general-edit`、视频 `minimax-video-h3`。媒体模型统一进入 `/v1/models` 和 WebUI「模型路由」，共用别名、启停与绑定规则；媒体模型打到聊天端点会被拒绝并指引到本节。
- 按请求选号，仅限已实测可用的媒体产品（当前为 `cn-cli`），且余额为正。静态媒体模型即使不在账号目录中也由上游裁决权限；动态模型要求目录带对应媒体标签。被明确拒绝（401/403/429）时可按 failover 配置在允许范围内换号重试一次，但上游限制码不会重放，账号限制码会禁用该账号（与聊天接口一致），5xx 与超时一律不重放以避免重复扣费。视频任务始终绑定提交账号查询。
- 多张图片并发下载，部分失败时返回成功的条目；全部失败才返回 502 且不残留已落盘文件。视频下载仅在签名失效（403/404/410）、类型被拒或超容量时永久失败，重复查询返回同一错误；其余失败（网络、超时、408/429/5xx、本地存储错误）任务保持进行中，进行中响应带 `message` 重试提示，下次查询重试。

## 值得了解的协议行为

- 先将 `developer` 转为 `system` 并置顶首条系统消息，再关联工具结果；不改动调用方原始载荷。
- Chat 兼容混入的 Anthropic `tool_use` / `tool_result` 历史，保留调用 ID、参数、结果图片与错误标记；普通 `thinking` 转为 `reasoning_content`，不混入正文，原生 Chat 字段保持不变。
- 字段冲突、工具结果无法关联、不支持的混合内容块及 `redacted_thinking` 在选路前返回 HTTP 400。需拆分的用户消息只能包含 `role`、`content`，且 `tool_result` 必须在普通文本/图片之前；Anthropic 思考签名不转发。
- Messages 的 `thinking` 块和 Responses 的可读 `reasoning` 项统一保留为 assistant `reasoning_content`，支持完整历史及工具续接；仅含加密状态的历史返回 HTTP 400。
- Messages 的 `thinking` / `output_config.effort` 与 Responses 的 `reasoning.effort` 会映射到上游思考控制；默认值和预算限制见[思考兼容](advanced.zh-CN.md#思考兼容)。
- 指定名称的函数选择会以 `required` 且仅含该函数的形式发往上游。Responses 按 `(namespace, name)` 精确匹配；未知或仅在历史中出现的选择返回 HTTP 400。
- Responses 分别保留当前与历史工具身份。Schema 清理仅移除 schema 位置的布尔 `encrypted` 标记，保留属性名、定义及字面数据。
- 命名空间工具（含嵌套和长名称）使用不超过 64 字符的合法 Chat 别名；Responses 输出与历史保留原命名空间和名称，普通工具名称保持不变。
- 错误按客户端协议各自的形态返回（OpenAI 的 `error` 对象与 Anthropic 的 `{"type":"error"}`），状态码保留。实时模式可能先送出有效增量，随后才遇到非法终端状态、断连或大小错误；合法截断/过滤仍保留协议原生区别。客户端不能仅凭 SSE 已开启就认定最终成功。
- `POST /v1/messages/count_tokens` 返回按字符估算的启发式结果，用于预算参考，不是精确计数。

完整的请求处理规则见[进阶参考](advanced.zh-CN.md#请求边界)。
