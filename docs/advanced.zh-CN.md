# 进阶参考

[首页](../README.zh-CN.md) · [English](advanced.md)

日常操作使用 [WebUI](webui.zh-CN.md)；部署方式见[部署指南](deployment.zh-CN.md)，客户端示例见[客户端配置](clients.zh-CN.md)。

## 配置与 CLI

配置优先级：**显式 CLI 参数 > 进程环境变量 > `.env` > SQLite 保存值 > 默认值**。热更新设置立即生效，标记为重启的项需手动重启；锁定项请在对应来源修改，WebUI 不改写 `.env`；普通密钥可再次查看，管理密钥只保存哈希。

Compose 会加载 `.env` 中的每个键，注释掉一行即可解除锁定；只有 `CODEBUDDY_AUTH_DIR` 和 `CODEBUDDY2API_ALLOW_OPEN_NOAUTH` 由 Compose 文件固定。修改这些值后重建容器。

`.env` 里的运行参数在 WebUI 中始终只读，直到被搬进配置库：停止服务后执行 `python3 converter.py config migrate`（见[配置迁移](deployment.zh-CN.md#配置迁移)）。Dashboard 会显示一次性横幅列出这些键；迁移或点击“不再提醒”都会在配置库写入 `config_notice_muted` 并停止提示，之后新增的 `.env` 运行参数也不再提醒。

`language`（`auto|zh|en`）是 WebUI 与 CLI 共用的唯一界面偏好：没有环境变量映射，也永不被锁定。`--lang` 会在 `serve`、`login`、`config migrate` 时写入该值；`config status` 只读，仅影响本次输出。

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `--host` / `--port` | `127.0.0.1` / `8787` | 本地监听地址与端口 |
| `--api-key` | 未设置 | 外部管理推理密钥及初始 WebUI 密码；空白按未设置处理，数据库密钥不会提升为该值 |
| `--admin-csrf [true/false]` | `true` | 管理 Origin/CSRF 校验；仅启动配置可关闭，API key 和会话鉴权不变 |
| `--admin-allowed-origins` | 无 | 额外信任的管理页来源（逗号分隔，裸域名按 HTTPS），用于反代登录；热生效，可在 WebUI 配置 |
| `--auth-file` | 扫描 `auth/` | 指定凭据文件，可重复传入；不再扫描其他文件 |
| `--log` | 已停用 | 提示弃用且不写文件；日志统一在 WebUI 查看 SQLite 记录 |
| `--desensitize` | 关 | 适配固定 CLI 模板、压缩运行时提示、零宽脱敏关键词 |
| `--prompt-mode` | `empty` | `empty` 不附加网关默认 system 提示词；`prepend` 在最前面插入精简的 WorkBuddy／CodeBuddy 官方前缀；热生效，可在 WebUI 配置 |
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
| `--responses-projection-mode balanced\|passthrough` | `balanced` | 仅改写有固定摘要的已识别 harness；passthrough 完全关闭 Responses 投影 |
| `--responses-projection-max-bytes` | `40000` | assistant、完整工具参数 JSON 与工具结果的单项 UTF-8 字节上限；`0` 禁用，`256..33554432` 为有效范围 |
| `--tool-call-max-retry` | `3` | 工具参数损坏时的额外生成上限（每次都消耗额度）；`0` 不重试 |
| `--max-inbound-bytes` | `67108864` | 生成及 token 估算 POST 的解析前原始字节上限（含 chunked），超限 413；其他路由不缓冲请求体 |
| `--max-collect-bytes` | `8388608` | 聚合及实时校验所需保留输出的总字节上限（正文+思考+工具参数/元数据），超限返回 `response_too_large`；`0` 不限制 |
| `--stream-mode compatible\|realtime` | `compatible` | `compatible` 保持聚合/片段重放行为；`realtime` 让三个协议增量发送，且不重生成工具参数 |
| `--max-concurrent` | `64` | 仅限制三个生成端点；占满立即 503（含 Retry-After），不限制 token 估算；`0` 不限制 |
| `--max-inflight-per-account` | `0` | 每进程、每账号的客户端推理在途上限；`0` 不限制，满载立即 503 |
| `--upstream-keepalive [true/false]` | `false` | 启用按官方入口隔离的有界连接复用；重启生效 |
| `--request-context-mode` | `legacy` | `scoped` 启用显式会话与逐尝试追踪；变更只影响新请求 |
| `--failover-max` | `0` | 请求在「一个字节都还没发给下游」之前失败时，最多再换几个凭证就地重放；`0` 表示如实把失败回给下游 |
| `--retry-write-timeout` | `false` | 让「写请求体超时」也参与重放（换新连接与 `--failover-max` 换凭证），代价是已发出的那半截正文可能已被上游处理 |
| `--max-request-bytes` | `33554432` | 处理后的上游 JSON 字节上限，不含网关内部元数据，须为正整数 |
| `--log-body-limit` | `65536` | 旧文本预览兼容项；文本输出已停用，SQLite 诊断使用独立预算 |

全部生效的环境变量及默认值见 `.env.example`；启动示例见[部署指南](deployment.zh-CN.md)。

### Responses 投影

通过 WebUI、CLI、环境变量或 `.env` 配置这两项热更新设置。优先级为 CLI > 进程环境变量 > `.env` > SQLite 保存值 > 默认值；CLI/环境变量会锁定 WebUI 字段。Compose 整体加载 `.env`，注释掉对应行即保持 WebUI 可编辑。

`responses_projection_mode` 默认为 `balanced`，仅接受 `balanced` 和 `passthrough`。balanced 仅改写有固定摘要的已识别 harness；这些块之外的文本不计入预算、不会被裁剪。它还会按 `responses_projection_max_bytes` 对 assistant 内容、完整工具参数 JSON 和工具结果采用 Codex 风格头尾截断；文本标记显示原始 bytes、估算 tokens 与总行数。超限工具参数仍是有效 JSON，并以有界对象保留原始头尾和大小元数据。passthrough 完全关闭 Responses 投影。

`responses_projection_max_bytes` 默认 `40000`，有效范围为 `0` 或 `256..33554432`。`0` 只禁用单项裁剪，仍受全局入站/请求与输出门禁约束。两项设置均不改变客户端 Base URL。

降级到不含这些键的旧源码时，需按[流式模式](#流式模式)所述离线删除 `settings.responses_projection_mode` 与 `settings.responses_projection_max_bytes`。

### 工具元数据保留

Responses 投影不再修改工具定义或 schema。启用脱敏时，默认剥离工具描述及参数 schema 的字符串 `description/title`；开启本开关后，Chat、Responses、Messages 会保留并按脱敏规则处理这些文本。`--no-compact` 不改变本开关。

- **WebUI**：系统设置 → 保留工具描述，未被启动来源锁定时可立即生效并持久化。
- **CLI**：在原启动命令追加 `--keep-tool-metadata` 或 `--keep-tool-metadata true`；显式 `false` 可覆盖环境变量。
- **环境变量**：在 `.env` 中设置 `CODEBUDDY2API_KEEP_TOOL_METADATA=true`；注释掉该行即解除锁定，不要设为空串。

需使用包含此功能的源码或镜像；修改容器环境后重新创建容器。保留描述可能增加输入 token 和审核拦截风险；设为 `false` 恢复脱敏默认剥离行为。此开关不改变 Responses 的 balanced/passthrough 模式，也不放宽请求体预算。

### 流式模式

`stream_mode` 默认 `compatible`，可通过 `--stream-mode compatible|realtime`、`CODEBUDDY2API_STREAM_MODE` 或 WebUI 枚举配置；显式 CLI/环境来源会锁定该项。所有生成请求（含非流式）均在选路前冻结模式及 `max_collect_bytes`，并记录所选模式。热更新只影响后续请求，不改变在途请求及换号；非流式仍返回聚合 JSON，客户端不能按请求覆盖模式。

- `compatible` 保持现有行为：Responses 流式先聚合；Chat/Messages 带工具时先聚合，无工具时沿用上游增量。聚合结果先校验，再按片段重放。两种模式下，非流式请求始终走已校验的聚合路径。
- `realtime` 让三个协议都实时增量发送正文、拒绝、工具参数与思考，客户端能看到真实的思考时间轴；Responses 与 Anthropic 仍只产生一个逻辑思考项/块并使用稳定索引。`compatible` 保留旧行为：在正文开始前把思考合并成一个事件，会隐藏思考时间轴，并可能让客户端 TPS/首字指标虚高。适配器在参数阶段或结束标记处确认工具身份，缺失时暂缓工具输出；元数据分片按顺序追加，不按字符串前缀猜测，输出项开始后禁止更换身份。`max_collect_bytes` 约束所保留的 UTF-8 输出，`0` 不限制。

实时 Messages 同时只打开一个内容块：当前工具仍增量输出，后续工具、正文或思考块可能等待上游结束，暂存事件字节计入 `max_collect_bytes`。尚未确认身份、未开始内容块的工具不会阻塞其它正文。

实时模式绝不重生成损坏或不完整的工具参数。发送成功终端前，会在终端边界校验工具 ID、名称、已声明名称、JSON object 参数及 `tool_choice`。实时模式下，存在工具调用时还必须带上游 `tool_calls` 结束标记；带工具却标为 `stop` 会拒绝，而 compatible 模式保留旧的兼容接受行为。下游尚未收到字节时，失败保留真实上游 HTTP 状态及既有、有界的响应前换号规则；已发送任何字节后，参数损坏、断连、流错误和预算超限均以协议错误终端结束，不重放或切换凭据。合法的 `length`、拒绝和审核结果保留各自协议区别（Responses 的截断/过滤为 `incomplete`，绝非 `completed`），且不会重生成。因此客户端必须接受「部分正文后跟错误」，不能假定已开流的 SSE 一定成功结束。审计只增加白名单 `stream_mode` 标记及上游实际提供的用量。

明确的纯审核拒绝即使没有正文也保留原生终态：实时 Responses 返回 `response.incomplete`，原因是 `content_filter`，并保留已有用量。普通空响应、缺少终止标记及错误帧仍不得伪装成功。

运行时选回 `compatible` 即可恢复聚合流式及工具参数重生成，保留当前保存状态。源码降级前，移除新增 CLI/环境选项，停止网关并备份**当前**数据目录及同一代 SQLite/WAL/SHM。不要恢复升级前旧库，否则可能回滚新的领取、会话、撤销和账号状态。下例会离线写入当前数据库：仅删除列出的 `settings.<name>` 键、递增 revision 并检查完整性，其它设置和表保持不变；可传一个或多个键。禁止对运行中的数据库执行或混用 SQLite 文件代数。

```sh
python3 - /path/to/control.sqlite3 settings.stream_mode <<'PY'
import json, sqlite3, sys
path, names = sys.argv[1], sys.argv[2:]
keys = [name.split(".", 1)[1] for name in names if name.count(".") == 1 and name.split(".", 1)[1]]
if len(keys) != len(names):
    raise SystemExit("pass one or more settings.<name> keys")
con = sqlite3.connect(path)
try:
    con.execute("BEGIN IMMEDIATE")
    row = con.execute("SELECT revision,payload FROM control WHERE id=1").fetchone()
    if row is None:
        raise SystemExit("missing control row")
    revision, payload = row
    data = json.loads(payload)
    if set(data) != {"settings", "models", "credentials"} or not isinstance(data["settings"], dict):
        raise SystemExit("unexpected control payload")
    present = [key for key in keys if key in data["settings"]]
    if not present:
        raise SystemExit("no requested key is present; no write needed")
    for key in present:
        del data["settings"][key]
    con.execute("UPDATE control SET revision=?,payload=? WHERE id=1",
                (revision + 1, json.dumps(data, ensure_ascii=False, allow_nan=False)))
    con.commit()
    if con.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
        raise SystemExit("integrity check failed")
finally:
    con.close()
print("ok")
PY
```

旧版严格设置校验器会拒绝这个未知键，仅把值改成 `compatible` 不能让旧源码兼容。该流程必须离线执行；没有当前备份和已停止服务时，不要对生产库执行。

### 连接复用与账号容量

WebUI 系统设置可配置这两项；环境变量为 `CODEBUDDY2API_UPSTREAM_KEEPALIVE` 和 `CODEBUDDY2API_MAX_INFLIGHT_PER_ACCOUNT`，在 `.env` 中注释掉对应行时保持 WebUI 可编辑。连接复用默认关闭，启用后每个官方入口最多 64 条连接、保留 16 条空闲连接，空闲复用期限 30 秒；认证头逐请求设置，不保存上游 Cookie，关闭服务时释放连接池。原有代理环境、超时和重放规则不变；关闭并重启恢复逐请求连接。

账号上限默认 `0`；设为正数后，仅在原路由及免费优先范围内避开满载账号，不因免费账号满载而转向收费账号。没有名额时返回 `503 / credential_concurrency_limit` 和 `Retry-After: 3`，不排队、不熔断；结束、断连和失败换号均释放名额。管理凭据 API 提供 `in_flight`、`max_in_flight`；限制只涵盖三个客户端生成接口，每进程独立，多个实例不共享计数。设回 `0` 即恢复原容量策略，不中断已开始的请求。

### 请求上下文

生成接口响应带网关生成的 `X-Request-ID`，可关联文本日志及可用的审计明细；正文响应 ID、工具调用 ID 不变，不采用客户端请求 ID 进行鉴权或去重。

`request_context_mode` 默认 `legacy`，保留旧会话提示行为。legacy 会话 ID 在同账号、同会话中保持稳定，但故障转移选择其他账号时会变化，即使两个账号属于同一 profile。通过 WebUI、`--request-context-mode scoped` 或 `CODEBUDDY2API_REQUEST_CONTEXT_MODE=scoped` 启用新模式：每次客户端 HTTP 请求的根 ID 在既有重试／换号中保持不变，每次上游尝试生成独立 ID/span，会话 ID 按账号隔离。不增加重试，不保存服务端历史，不自动生成缓存键。

scoped 模式可选传入 `X-Codebuddy-Session-ID`、`metadata.conversation_id` / `metadata.conversationId` 或顶层 `conversation_id` / `conversationId`。多处值须一致；冲突、非字符串、控制字符或超过 512 UTF-8 字节时返回 400。空值回退为协议适配后的指令与首条用户输入指纹，包含图片引用但不抓取 URL；缺少可靠输入时使用临时会话。无显式 ID 的相同输入仍无法区分；`user`、`metadata.user_id`、`prompt_cache_key` 不是会话 ID。原始标识不记录、不转发上游。

设回 `legacy` 即恢复新请求的旧行为，在途请求保留入口模式。
上游客户端身份按已核实的官方包对齐：国内 CLI 2.165.0／WorkBuddy 5.9.3，国际 CLI 2.161.0／WorkBuddy 5.8.4。国内 WorkBuddy 使用会话稳定的 UUIDv7 会话 ID、逐次请求／追踪头，并在 JSON 请求体达到 1024 字节时使用 gzip；CLI 与国际 WorkBuddy 保持原有 ID 关系，聊天 JSON 始终使用 gzip。所有身份头均在本地生成，不读取硬件 ID。

## 自动化与奖励

自动签到与 Buddy 旅行是按账号设置的开关（WebUI 凭证页）：国内个人账号默认开启，国际默认关闭，保存即生效，无需重启。成长中心仅面向个人账号，企业账号不参与旅行（也不再显示上游 403）。

成长中心补领（`auto_growth`，国内个人账号默认开启）随每小时维护扫描执行，只做接取与领取：领取已完成任务的积分与能量，自身不启动任何行为链。任务级 `growth_auto` 开关（默认全部关闭）把指定任务纳入自动化，扫描随后驱动对应行为链——可能消耗积分的真实对话、应用体验或专家召唤。凭证页展示实时任务列表，支持单任务与已开开关任务的一键手动执行，任务开关可复制到已选账号（`POST /admin/credentials/growth-batch`）；面板由 `GET /admin/credentials/{id}/growth-tasks`、`.../growth-tasks/auto` 与 `.../auto-all` 支撑。手动执行是服务端后台任务（每个账号同时只有一个）：请求立即返回任务，关闭抽屉或页面不影响执行，运行中再次发起会返回当前任务；`GET /admin/growth-jobs` 列出各账号当前或最近一次任务及正在执行的任务，供页面横幅显示。指定专家任务使用已核实的任务专属专家 ID，沿用需主动开启的真实对话链。手动执行与扫描领取均入审计；企业与国际账号不显示该面板。
签到、旅行、成长和活跃打卡的结果或异常携带 11140 时，与推理共用持久两小时官方限制状态。当前扫描立即停止，包括剩余成长领取；限制到期或人工解除前，后续扫描跳过该账号；其他业务码保持原有日志和重试行为。

`CODEBUDDY2API_AUTO_ACCEPT_BUDDY` 仅启动读取，默认 `false`；预授权已启用国内账号完成首次领猫任务、协议及旅行，自动旅行仍受账号开关控制。`first_buddy` 无需单独接取，包含 `not_accepted` 在内的待完成状态可直接发起一次本账号的真实国内 WorkBuddy 对话；优先可用零倍率模型，否则取最低已知倍率，最多请求 32 个输出 token，可能消耗少量积分。不执行其他奖励任务、不付费开盒、不切猫、不领取国际试用积分。

手动 `POST /admin/credentials/{id}/travel` 返回 `buddy_confirmation`，包含官方条款与独立的 `authorization` 自动化范围。勾选后提交 `{"confirm_buddy":true,"agreement_revision":"<返回的版本>"}`；旧版仅领猫授权失效。`can_claim` 仅表示资格，不禁用同意。

`control.sqlite3` 保留同意及每账号一次实际新手对话，重启不重复。发送前明确取消时仅撤销本次未发送预留，未知或已发送的尝试不自动释放；旧接取记录不阻塞尚未发送的对话。仅官方任务完成才继续领猫。首次领取结果不确定时，超过 24 小时仍保留预留、仅回查；未进入领取阶段的失败可在退避后续办。升级保留该数据库，旅行状态与余额同步不触发任务。

旅行奖励领取与派遣共用按账号隔离的写入预留，不确定结果不超时重放；状态查询只回查并更新本地确认记录，不发上游写请求。存储失败停止领取和派遣，跨进程确认不覆盖其他请求的记录。

体验积分仅供符合资格的 `intl-work` 账号通过凭证抽屉或 `POST /admin/credentials/{id}/trial` 手动领取，启动和维护不领取。安全结果、成功历史与请求预留保存在 `control.sqlite3`，失败保留 24 小时退避。响应正文上限 64 KiB，且不返回浏览器；升级时保留数据库。

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
| `PATCH /admin/credentials/{id}` | 按账号设置单个布尔字段（`enabled`、`auto_checkin`、`auto_travel`、`auto_daily_chat` 或 `auto_growth`）或 `growth_auto` 任务码映射；保存自动任务开关不立即领取 |
| `GET /admin/credentials/{id}/growth-tasks` · `POST .../growth-tasks/auto` · `.../auto-all` · `GET /admin/growth-jobs` · `POST /admin/credentials/growth-batch` | 实时成长任务列表；以后台任务执行单任务或全部已开任务；各账号当前与最近任务；为已选账号批量设置任务开关 |
| `POST /admin/oauth/start` · `GET /admin/oauth/poll` | 发起与轮询登录；`site=cn`（默认）、`intl`（国际 WorkBuddy）或 `intl-codebuddy`（国际 CodeBuddy） |
| `POST` / `GET` / `DELETE /admin/session` · `DELETE /admin/sessions` | 密码登录、会话查看/退出及全部撤销；记住我会话用同一 SID 滑动续期 |
| `GET` / `PUT /admin/account` | 查看或修改管理用户名/密码；修改后更换会话 epoch 和 CSRF token |
| `GET` / `POST /admin/keys` · `PATCH` / `DELETE /admin/keys/{id}` · `POST .../rotate` · `POST .../reveal` | 创建和管理推理密钥；写操作使用乐观版本，普通密钥可查看，管理密钥只显示一次 |
| `GET /admin/credits` · `POST /admin/checkin` | 查询额度；按日幂等签到，国内按开关继续旅行 |
| `POST /admin/sync` | 同步全部启用账号的余额、目录和用量，不签到、不领取试用 |
| `POST /admin/credentials/{id}/{action}` | 单账号 `refresh`、`checkin`、`sync`、`travel-status`（仅查询）、`travel`（领取后派出）、`trial`（一次性体验积分）或 `reset-cooldown`（仅本地状态） |

旅行结果包含 `phase`、可选的安全诊断 `error_kind`/`http_status`/`code` 和查询时的 `remaining_seconds`。后续查询失败会设置 `ok=false`、`stale=true`，但保留已确认的 `claimed`/`departed`；再次操作前先查询核验。

`reset-cooldown` 不接受请求体，会清除该账号的全部冷却：既包括 401/403 认证熔断，也包括按模型的 429 冷却。这样在冷却被误判或上游已恢复时，无需重启网关即可放行。它只修改本地状态：不刷新 Token、不访问上游、不排队同步，人工停用的账号同样可用。结果区分 `changed_in_memory` 与 `durable`；写入失败时 `ok` 为 false，此时内存冷却已清除但重启会恢复盘上记录，直接重试该请求即可。

页面使用 `/dashboard/*`，管理 API 使用 `/admin/*`，客户端保留原 `/v1/*`；不注册 `/cn`、`/intl` API 前缀。模型自动选路不要求客户端改变地址。

**`OPEN_NOAUTH` 匿名模式会同时开放推理和账号池 billing（`/v1/dashboard/billing/*`）。** 仅在没有任何外部密钥和 `api_keys` 记录时生效，缺失或占位 token 都会放行，公网部署绝不可使用。只要存在任意密钥记录，包括停用或过期记录，匿名模式即失效。

管理端使用用户名和密码，任何 API key 都不能登录管理接口。HttpOnly Cookie 仅授权 `/admin/*`，推理密钥仅授权 `/v1/*`。普通登录有效期 12 小时；「记住我」有效期 400 天并滑动续期。修改账户或「退出所有会话」会撤销所有会话。

外部 `CODEBUDDY2API_KEY` 是初始密码和不受限的管理推理密钥。旧默认 key 会迁移为初始密码和数据库管理密钥；修改密码后，登录不再跟随外部密钥变化。忘记密码时，交互执行 `converter.py admin reset-password`。

普通密钥支持有效期、IP/CIDR 白名单、RPM 和上游模型限制，可再次查看并以明文保存。管理密钥不受限、最多一个，只保存哈希。普通密钥不能读取 billing 或其他密钥的视频任务。公开 `/v1/media/...` 资源在媒体 TTL 到期前始终可读，撤销创建密钥不会将其撤回。

### 管理 Origin / CSRF 开关

默认开启。OAuth 轮询在 `Origin`、`Sec-Fetch-Site` 均缺失时，兼容同源 `Referer`（协议、主机、端口一致），仍要求有效 CSRF token。已有 `Origin` 优先校验；无 `Origin` 但有 Fetch Metadata 时，只接受 `same-origin`，不会再用 Referer 回退。登录和写操作仍要求 Origin。

正常同源访问不需要关闭保护。反代改写转发的 Host 或协议时（例如域名 HTTPS 访问而容器内看到 HTTP），浏览器 Origin 与服务端看到的地址不一致，登录会报 Origin 校验失败。把对外地址加入 `admin_allowed_origins`（WebUI 系统设置，即时生效），或设置 `CODEBUDDY2API_ADMIN_ORIGINS` / `--admin-allowed-origins`：逗号分隔的来源或裸域名（如 `https://chat.example.com`、`chat.example.com`，裸域名按 HTTPS），最多 32 条。CLI 或环境变量显式设置后 WebUI 字段锁定。优先使用此白名单而非关闭保护；若仍报错，先统一访问地址、检查反代传递的 Host 和协议，并刷新页面重新登录。仅在受信任本地环境需要关闭时，在原启动命令追加 `--admin-csrf false`，或在已有 `.env` 中设置：

```dotenv
CODEBUDDY2API_ADMIN_CSRF=false
```

仅启动时生效，CLI 优先于环境变量，不能通过 WebUI 修改。在 `.env` 中写入取值或传入对应参数后重新创建容器，不能仅执行 `docker compose restart`。

关闭会跳过登录请求的 Origin 检查，以及 Cookie 管理写操作和 OAuth 轮询的 Origin/CSRF 检查；API key、会话有效期、OAuth 任务归属、官方授权地址白名单和危险操作确认仍保留，不影响 `/v1/*`。

**关闭会降低浏览器跨站请求保护，不应将此配置直接暴露到公网。** 恢复 `--admin-csrf true` 或环境变量 `true` 并重启即可重新启用。

### 服务端路径导入

WebUI 可以直接上传文件；以下限制针对 `POST /admin/credentials` 的路径导入：

- 将文件放入 `auth/imports/`，或 `CODEBUDDY_IMPORT_DIR` 指定的服务端目录。
- 仅接受直接子级普通 `.info` 文件，拒绝符号链接、子目录及超过 1 MiB 的文件。
- 请求体为 `{"path":"account.info"}`，也可填写该文件的绝对路径。同名文件按导入规则更新。
- 身份由产品 profile、UID 和租户共同确定；另一文件已持有同一身份时返回 409。同 UID 的不同产品或租户可以共存。删除接口使用文件名，启停接口使用身份 ID，二者不要混用。

## 模型与调度

以 WebUI 和 `GET /v1/models` 为客户端选择依据。原始目录按账号/租户、地域、产品与客户端版本缓存到 `auth/control.sqlite3`，默认有效期 6 小时；新凭据触发同步，刷新失败保留该账号的可信旧缓存。旧未隔离缓存不作为国际共享来源。

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

选路与目录维护都可切换模式，无需回滚已持久化数据：

- `routing_scheduler`：`shadow`（默认）同时执行旧的全量判断与索引分片，返回旧结果，并把有界、不含凭证信息的差异记录在 `GET /admin/performance`；差异计数持续为零后再切到 `indexed`；`indexed` 按模型、请求内别名和版本组合复用同一份候选分片；`legacy` 每次请求重新判断全部凭证。只有条目身份、策略、已发布目录或账号偿付能力真实变化才会使分片失效，令牌刷新和维护写入不打断稳态。
- `catalog_probe_mode`：`account`（默认）每周期刷新全部已同步账号；`representative` 每个产品每 TTL 周期只探测一个凭证——无目录账号优先，其次当前来源，再次目录最旧——失败按指数退避，并按每周期有界配额补齐缺失或标脏的账号目录，新账号无需手动扫描即可路由。未探测或失败的账号保留上次可信目录，`/v1/models` 仍是当前账号目录并集，只有代表目录新增模型才会标脏旧版本账号。
- `POST /admin/models/scan` 在后台把当前全部账号各刷新一次，`GET /admin/models/scan` 返回进度、成功数、失败数、目录差异数、整体错误原因和按产品分组的目录版本分布；它是一次性校准入口，不是周期任务。
- `housekeep_concurrency`（默认 `4`）限制后台维护并发：余额、目录和用量各自独立限额，设为 `1` 即回到原来的串行周期；账号同步失败按指数退避加抖动重试。5 分钟内已同步的账号会被下一个完整周期跳过，因此事件触发周期不会重复同步。

### 模型声明与图片兼容

`/v1/models` 保留原字段，增加 `capabilities`、`limits`、`metadata_by_profile`，管理接口及路由预览同步提供。能力状态为 `supported`、`unsupported`、`mixed`、`unknown`；限制含 `state`（`known`、`mixed`、`unknown`）和 `value`，仅已知且一致时返回数值。按产品保留不同账号的声明版本，但不公开账号身份。白名单覆盖说明、能力、窗口、思考选项、关联模型及参数建议；凭据、内部配置和带认证信息的 URL 不公开。上游声明不等于原生能力或实测保证，参数建议不自动覆盖请求。

`model_capability_guard` 默认 `true`，可在 WebUI、`--model-capability-guard false` 或 `CODEBUDDY2API_MODEL_CAPABILITY_GUARD=false` 关闭。仅在现有绑定和当前免费优先范围内筛选，明确不兼容返回 400、不发上游；未知能力兼容放行，每条请求固定入口开关。检查图片、工具及历史、已声明思考选项，以及 `max_tokens` 输出上限（含 Responses 映射的 `max_output_tokens`）；不估算输入 token，不对 `max_completion_tokens` 改名或套用该上限，不转换 Anthropic 思考预算。关闭不绕过鉴权、目录授权、容量或大小限制。

两种国际产品在选路后归并含图的连续 `user` 段，保留内容顺序和图片数据；国内请求、纯文本段及 system/assistant/tool 边界不变。消息级属性冲突或内容无法无损表达时返回 `400 / image_user_run_not_mergeable`，最终字节限制仍生效。图片兼容不随能力开关关闭，不增加重试，也不让文本模型获得原生视觉。

### 实测计费与输出上限

目录倍率只是上游声明；上游开始收费后，模型仍可能继续声明 `x0.00`。每次响应的 `usage` 都带有实际扣费 credit，审计日志按请求保存，因此网关以实测为准，而不是只信声明：

- 某产品声明免费的模型，若该产品近 7 天最近 20 次请求中有任意一次被扣费，即视为**实测计费**，并在各处不再按免费处理：零余额账号不再可用、免费优先排序不再优先、`auto` 不再视其为免费。连续 20 次未扣费后恢复为免费。
- 流量只能证明模型被计费，不能给出倍率：上游按会话缓存提示词，命中部分约按新输入的 1/20 计费，且常不在 `usage` 中体现，因此按单次扣费折算会把每 token 价格低估一个数量级。实测计费的模型在被探测校准前显示 **实测计费 · 倍率待估** 与平均每次扣费，同样不会被当作免费。
- 探测时对目标模型与本产品一个已声明付费倍率的参考模型（最接近 0.1x 者）各发一次约 8K token、各自新会话的相同请求；目标倍率 = 参考倍率 × 两者每 token 扣费之比，无需假设任何 token 单价。目标未扣费时不发参考请求。
- `GET /v1/models` 的 `credits` / `credits_by_profile` 为生效倍率；最低倍率来自估算时额外返回 `credits_estimated: true`。
- 可按模型 × 产品手填倍率（模型规则的 `credits_override`，范围 `0`–`1000`，`0` 表示确认免费），路由与 `/v1/models` 均以手填值优先于声明与实测。自建别名不单独设倍率，请设在其上游模型的规则上。
- `POST /admin/models/{id}/rate-probe` 在提供该模型的每个产品上，用有余额的账号发起一次约 8K token 的请求，并记录上游返回的扣费与校准倍率；`POST /admin/models/rate-probe` 对所有被某产品声明免费的模型执行同样探测。两者都启动同一个共享后台任务（并发 4）并立即返回，运行中再次发起会返回当前任务；`GET /admin/models/rate-probe` 返回进度、正在探测的模型/产品/步骤以及全部结果。真正免费的模型探测不消耗积分；被计费的模型消耗其自身请求（约 1x 的模型约 0.6 积分）加参考模型的数个计费单位。每个探测请求（含参考模型）也会写入请求日志，带 token 与扣费：协议为 `rate_probe`，会话 ID 以 `rate-probe-` 为前缀。校准样本在控制库中保留 30 天，不会从日志重复计入。

目录声明的 `maxOutputTokens` 常高于模型实际能输出的量，且更大的 `max_tokens` 不会报错，而是被静默钳制。`scripts/probe_max_tokens.py` 经网关实测真实上限：要求模型数到足以写满当前档位，并区分上游截断（`finish=length` 且输出低于请求值）与模型主动收尾。档位不超过声明值，按 4 个模型并行，每个模型限时 600 秒，结果追加写入 JSONL，`--resume` 可断点续测。

```bash
python3 scripts/probe_max_tokens.py --key "$CODEBUDDY2API_KEY" --panel-out auth/output_probes.json
python3 scripts/probe_max_tokens.py --key "$CODEBUDDY2API_KEY" --models glm-5.2 --resume --panel-out auth/output_probes.json
python3 scripts/probe_max_tokens.py --key "$CODEBUDDY2API_KEY" --dry-run
```

`--panel-out` 将结果合并写入 `control.sqlite3` 同目录的 `output_probes.json`，未重测的模型保留原记录。`GET /admin/models/output-probes` 只读返回该文件，重跑后下次请求即生效，无需重启。探测会真实消耗积分。

## 思考兼容

Messages 的 `enabled` / `adaptive` 启用 Chat 推理，`output_config.effort` 和 Responses 的 `reasoning.effort` 映射为 `reasoning_effort`。显式顶层 `reasoning_effort` 优先，但 Messages 的 `disabled` 始终使用 `none`；模型能力检查仍生效。未提供控制参数时保留上游默认行为。

未指定强度时，Messages 从实际选中账号的 `reasoning.defaultEffort` 或旧 `reasoning.effort` 选择声明支持的默认值；否则优先 `high`，再选可用选项，声明未知时回落为 `high`。换号后重新解析新账号的默认值。

手工 `enabled` 要求整数 `budget_tokens >= 1024`，但该预算不等于上游精确 token 限额，`max_tokens` 原样转发；兼容映射不模拟原生自适应调度。仅支持 `display: summarized`。

可读历史统一放入 `reasoning_content`，不混入普通正文。Responses 优先使用可读 `content`，其次使用 `summary`；摘要不能还原原生隐藏推理。签名不转发，`redacted_thinking`、仅含加密签名的思考及非空 Responses `encrypted_content` 在选路前返回 400；上游自行决定使用哪些可读历史。

## 请求边界

- 三个生成协议统一将 `developer` 归一为 `system`，已有 system 移到首位；归一化不修改调用方 payload。可选的 [Responses 投影](#responses-投影)和脱敏会另行处理内容，因此默认链路并非逐字透传。
- `prompt_mode` 热生效，可在 WebUI、CLI 或环境变量配置。默认 `empty` 不附加网关 system 提示词，也不清空客户端已有指令。`prepend` 在选路后仅插入一次前缀：WorkBuddy 后端使用 `You are WorkBuddy, a powerful AI assistant.` 及官方任务目标，CLI 后端使用 `You are CodeBuddy Code.` 及官方 CLI 任务说明。前缀提炼自官方模板，重试不重复插入；DSH 适配不受此开关控制。
- 独立 DSH 适配始终将官方固定身份、共享 coding persona（保留模型 ID）、minimal persona、`You are DeepSeek Harness.` 及当前／旧版 DSH Web GUI 角色语境转换为 WorkBuddy 表述，覆盖 system、developer、assistant 文本及文本块；同时改写已确认的 `Web GUI whose URL the runtime context names. When the user refers` 指纹，包括 assistant 引用。适配 standard／PTC／Cordis／minimal／headless／web／SDK／ACP，保留 plan 指令、源码 checkout 说明和普通品牌引用，关闭脱敏也生效；不对 user/tool 内容做全局替换。
- 图片计入全部历史和工具结果，重复图片逐次计数，按消息与内容块数组顺序判断新旧。默认保留最新 16 张，只移除超额图片并保留文本和消息结构；图片清空的内容用文本占位。
- `--image-policy error` 在本地返回 `413 / too_many_images`。处理后仍超过字节上限则返回 `413 / request_too_large`，不为满足预算继续截断文本。
- 图片数量合规不保证单图大小或模型视觉能力满足上游要求。URL/base64 图片可转换，Responses 图片 `file_id` 不支持。
- 省略 `stream` 时三个端点都按协议默认返回完整 JSON（非流式）；`stream` 必须是布尔值。默认 `stream_mode=compatible` 时，Responses 流式以及带工具的 Chat / Messages 流式先聚合校验再输出 SSE；`realtime` 则让三个协议都增量发送，省略/非流式请求仍返回完整的已校验 JSON。
- 推理错误按客户端协议成形：OpenAI 路由为顶层 `error` 对象，Messages 路由为 `{"type": "error", ...}`；保留状态码，开流后的错误只用 SSE 报告，不重放。
- 上游有效 `Retry-After`（0–86400 秒或对应 HTTP 日期）规范化为秒并在开流前返回；429 仅冷却对应账号/模型。无效或过期值回落正文重置时间或默认 600 秒；本地全凭据冷却的 429 返回剩余等待秒数。
- Chat 与 Responses 保留客户端显式 `prompt_cache_key`，不自动生成；缓存命中和节费取决于上游。
- 不支持的能力显式拒绝而非静默降级：Chat 的 `n≠1`、Responses 的 `previous_response_id`/`conversation`（本网关不保存服务端响应状态）返回 400；长度截断或审核过滤的 Responses 标记为 `incomplete`，不伪装为 `completed`。
- SQLite 审计只保留有界、脱敏诊断，不是完整原始请求。日志、凭证导出和备份仍须按私有数据保管。

## 部署暴露与凭据导入

- Compose 默认映射回环地址。对外开放前配置外部密钥或创建数据库密钥，不要把匿名模式当作公网访问方案。
- **没有任何外部或数据库密钥时，`CODEBUDDY2API_ALLOW_OPEN_NOAUTH=true` 会匿名开放推理和账号池 billing，缺失或占位 token 均可。** 存在任意密钥记录即强制鉴权；没有密钥且开关关闭时，所有推理请求返回 401 `api_key_required`。
- 每个数据目录只运行一个网关进程。其他进程的密钥变更约 1 秒内可见，但 RPM 计数与 `last_used_at` 聚合按进程独立。备份含普通密钥明文，必须按凭证保护。
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
| WebUI 无法登录 | 使用初始或当前管理密码，或执行 `converter.py admin reset-password`。HTTPS 反代后经域名登录失败时，用 `admin_allowed_origins` 信任对外来源（见上文） |
| 本地 401 / 403 / 429 | 检查推理密钥是否存在、启用且未过期，再检查 IP/模型允许范围和 RPM；鉴权与权限错误按客户端协议返回 |
| 上游 401 / 403 | 凭证级认证熔断；在 WebUI 检查并重新登录 |
| 渠道校验（11128） | 可用时仅在同账号 `/console` 回退一次；第二次渠道拒绝可把 system 指令降为 user 后在同账号重试一次。最终仍拒绝则直接返回，不冷却任何账号；搜索/MCP 没有回退端点，账号保持可用 |
| 账号限制（11140） | 标记账号为官方限制并休眠 2 小时，保留证据且暂时移出选路；申诉通过后可在凭证页一键重新启用。当前请求不换账号重放 |
| 模型限制（11148） | 仅冷却当前账号×模型 30 分钟，不冷却整个账号，当前请求不重放 |
| 模型级 11129/11134/11150/11158 | 业务码优先于 HTTP 状态（包括 500/503）；不冷却账号或模型，不换账号重放，返回上游响应 |
| 429 | 对该凭证的上游模型冷却，后续请求自动换绑；全部候选都在冷却时仍返回 429。设了 `--failover-max` 时，当前请求就地换凭证重放 |
| 上游 `service info not found`（11102） | 明确的 400/404 模型拒绝：国内按（后端、模型）退避，国际按（账号、产品、模型）隔离，不冷却整个账号；无可用候选时返回 404。6 小时后半开，反复命中最长 24 小时，成功即解除；`/console` 确认的模型缺失仍可故障转移。旧国际入口级记录不再拦截账号；用 `GET /admin/model-blocks` 查看 |
| 建连失败 | `ConnectError` / `ConnectTimeout` 换新连接重放一次：两者都发生在写下第一个正文字节之前，上游手里什么都没有，重放不会重复计费 |
| 发送后断连、读超时、协议错误 | 不做网络重放，避免重复计费；日志记录异常类型与耗时 |
| 非流式响应还没成形，客户端就挂断 | 立刻取消这次上游调用并归还并发名额，审计记为 `cancelled`，不会被记成一次已完成的回答；流式本来就是这一行为 |
| 流式在第一个字节之前失败 | 按真实状态码返回，与 `stream=false` 同口径。只带一个流内 `error` 事件的 200 会被客户端读成「模型答了个空」，会话静默结束，审计里还记成一次成功 |
| 换凭证重放（`--failover-max`） | 默认关闭。开启后，失败发生在「一个字节都没发给下游」之前时换一个凭证重放，最多 N 次，审计记为 `success` 并留下 `failover_recovered` 尝试标记。可重放的失败：上游 HTTP 401/403/429/502/503/504 拒绝，与确定没开始收正文的传输失败（建连失败/超时）；内容审核拒绝、11128/11140/11148、11129/11134/11150/11158、上游已回 200 后合成的 502、读超时与协议错误一律不重放；换不出其他凭证时如实回第一次的状态码。计费口径：401/403/429/503 与建连类失败发生在受理阶段，不会扣费；502/504 可能已被上游处理并计费，但结果到不了下游，不重放也退不回额度——只是把一次已付费请求变成断掉的会话。这类重放在日志里标注「上游可能已处理该请求」，便于对账 |
| 写超时重放（`--retry-write-timeout`） | 默认关闭。写超时只能证明正文没发完，不能证明上游忽略了已收到的部分，因此默认既不参与连接重试也不参与换凭证重放；跨境长会话比握手更容易遇到写超时，确认上游不按半截正文计费后再开启。这类重放同样带「上游可能已处理该请求」日志标记 |
| 工具参数损坏 | 兼容聚合校验按 `--tool-call-max-retry`（默认 3）额外生成，可能消耗更多额度；被丢弃的生成带用量记入尝试明细；耗尽后返回错误。实时模式绝不重生成，只在成功终端前报告协议错误 |
| 上游空流或残流 | 没有有效输出、缺少结束标记或包含错误的流不伪装为成功 |
| 内容审核拒绝 | 脱敏 + `--no-compact` 下，仅完整非流式纯拒绝且模板确实缩短时，最多同账号兜底一次；流式不做审核重试，也不因此熔断或切号 |
| 响应慢 | 在 WebUI 查看耗时与失败尝试，再选择当前账号支持的更快模型 |
| 同账号多处登录相互失效 | 桌面端与网关独立刷新可能互相顶掉；优先独立扫码登录或停止另一端使用 |

## 降级与回滚

功能开关不藏隐状态：关闭守卫或模式即对新请求停止生效，回退源码即恢复旧行为。例外是持久化设置与自动化状态：`control.sqlite3` 保存 WebUI 设置、模型规则与奖励预留，旧代码会拒绝未知字段。含 `api_keys` 的控制库 schema v3、审计库 schema v5 和媒体库 schema v3 均不能被 1.4.x 读取；降级必须停机并从升级前备份恢复。更早的纯设置变更可移除新增启动参数，并使用上面的窄范围离线 `settings.<name>` 删除流程，递增 revision 并执行完整性检查。不要恢复升级前数据库，也不要混用 WAL/SHM 代数，否则可能回滚较新的领取、会话、撤销和账号状态。回滚无法撤销已完成的上游签到、领取或旅行派出。
