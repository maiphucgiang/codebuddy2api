# 进阶参考

[首页](../README.zh-CN.md) · [English](advanced.md)

日常操作使用 [WebUI](webui.zh-CN.md)；部署方式见[部署指南](deployment.zh-CN.md)，客户端示例见[客户端配置](clients.zh-CN.md)。

## 配置与 CLI

配置优先级：**显式 CLI 参数 > 环境变量 > WebUI 持久化配置 > 默认值**。WebUI 中可热更新的设置立即生效，标记为重启生效的设置需手动重启；锁定项须在启动配置中修改，WebUI 不改写 `.env`。

Compose 会显式传入部分环境变量及 CLI 参数，删除 `.env` 中的一行不一定解除锁定。修改这些值后重建容器；若要由 WebUI 接管，还需取消 Compose 中对应的显式设置。

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `--host` / `--port` | `127.0.0.1` / `8787` | 本地监听地址与端口 |
| `--api-key` | 无 | 管理与推理共用密钥；未设置时管理锁定 |
| `--admin-csrf [true/false]` | `true` | 管理 Origin/CSRF 校验；仅启动配置可关闭，API key 和会话鉴权不变 |
| `--admin-allowed-origins` | 无 | 额外信任的管理页来源（逗号分隔，裸域名按 HTTPS），用于反代登录；热生效，可在 WebUI 配置 |
| `--auth-file` | 扫描 `auth/` | 指定凭据文件，可重复传入；不再扫描其他文件 |
| `--log` | 无 | 额外文本日志，50 MiB 轮转、保留 2 份；不影响默认 SQLite 审计 |
| `--desensitize` | 关 | 适配固定 CLI 模板、压缩运行时提示、零宽脱敏关键词 |
| `--no-compact` | 关 | 配合脱敏保留主要行为指令，仍适配模板及裁剪运行时上下文；不关闭 Responses 投影 |
| `--keep-tool-metadata [true/false]` | `false` | 保留工具描述及参数 schema 的 `description/title`，与提示词压缩独立 |
| `--skip-check` | 关 | 跳过启动预检 |
| `--credit-price-cny` | `0.014` | 国内积分折算单价，元/Credit |
| `--credit-price-usd` | `0.03` | 国际积分折算单价，美元/Credit |
| `--usd-rate` | `7.15` | 每美元对应人民币金额，用于 billing 折算 |
| `--model-catalog-ttl` | `21600` | 模型目录缓存有效期，秒 |
| `--no-model-guard` | 关 | 关闭目录外模型的本地拦截；表外透传仅限单产品，不绕过禁用、绑定或目录就绪检查 |
| `--model-capability-guard [true/false]` | `true` | 预检模型声明的图片、工具、思考及已映射输出上限；新请求生效 |
| `--max-images` | `16` | 单请求图片总数；`0` 不允许图片 |
| `--image-policy` | `truncate` | 保留最新图片；设为 `error` 时超限返回 413 |
| `--tool-call-max-retry` | `3` | 工具参数损坏时的额外生成上限（每次都消耗额度）；`0` 不重试 |
| `--max-inbound-bytes` | `67108864` | 生成及 token 估算 POST 的解析前原始字节上限（含 chunked），超限 413；其他路由不缓冲请求体 |
| `--max-collect-bytes` | `8388608` | 聚合路径输出收集总字节上限（正文+思考+工具参数），超限返回 `response_too_large`；`0` 不限制 |
| `--max-concurrent` | `64` | 仅限制三个生成端点；占满立即 503（含 Retry-After），不限制 token 估算；`0` 不限制 |
| `--max-inflight-per-account` | `0` | 每进程、每账号的客户端推理在途上限；`0` 不限制，满载立即 503 |
| `--upstream-keepalive [true/false]` | `false` | 启用按官方入口隔离的有界连接复用；重启生效 |
| `--request-context-mode` | `legacy` | `scoped` 启用显式会话与逐尝试追踪；变更只影响新请求 |
| `--failover-max` | `0` | 请求在「一个字节都还没发给下游」之前失败时，最多再换几个凭证就地重放；`0` 表示如实把失败回给下游 |
| `--retry-write-timeout` | `false` | 让「写请求体超时」也参与重放（换新连接与 `--failover-max` 换凭证），代价是已发出的那半截正文可能已被上游处理 |
| `--max-request-bytes` | `33554432` | 处理后的上游 JSON 字节上限，须为正整数 |
| `--log-body-limit` | `65536` | 兼容文本日志正文预览字节；`0` 只记摘要，不控制 SQLite 诊断预算 |

环境变量包括 `CODEBUDDY_AUTH_DIR`、`CODEBUDDY_IMPORT_DIR`、`CODEBUDDY2API_KEY`、`CODEBUDDY2API_ADMIN_CSRF`、`CODEBUDDY2API_ADMIN_ORIGINS`、`CODEBUDDY2API_KEEP_TOOL_METADATA`、`CODEBUDDY2API_LOG`，以及 `CODEBUDDY2API_MAX_IMAGES`、`CODEBUDDY2API_IMAGE_POLICY`、`CODEBUDDY2API_MAX_REQUEST_BYTES`、`CODEBUDDY2API_LOG_BODY_LIMIT`、`CODEBUDDY2API_FAILOVER_MAX`、`CODEBUDDY2API_RETRY_WRITE_TIMEOUT`。启动示例见[部署指南](deployment.zh-CN.md)。

### 工具元数据保留

默认关闭，沿用旧策略：启用脱敏会剥离工具描述，Responses 的工具投影也会剥离描述；`--no-compact` 不改变这一行为。开启后，Chat、Responses、Messages 保留已支持工具定义中的描述及参数 schema 的字符串 `description/title`。若启用脱敏，保留的文本仍会处理；提示词压缩、现有审核兜底条件与重试次数不变，兜底也遵守本开关。

- **WebUI**：系统设置 → 保留工具描述，未被启动来源锁定时可立即生效并持久化。
- **CLI**：在原启动命令追加 `--keep-tool-metadata` 或 `--keep-tool-metadata true`；显式 `false` 可覆盖环境变量。
- **环境变量**：设置 `CODEBUDDY2API_KEEP_TOOL_METADATA=true`；Compose 会传入已设置的值，未设置时不锁定 WebUI。删除或注释变量可解除环境锁定，不要设为空串。

需使用包含此功能的源码/镜像和 Compose 配置；修改容器环境后重新创建容器。保留描述可能增加输入 token 和审核拦截风险，不保证所有账号/模型都同样兼容；设为 `false` 可恢复旧策略。此开关不恢复 Responses 原有投影裁掉的其他 schema 字段或深层节点，也不放宽请求体预算。

### 连接复用与账号容量

WebUI 系统设置可配置这两项；环境变量为 `CODEBUDDY2API_UPSTREAM_KEEPALIVE` 和 `CODEBUDDY2API_MAX_INFLIGHT_PER_ACCOUNT`，未设置时 Compose 不锁定 WebUI。连接复用默认关闭，启用后每个官方入口最多 64 条连接、保留 16 条空闲连接，空闲复用期限 30 秒；认证头逐请求设置，不保存上游 Cookie，关闭服务时释放连接池。原有代理环境、超时和重放规则不变；关闭并重启恢复逐请求连接。

账号上限默认 `0`；设为正数后，仅在原路由及免费优先范围内避开满载账号，不因免费账号满载而转向收费账号。没有名额时返回 `503 / credential_concurrency_limit` 和 `Retry-After: 3`，不排队、不熔断；结束、断连和失败换号均释放名额。管理凭据 API 提供 `in_flight`、`max_in_flight`；限制只涵盖三个客户端生成接口，每进程独立，多个实例不共享计数。设回 `0` 即恢复原容量策略，不中断已开始的请求。

### 请求上下文

生成接口响应带网关生成的 `X-Request-ID`，可关联文本日志及可用的审计明细；正文响应 ID、工具调用 ID 不变，不采用客户端请求 ID 进行鉴权或去重。

`request_context_mode` 默认 `legacy`，保留旧会话键和上游头。通过 WebUI、`--request-context-mode scoped` 或 `CODEBUDDY2API_REQUEST_CONTEXT_MODE=scoped` 启用新模式：每次客户端 HTTP 请求的根 ID 在既有重试／换号中保持不变，每次上游尝试生成独立 ID/span，会话 ID 按账号隔离。不增加重试，不保存服务端历史，不自动生成缓存键。

scoped 模式可选传入 `X-Codebuddy-Session-ID`、`metadata.conversation_id` / `metadata.conversationId` 或顶层 `conversation_id` / `conversationId`。多处值须一致；冲突、非字符串、控制字符或超过 512 UTF-8 字节时返回 400。空值回退为协议适配后的指令与首条用户输入指纹，包含图片引用但不抓取 URL；缺少可靠输入时使用临时会话。无显式 ID 的相同输入仍无法区分；`user`、`metadata.user_id`、`prompt_cache_key` 不是会话 ID。原始标识不记录、不转发上游。

设回 `legacy` 即恢复新请求的旧行为，在途请求保留入口模式。

## 自动化与奖励

自动签到与 Buddy 旅行是按账号设置的开关（WebUI 凭证页）：国内默认开启，国际默认关闭，保存即生效，无需重启。

`CODEBUDDY2API_AUTO_ACCEPT_BUDDY` 仅启动读取，默认 `false`；预授权已启用国内账号完成首次领猫任务、协议及旅行，自动旅行仍受账号开关控制。`first_buddy` 无需单独接取，包含 `not_accepted` 在内的待完成状态可直接发起一次本账号的真实国内 WorkBuddy 对话；优先可用零倍率模型，否则取最低已知倍率，最多请求 32 个输出 token，可能消耗少量积分。不执行其他奖励任务、不付费开盒、不切猫、不领取国际试用积分。

手动 `POST /admin/credentials/{id}/travel` 返回 `buddy_confirmation`，包含官方条款与独立的 `authorization` 自动化范围。勾选后提交 `{"confirm_buddy":true,"agreement_revision":"<返回的版本>"}`；旧版仅领猫授权失效。`can_claim` 仅表示资格，不禁用同意。

`control.sqlite3` 保留同意及每账号一次实际新手对话，重启不重复。发送前明确取消时仅撤销本次未发送预留，未知或已发送的尝试不自动释放；旧接取记录不阻塞尚未发送的对话。仅官方任务完成才继续领猫。首次领取结果不确定时，超过 24 小时仍保留预留、仅回查；未进入领取阶段的失败可在退避后续办。升级保留该数据库，旅行状态与余额同步不触发任务。

旅行奖励领取与派遣共用按账号隔离的写入预留，不确定结果不超时重放；状态查询只回查并更新本地确认记录，不发上游写请求。存储失败停止领取和派遣，跨进程确认不覆盖其他请求的记录。

体验积分仅供符合官方资格的 `intl-work` 账号手动领取：使用凭证行的领取抽屉或 `POST /admin/credentials/{id}/trial`。启动、定时维护、余额同步均不领取。结果显示安全错误类别、HTTP 状态／业务码及重试时间，响应正文限制为 64 KiB 且不返回浏览器。成功或已领取记录保存在 `auth/trial-ledger.json`，失败至少等待 24 小时才能再次手动申请；升级时保留该文件。

`CODEBUDDY2API_AUTO_TRIAL` 和 `--auto-trial` 已停用：旧启动选项仅提示、不触发任务；控制库中的旧布尔 `auto_trial` 设置在加载时忽略。请从部署配置中移除；回滚旧代码前也须核对这些旧设置，避免重新启用自动领取。

## API 与鉴权

| 客户端接口 | 说明 |
|------------|------|
| `POST /v1/chat/completions` | OpenAI Chat Completions |
| `POST /v1/responses` | OpenAI Responses |
| `POST /v1/messages` | Anthropic Messages |
| `POST /v1/messages/count_tokens` | 按字符启发式估算 token，仅作预算参考，不是精确计数 |
| `GET /v1/models` | 可用模型、倍率及按产品区分的安全元数据 |
| `GET /v1/dashboard/billing/subscription` | 积分折算额度；`codebuddy_balance_usd` 为剩余余额 |
| `GET /v1/dashboard/billing/usage` | 美分计量的 `total_usage` 与按日明细 |

`hard_limit_usd` 是剩余额度加已用额度的美元折算，不是剩余余额。未指定日期范围时，余额等于 `hard_limit_usd - total_usage / 100`；这些是本地折算值，不是分发计费系统。

| 管理与共用接口 | 说明 |
|----------------|------|
| `GET /health` | 公开存活检查，仅返回 `{"status":"ok"}` |
| `GET /admin/credentials` | 凭证列表与运行状态 |
| `POST /admin/credentials` | 从服务端受控目录导入 `.info` |
| `DELETE /admin/credentials/{name}` | 按文件名删除凭证文件；仍被模型绑定引用时返回 409 |
| `PATCH /admin/credentials/{id}` | 按账号设置单个布尔字段：`enabled`、`auto_checkin` 或 `auto_travel`；保存自动任务开关不立即领取 |
| `POST /admin/oauth/start` · `GET /admin/oauth/poll` | 发起与轮询登录；`site=cn`（默认）、`intl`（国际 WorkBuddy）或 `intl-codebuddy`（国际 CodeBuddy） |
| `GET /admin/credits` · `POST /admin/checkin` | 查询额度；按日幂等签到，国内按开关继续旅行 |
| `POST /admin/sync` | 同步全部启用账号的余额、目录和用量，不签到、不领取试用 |
| `POST /admin/credentials/{id}/{action}` | 单账号 `refresh`、`checkin`、`sync`、`travel-status`（仅查询）、`travel`（领取后派出）、`trial`（一次性体验积分）或 `reset-cooldown`（仅本地状态） |

旅行结果包含 `phase`、可选的安全诊断 `error_kind`/`http_status`/`code` 和查询时的 `remaining_seconds`。后续查询失败会设置 `ok=false`、`stale=true`，但保留已确认的 `claimed`/`departed`；再次操作前先查询核验。

`reset-cooldown` 不接受请求体，会清除该账号的全部冷却：既包括 401/403 认证熔断，也包括按模型的 429 冷却。这样在冷却被误判或上游已恢复时，无需重启网关即可放行。它只修改本地状态：不刷新 Token、不访问上游、不排队同步，人工停用的账号同样可用。结果区分 `changed_in_memory` 与 `durable`；写入失败时 `ok` 为 false，此时内存冷却已清除但重启会恢复盘上记录，直接重试该请求即可。

页面使用 `/dashboard/*`，管理 API 使用 `/admin/*`，客户端保留原 `/v1/*`；不注册 `/cn`、`/intl` API 前缀。模型自动选路不要求客户端改变地址。

管理必须配置 API key；WebUI 使用同 key 建立 HttpOnly 管理 Cookie，Cookie 仅授权 `/admin/*`，不能用于 `/v1/*`。命令行 API 请求携带 `Authorization: Bearer <key>` 或 `X-Api-Key`。空 key 仅保留推理接口的历史无鉴权行为，不开放管理；`/health` 不返回账号、路径或异常详情。

### 管理 Origin / CSRF 开关

默认开启。OAuth 轮询在 `Origin`、`Sec-Fetch-Site` 均缺失时，兼容同源 `Referer`（协议、主机、端口一致），仍要求有效 CSRF token。已有 `Origin` 优先校验；无 `Origin` 但有 Fetch Metadata 时，只接受 `same-origin`，不会再用 Referer 回退。登录和写操作仍要求 Origin。

正常同源访问不需要关闭保护。反代改写转发的 Host 或协议时（例如域名 HTTPS 访问而容器内看到 HTTP），浏览器 Origin 与服务端看到的地址不一致，登录会报 Origin 校验失败。把对外地址加入 `admin_allowed_origins`（WebUI 系统设置，即时生效），或设置 `CODEBUDDY2API_ADMIN_ORIGINS` / `--admin-allowed-origins`：逗号分隔的来源或裸域名（如 `https://chat.example.com`、`chat.example.com`，裸域名按 HTTPS），最多 32 条。CLI 或环境变量显式设置后 WebUI 字段锁定。优先使用此白名单而非关闭保护；若仍报错，先统一访问地址、检查反代传递的 Host 和协议，并刷新页面重新登录。仅在受信任本地环境需要关闭时，在原启动命令追加 `--admin-csrf false`，或在已有 `.env` 中设置：

```dotenv
CODEBUDDY2API_ADMIN_CSRF=false
```

仅启动时生效，CLI 优先于环境变量，不能通过 WebUI 修改。使用包含该开关的源码或镜像，并更新 Compose 配置；修改环境后须重新创建容器，不能仅执行 `docker compose restart`。旧镜像不会因新增变量自动获得此功能。

关闭会跳过登录请求的 Origin 检查，以及 Cookie 管理写操作和 OAuth 轮询的 Origin/CSRF 检查；API key、会话有效期、OAuth 任务归属、官方授权地址白名单和危险操作确认仍保留，不影响 `/v1/*`。

**关闭会降低浏览器跨站请求保护，不应将此配置直接暴露到公网。** 恢复 `--admin-csrf true` 或环境变量 `true` 并重启即可重新启用。

### 服务端路径导入

WebUI 可以直接上传文件；以下限制针对 `POST /admin/credentials` 的路径导入：

- 将文件放入 `auth/imports/`，或 `CODEBUDDY_IMPORT_DIR` 指定的服务端目录。
- 仅接受直接子级普通 `.info` 文件，拒绝符号链接、子目录及超过 1 MiB 的文件。
- 请求体为 `{"path":"account.info"}`，也可填写该文件的绝对路径。同名文件按导入规则更新。
- 身份由产品 profile、UID 和租户共同确定；另一文件已持有同一身份时返回 409。同 UID 的不同产品或租户可以共存。删除接口使用文件名，启停接口使用身份 ID，二者不要混用。

## 模型与调度

以 WebUI 和 `GET /v1/models` 为客户端选择依据。原始目录仍按账号/租户、地域、产品与客户端版本缓存到 `auth/model-catalog.json`，默认有效期 6 小时；新凭据触发同步，刷新失败保留该账号的可信旧缓存。旧未隔离缓存不作为国际共享来源。

国际 CLI／WorkBuddy 使用已启用、目录已就绪的国际账号生成去重共享视图。目标账号须完成自身目录同步；已有型号保留自己的完整声明，缺失型号才继承，并通过 `catalog_source`、安全 `source_variants` 标明来源。继承声明冲突时倍率取较高值、上限取较小值、思考选项取交集、描述类字段不一致即省略；未知倍率不当零。国内目录、凭据、余额、绑定及 `auto` 默认模型保持独立；共享倍率只是目录参考，不保证权限或实际扣分。

每次 `/v3/config` 刷新将选择器子集缓存为 `models`，账号根表缓存为 `serves`。选路与 `GET /v1/models` 合并这两组候选，同名保留选择器元数据；`disabled` 和 `availableModels` 筛选仍生效，不支持工具调用的模型会被排除。

根表不保证每个模型都可调用，仍由实测 `11102` 避让兜底。未知账号目录不参与派发，也不能触发过早的「全部后端不支持」404，应保留可重试的未就绪状态。旧缓存没有 `serves` 时，在下次刷新前继续使用选择器子集。

标准模型字段之外，`credits` 是各来源最低倍率：`0.0` 表示该来源零倍率，`null` 表示未声明可解析倍率。`credits_by_profile` 提供来源明细，例如 `{"intl-work":0.0,"cn-cli":0.03}`；兼容客户端可忽略这些扩展字段，倍率不保证永久不变。

凭据 domain / token issuer 决定产品身份，聊天与刷新使用各自固定入口及独立产品头：

| Profile | 聊天 / 刷新入口 |
|---------|-----------------|
| `cn-cli` | `https://copilot.tencent.com` |
| `cn-work` | `https://www.workbuddy.cn` |
| `intl-cli` | `https://www.codebuddy.ai` |
| `intl-work` | `https://www.workbuddy.ai` |

- 国内按自身可信目录选路，国际使用上述共享视图；具体零倍率模型优先，其次按积分过期时间、冷却与会话黏绑调度。余额始终不跨账号借用。
- 零余额账号退出付费模型轮询，仍可使用有效目录中明确零倍率的具体模型；余额恢复后重新加入。国际付费模型须有已知正余额。
- `auto` 是账号默认模型的调度别名，不代表任意模型。国际账号须有正余额且目录声明 `default-model`；国内 WorkBuddy 须声明 `auto`，国内 CLI 须有已知非空可用目录。`auto` 不享受具体零倍率模型的余额豁免。
- WebUI 的地域、产品和凭证绑定严格限制候选账号，不会回退到未选账号。模型禁用后直接请求同样拒绝；改名默认不保留原 ID，只有选择保留时才同时提供旧 ID。
- 已发送请求不会因账号不可用或 HTTP 错误换账号重放；后续请求才重新选路。目录同步或凭证未就绪通常返回带 `Retry-After` 的 503，不支持或禁用的模型返回 404。

### 模型声明与图片兼容

`/v1/models` 保留原字段，增加 `capabilities`、`limits`、`metadata_by_profile`，管理接口及路由预览同步提供。能力状态为 `supported`、`unsupported`、`mixed`、`unknown`；限制含 `state`（`known`、`mixed`、`unknown`）和 `value`，仅已知且一致时返回数值。按产品保留不同账号的声明版本，但不公开账号身份。白名单覆盖说明、能力、窗口、思考选项、关联模型及参数建议；凭据、内部配置和带认证信息的 URL 不公开。上游声明不等于原生能力或实测保证，参数建议不自动覆盖请求。

`model_capability_guard` 默认 `true`，可在 WebUI、`--model-capability-guard false` 或 `CODEBUDDY2API_MODEL_CAPABILITY_GUARD=false` 关闭。仅在现有绑定和当前免费优先范围内筛选，明确不兼容返回 400、不发上游；未知能力兼容放行，每条请求固定入口开关。检查图片、工具及历史、已声明思考选项，以及 `max_tokens` 输出上限（含 Responses 映射的 `max_output_tokens`）；不估算输入 token，不对 `max_completion_tokens` 改名或套用该上限，不转换 Anthropic 思考预算。关闭不绕过鉴权、目录授权、容量或大小限制。

两种国际产品在选路后归并含图的连续 `user` 段，保留内容顺序和图片数据；国内请求、纯文本段及 system/assistant/tool 边界不变。消息级属性冲突或内容无法无损表达时返回 `400 / image_user_run_not_mergeable`，最终字节限制仍生效。图片兼容不随能力开关关闭，不增加重试，也不让文本模型获得原生视觉。

## 请求边界

- 三个生成协议统一将 `developer` 归一为 `system`，已有 system 移到首位，缺失时补默认值；归一化不修改调用方 payload。Responses 上下文投影和可选脱敏另行处理内容，不能据此理解为整个链路逐字透传。
- 图片计入全部历史和工具结果，重复图片逐次计数，按消息与内容块数组顺序判断新旧。默认保留最新 16 张，只移除超额图片并保留文本和消息结构；图片清空的内容用文本占位。
- `--image-policy error` 在本地返回 `413 / too_many_images`。处理后仍超过字节上限则返回 `413 / request_too_large`，不为满足预算继续截断文本。
- 图片数量合规不保证单图大小或模型视觉能力满足上游要求。URL/base64 图片可转换，Responses 图片 `file_id` 不支持。
- 省略 `stream` 时三个端点都按协议默认返回完整 JSON（非流式）；`stream` 必须是布尔值。Responses 流式以及带工具的 Chat / Messages 流式先聚合校验，再输出 SSE，并非所有路径都实时逐 token 转发。
- 推理错误按客户端协议成形：OpenAI 路由为顶层 `error` 对象，Messages 路由为 `{"type": "error", ...}`；保留状态码，开流后的错误只用 SSE 报告，不重放。
- 上游有效 `Retry-After`（0–86400 秒或对应 HTTP 日期）规范化为秒并在开流前返回；429 仅冷却对应账号/模型。无效或过期值回落正文重置时间或默认 600 秒；本地全凭据冷却的 429 返回剩余等待秒数。
- Chat 与 Responses 保留客户端显式 `prompt_cache_key`，不自动生成；缓存命中和节费取决于上游。
- 不支持的能力显式拒绝而非静默降级：Chat 的 `n≠1`、Responses 的 `previous_response_id`/`conversation`（本网关不保存服务端响应状态）返回 400；长度截断或审核过滤的 Responses 标记为 `incomplete`，不伪装为 `completed`。
- 兼容文本日志和 SQLite 审计使用独立预算；日志仅记录有界、脱敏预览，不是完整原始请求。日志、凭证导出和备份仍须按私有数据保管。

## 部署暴露与凭据导入

- Compose 端口映射默认只绑回环（`CODEBUDDY2API_BIND` 默认 127.0.0.1）；原生运行在合并 CLI、环境和持久化设置后，若实际地址非回环且生效 key 为空则拒绝启动，须显式设 `CODEBUDDY2API_ALLOW_OPEN_NOAUTH=true` 放行。
- 配置 key 时，生成及 token 估算 POST 在缓冲请求体、预留推理名额之前校验请求头；即使名额已满，无效 key 仍返回 401。其他路由保留原有鉴权和路由行为。
- 凭据导入/上传在落盘前把 token 别名归一化为官方字段名；严格 JSON 解析拒绝 NaN/Infinity，`expiresAt`/`lastRefreshTime` 必须是合理的有限毫秒时间戳。

## 账务数据完整性

- 余额/用量来自官方接口的分页遍历；达到页数上限或任一账号同步失败时，响应带 `partial: true`（及 `stale_accounts` 列表），不伪装为精确全量。
- 单账号同步失败保留其上次成功快照；接口返回 HTTP 200 但业务码失败或结构缺失时按错误处理，不覆盖历史。
- 首次同步全部失败时，两个账务端点仍标记数据不完整及失败账号，并保留额度差回退；已有账号快照在失败时不更新其成功时间。
- daily_costs 按各站（国内/国际）本站单价逐日折算，不再使用全局平均单价。

## 故障与重试

| 现象 | 处理与边界 |
|------|------------|
| WebUI 无法登录 | 确认设置了 API key；更换 key 后重新登录并重新发起未完成的 OAuth。HTTPS 反代后经域名登录失败时，用 `admin_allowed_origins` 信任对外来源（见上文） |
| 本地 401 | 客户端密钥与网关不一致 |
| 上游 401 / 403 | 凭证级认证熔断；在 WebUI 检查并重新登录 |
| 429 | 对该凭证的上游模型冷却，后续请求自动换绑；全部候选都在冷却时仍返回 429。设了 `--failover-max` 时，当前请求就地换凭证重放 |
| 上游 `service info not found`（11102） | 明确的 400/404 模型拒绝：国内按（后端、模型）退避，国际按（账号、产品、模型）隔离；无可用候选时返回 404。6 小时后半开，反复命中最长 24 小时，成功即解除。旧国际入口级记录不再拦截账号；用 `GET /admin/model-blocks` 查看 |
| 建连失败 | `ConnectError` / `ConnectTimeout` 换新连接重放一次：两者都发生在写下第一个正文字节之前，上游手里什么都没有，重放不会重复计费 |
| 发送后断连、读超时、协议错误 | 不做网络重放，避免重复计费；日志记录异常类型与耗时 |
| 非流式响应还没成形，客户端就挂断 | 立刻取消这次上游调用并归还并发名额，审计记为 `cancelled`，不会被记成一次已完成的回答；流式本来就是这一行为 |
| 流式在第一个字节之前失败 | 按真实状态码返回，与 `stream=false` 同口径。只带一个流内 `error` 事件的 200 会被客户端读成「模型答了个空」，会话静默结束，审计里还记成一次成功 |
| 换凭证重放（`--failover-max`） | 默认关闭。开启后，失败发生在「一个字节都没发给下游」之前时换一个凭证重放，最多 N 次，审计记为 `success` 并留下 `failover_recovered` 尝试标记。可重放的失败：上游 HTTP 401/403/429/502/503/504 拒绝，与确定没开始收正文的传输失败（建连失败/超时）；内容审核拒绝、上游已回 200 后合成的 502、读超时与协议错误一律不重放；换不出其他凭证时如实回第一次的状态码。计费口径：401/403/429/503 与建连类失败发生在受理阶段，不会扣费；502/504 可能已被上游处理并计费，但结果到不了下游，不重放也退不回额度——只是把一次已付费请求变成断掉的会话。这类重放在日志里标注「上游可能已处理该请求」，便于对账 |
| 写超时重放（`--retry-write-timeout`） | 默认关闭。写超时只能证明正文没发完，不能证明上游忽略了已收到的部分，因此默认既不参与连接重试也不参与换凭证重放；跨境长会话比握手更容易遇到写超时，确认上游不按半截正文计费后再开启。这类重放同样带「上游可能已处理该请求」日志标记 |
| 工具参数损坏 | 聚合校验失败按 `--tool-call-max-retry`（默认 3）额外生成，可能消耗更多额度；被丢弃的生成带用量记入尝试明细；耗尽后返回错误 |
| 上游空流或残流 | 没有有效输出、缺少结束标记或包含错误的流不伪装为成功 |
| 内容审核拒绝 | 脱敏 + `--no-compact` 下，仅完整非流式纯拒绝且模板确实缩短时，最多同账号兜底一次；流式不做审核重试，也不因此熔断或切号 |
| 响应慢 | 在 WebUI 查看耗时与失败尝试，再选择当前账号支持的更快模型 |
| 同账号多处登录相互失效 | 桌面端与网关独立刷新可能互相顶掉；优先独立扫码登录或停止另一端使用 |

## 降级与回滚

功能开关不藏隐状态：关闭守卫或模式即对新请求停止生效，回退源码即恢复旧行为。例外是持久化设置与自动化状态：`control.sqlite3` 保存 WebUI 设置、模型规则与奖励预留，旧代码会拒绝未知字段。源码降级前移除新增的启动参数，并恢复升级前的控制库备份（含 WAL/SHM 文件，不混用）。回滚无法撤销已完成的上游签到、领取或旅行派出。
