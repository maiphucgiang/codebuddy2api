# codebuddy2api

把 **WorkBuddy / CodeBuddy（腾讯代码助手）** 订阅变成本机可直接使用的 **OpenAI / Anthropic 兼容 API**。

[English](README.md)

## 功能

- 提供 OpenAI Chat Completions / Responses 与 Anthropic Messages，支持原生 tools / tool_calls 与流式 SSE；沿用原 `/v1` 接口，后端自动选择国内／国际、CLI／WorkBuddy 账号
- **无感登录**：浏览器扫码即可添加账号，**无需安装桌面端**
- 多账号凭证池：会话黏绑、零倍率（`x0.00`）模型优先、快过期积分优先调度、401/429 自动熔断换绑
- token 自动刷新与每日保活
- 可选积分余额折算，经 OpenAI billing 端点（`/v1/dashboard/billing/*`）输出

## 快速上手

### 1. 安装

```bash
git clone https://github.com/maiphucgiang/codebuddy2api.git
cd codebuddy2api

uv venv
uv pip install -r requirements.txt
```

或用普通虚拟环境：`python3 -m venv .venv && source .venv/bin/activate && pip install -r requirements.txt`

### 2. 添加账号（无需桌面端）

```bash
uv run converter.py login
```

浏览器会自动打开登录页。扫码后，即使网页已显示“登录成功”，也请等待终端提示“账号已保存”再关闭命令。账号默认保存在 `auth/`，无需手动复制凭据或先启动服务。

- 国际站（workbuddy.ai）：`uv run converter.py login --site intl`。
- 服务器或无浏览器环境：`uv run converter.py login --no-browser`，在其他设备打开终端里的链接扫码。
- 添加多个账号时，重复运行登录命令；同一账号重新登录会更新已有凭据。
- 登录链接 10 分钟内有效，按 `Ctrl+C` 可取消。
- 使用普通虚拟环境时，将 `uv run` 换成 `python3`。

### 3. 启动

首次启动先复制并编辑配置；已有 `.env` 不要覆盖。Compose 专用的镜像、端口映射等变量不会改变本地 Python 的监听参数。

```bash
cp .env.example .env
# 编辑 .env 中的密钥、图片策略等配置
uv run --env-file .env converter.py --desensitize
```

看到监听 `http://127.0.0.1:8787` 即启动成功。

直接运行 `python3` 不会自动读取 `.env`，需显式设置环境变量或命令行参数。启用 API key 后，API 请求须携带对应的 Authorization 头。

服务运行期间也可以在另一个终端添加账号，默认在下次请求时自动加载。登录命令与服务须使用同一个 `CODEBUDDY_AUTH_DIR`（默认 `auth/`）；以 `--auth-file` 启动的服务只使用指定文件。

本机桌面端已登录的话，首次启动会自动导入其凭据。也可以把其他账号的 `*.info` 文件放进 `auth/`。

### 4. 自检

```bash
curl http://127.0.0.1:8787/health
curl http://127.0.0.1:8787/v1/models
# 自动合并可用账号的模型；启用密钥时添加 Authorization 头
```

## 管理界面

源码运行前构建界面：`cd web && vp install && vp build`，然后回仓库根目录启动服务。Docker 构建会自动打包界面。

访问 `http://127.0.0.1:8787/dashboard`，使用当前 API key 登录；未设置 key 时管理界面锁定。页面统一位于 `/dashboard/*`，管理接口仍为 `/admin/*`，客户端仍使用 `/v1/*`。

可管理模型启停、对外 ID、区域/凭证绑定、凭证启停及 OAuth/文件导入导出，并查看熔断、模型冷却、请求审计与历史统计。配置优先级为 CLI > 环境变量 > WebUI > 默认值，外部锁定项不会被页面覆盖。

凭证仍保存在 `auth/*.info`。管理元数据保存于 `auth/control.sqlite3`，日志默认保存于独立的 `auth/logs.sqlite3`；默认明细预算 256 MiB、保留 30 天，聚合统计不随明细清理。全部清空日志与统计需要危险操作确认，不删除凭证和网关配置。旧文本日志保留，不自动回填精确统计。

详见 [WebUI 与数据管理](docs/webui.md)。

## 客户端接入

OpenAI / Responses 的 Base URL 保持为 `http://127.0.0.1:8787/v1`。后端自动选择支持目标模型的可用账号，并按最终账号的地域和产品路由；国际特有模型也使用同一地址，无需地域前缀或新增客户端参数。

### Codex CLI（推荐）

Codex CLI 走 `/v1/responses`。把下面配置合并到 `~/.codex/config.toml`：

```toml
[model_providers.workbuddy]
name = "WorkBuddy (via local converter)"
base_url = "http://127.0.0.1:8787/v1"
wire_api = "responses"
env_key = "CODEBUDDY2API_KEY"

[profiles.workbuddy]
model = "glm-5.2"                   # 也可用 kimi-k2.7 / deepseek-v4-pro / auto
model_provider = "workbuddy"
```

```bash
export CODEBUDDY2API_KEY=any-value   # 转换器未启用 --api-key 时随便填
codex --profile workbuddy "你的任务描述"
```

运行时上下文与真实用户指令分开处理；超限请求返回 HTTP 413，不静默截断最新用户请求。

### Claude Code / CC Switch

Claude Code / Anthropic SDK 的 Base URL **不要带 `/v1/messages`**，SDK 会自动追加该路径。

```bash
export ANTHROPIC_BASE_URL=http://127.0.0.1:8787
export ANTHROPIC_AUTH_TOKEN=any-value  # 启用 API key 时填写配置的密钥
export ANTHROPIC_MODEL=deepseek-v4-pro
claude
```

CC Switch 的 Anthropic 提供商 Base URL 填写 `http://127.0.0.1:8787`，国内、国际账号通用。只有明确要求完整端点的客户端才填写 `/v1/messages`；Anthropic SDK 会自行追加此路径。

模型名使用 `/v1/models` 发布的 ID，可在管理界面设置对外别名；不自动猜测 Anthropic→腾讯模型映射。开启 `--desensitize` 可适配 WorkBuddy 对 Claude Code 固定身份、Git 分支提示的兼容要求；`/v1/messages` 同样转为上游 Chat Completions。

### 其他 OpenAI 兼容客户端

Cherry Studio / ZCode / LobeChat / NextChat / Open WebUI 或自写 SDK 客户端：

- Base URL：`http://127.0.0.1:8787/v1`
- API Key：留空，或填启动时设置的 `--api-key`
- 模型名：`glm-5.2` / `deepseek-v4-pro` / `kimi-k2.7` / `auto` 等

## 接口一览

| 接口 | 说明 |
|------|------|
| `POST /v1/chat/completions` | OpenAI Chat Completions |
| `POST /v1/responses` | OpenAI Responses（适配 Codex CLI） |
| `POST /v1/messages` | Anthropic Messages |
| `POST /v1/messages/count_tokens` | Anthropic token 数量估算 |
| `GET /v1/models` | 自动合并可用账号的模型；标准字段之外附 `credits` 倍率与 `credits_by_profile` 分组 |

以上原地址适用于所有支持的地域和产品；不注册 `/cn`、`/intl` API 前缀，带前缀请求返回 404。三个生成协议统一保证发往上游的首条消息为 system：已有 system 则移到首位，缺失时补默认值；保留已有 system 和其它内容。

| 共用接口 | 说明 |
|------|------|
| `GET /health` | 公开存活检查，仅返回 `{"status":"ok"}` |
| `GET /v1/dashboard/billing/subscription` | 总积分折算余额（`hard_limit_usd`） |
| `GET /v1/dashboard/billing/usage` | 用量（美分）与按日明细 |
| `GET/POST/DELETE /admin/credentials` | 查看 / 导入 / 移除凭证 |
| `POST /admin/oauth/start` · `GET /admin/oauth/poll` | 无感登录（见上文） |
| `GET /admin/credits` · `POST /admin/checkin` | 积分余额 / 手动签到 |

管理接口必须配置 API key，空 key 时锁定；WebUI 使用同 key 建立管理会话。详细凭证池状态请查 `/admin/credentials`；`/health` 不返回账号、路径或异常信息。

### 凭据文件导入

将 `.info` 文件放入 `auth/imports/`，或服务端 `CODEBUDDY_IMPORT_DIR` 指定的目录。仅接受该目录的直接子级普通文件，不接受符号链接、子目录或超过 1 MiB 的文件。

向 `POST /admin/credentials` 发送 `{"path":"account.info"}`，也可使用该文件的绝对路径。同名文件覆盖更新，同 UID 不同文件名返回 409。

## 命令行参数

| 参数 | 默认值 | 说明 |
|------|------|------|
| `--host` | `127.0.0.1` | 监听地址 |
| `--port` | `8787` | 监听端口 |
| `--api-key` | 无 | 要求本地客户端携带该 key |
| `--log` | 无 | 额外输出兼容文本日志（50 MiB 轮转，保留 2 份）；SQLite 审计默认开启 |
| `--desensitize` | 关 | 适配固定 CLI 模板、压缩运行时提示、零宽脱敏关键词 |
| `--no-compact` | 关 | 配合 `--desensitize` 保留主要行为指令；仍适配固定模板、裁剪运行时元数据 |
| `--auth-file` | 扫描 `auth/` | 显式指定凭据文件，可重复传入 |
| `--credit-price-cny` | `0.014` | 积分折算单价（元/Credit） |
| `--credit-price-usd` | `0.03` | 国际站积分折算单价（美元/Credit） |
| `--usd-rate` | `7.15` | 人民币→美元汇率（billing 端点用） |
| `--model-catalog-ttl` | `21600` | 云端模型表缓存有效期（秒） |
| `--no-model-guard` | 关 | 关闭表外模型本地拦截 |
| `--auto-trial [true/false]` | `false` | 启用国际 WorkBuddy 账号的一次性体验积分领取 |
| `--max-images` | `16` | 单请求图片总数上限；`0` 不允许图片 |
| `--image-policy` | `truncate` | `truncate` 保留最新图片；`error` 超限返回 413 |
| `--max-request-bytes` | `33554432` | 图片处理与适配后发往上游的 JSON 上限（32 MiB，须为正整数） |
| `--log-body-limit` | `65536` | 正文日志预览上限（64 KiB）；`0` 只记摘要 |

指定函数的 `tool_choice` 会转换为仅提供该函数并设为 `required`，无效名称在本地拒绝。需要 JSON 响应时显式设置 `stream: false`，SSE 则设置 `stream: true`。

环境变量：`CODEBUDDY_AUTH_DIR`（凭据目录）、`CODEBUDDY_IMPORT_DIR`（API 允许导入目录）、`CODEBUDDY2API_KEY`、`CODEBUDDY2API_LOG`。

限额也可通过 `CODEBUDDY2API_MAX_IMAGES`、`CODEBUDDY2API_IMAGE_POLICY`、`CODEBUDDY2API_MAX_REQUEST_BYTES`、`CODEBUDDY2API_LOG_BODY_LIMIT` 配置；命令行参数优先，修改后重启生效。

在 `.env` 设置 `CODEBUDDY2API_AUTO_TRIAL=true` 可启用 `intl-work` 账号的一次性体验积分领取，默认关闭。成功或已领取后按账号持久化到 `auth/trial-ledger.json`，失败至少退避 24 小时，不立即重放 POST；资格及额度以上游为准，升级时保留该状态文件。

### 图片与请求限制

- Chat、Responses、Anthropic 请求均计入全部历史及工具结果中的图片，重复图片逐次计数；按消息与内容块数组顺序判断新旧，不依赖非标准时间戳。
- 默认保留最新 16 张，只移除超额图片，保留文本和工具消息；图片清空的内容用文本占位。设置 `--image-policy error` 后超限返回 `413 / too_many_images`，不访问上游。
- 图片处理后仍超过请求字节上限时返回 `413 / request_too_large`，不再截断文本；图片数量合规不保证单图大小或模型视觉能力符合上游限制。URL/base64 图片可转换，Responses 的 `file_id` 不支持。
- 日志只记录有界预览，图片 base64 与常见认证字段会脱敏；日志不等于完整原始请求，仍应按私有数据保管。

## Docker

### Docker Compose

在仓库目录首次配置：复制模板为 `.env` 并修改；已有 `.env` 不要覆盖，补齐需要的字段即可。

```bash
cp .env.example .env
# 编辑 .env：密钥、监听地址、端口、凭据目录、图片策略等
docker compose build
docker compose up -d
docker compose exec codebuddy2api python3 converter.py login --no-browser
```

模板默认构建当前源码，只监听 `127.0.0.1:8787`，图片上限 16 张、保留最新图片。需要外部访问时修改 `CODEBUDDY2API_BIND`，并先设置随机的 `CODEBUDDY2API_KEY`、限制网络访问。

Compose 自动读取 `.env` 中已声明的变量，Shell 环境优先；使用普通变量插值保留旧版兼容性。没有 `.env` 时仍使用 Compose 文件中的兼容默认值；新部署建议始终复制模板。

在浏览器中打开登录链接扫码，等待终端确认已保存；国际站追加 `--site intl`。凭据目录由 `CODEBUDDY2API_AUTH_PATH` 指定，默认 `./auth`，挂载至 `/data/auth`；添加账号自动热加载。修改 `.env` 后执行 `docker compose up -d` 重建配置有变化的容器，无需重新登录。

### 使用发布镜像

将 `.env` 中 `CODEBUDDY2API_IMAGE` 改为 `ghcr.io/maiphucgiang/codebuddy2api:<版本>`，然后执行：

```bash
docker compose pull
docker compose up -d --no-build
```

发布镜像支持 `linux/amd64` 和 `linux/arm64`；版本标签固定版本，`latest` 为稳定版，`edge` 跟随 `main`。镜像功能以所选版本为准，本地修改只有重新构建后生效。

### Docker CLI

同样先准备 `.env`；以下命令使用本地源码及默认端口、目录：

```bash
docker build -t codebuddy2api:local .
docker run -d --name codebuddy2api -p 127.0.0.1:8787:8787 \
  --env-file .env -v "$PWD/auth:/data/auth" \
  -e CODEBUDDY_AUTH_DIR=/data/auth codebuddy2api:local
docker exec -it codebuddy2api python3 converter.py login --no-browser
```

## 模型列表

以 `/v1/models` 为准，自动合并国内、国际账号的可用来源。目录按账号/租户、地域、产品与客户端版本缓存于 `auth/model-catalog.json`，有效期 6 小时；新凭据触发同步。同步失败只保留相同账号的可信旧缓存，旧版未隔离的根模型表不用于授权路由。

每个模型在标准 OpenAI 字段（`id` / `object` / `created` / `owned_by`）之外附带倍率：`credits` 为该模型各来源的最小倍率（`0.0` 表示零计费，`null` 表示目录未声明可解析倍率），`credits_by_profile` 为按产品来源的分组倍率，例如 `{"intl-work": 0.0, "cn-cli": 0.03}`。客户端可忽略这两个扩展字段，不影响兼容性。

凭据的 domain / token issuer 决定产品 profile；聊天与 token 刷新使用固定入口，CLI / WorkBuddy 身份头各自生成：

| Profile | 聊天 / 刷新入口 |
|------|------|
| `cn-cli` | `https://copilot.tencent.com` |
| `cn-work` | `https://www.workbuddy.cn` |
| `intl-cli` | `https://www.codebuddy.ai` |
| `intl-work` | `https://www.workbuddy.ai` |

具体同名模型可在任何地域、产品之间调度，但仅选择自身已知目录支持该模型的账号。目录和余额按账号隔离，不借用其它账号的能力或额度；国际凭据必须有已知的正额度。调度优先选择自身目录把该模型声明为零倍率（`credits: x0.00`，如国际 WorkBuddy 的 `deepseek-v4.1-flash`）的账号，其次才按额度优先级、冷却和会话黏绑；余额已归零的账号退出付费模型轮询（不再占用队列、也不会因零额度反复失败），但仍可服务其目录声明为零倍率的模型，余额恢复后自动回到轮询；失效黏绑在发送前重绑，最终账号严格决定固定 host 和身份头。已发送的上游 POST 不会向其它账号重放。目录或凭据未就绪返回可重试的 503，明确不支持的模型返回 404。

`auto` 保留为按可用账号默认模型调度的别名，并非所有账号都默认获得所有模型。国际账号必须在自身目录声明 `default-model`，上游映射为 `default-model`；国内 WorkBuddy 必须声明 `auto`；国内 CLI 仅在已知非空可用目录下保留旧 `auto`。别名同样遵守余额、黏绑和冷却约束，包括映射后上游模型的冷却。

## 常见问题

- **本地 401**：启用了 `--api-key` 但客户端没带同一个 key。
- **上游 401**：账号 token 失效，用无感登录重新添加账号。
- **429**：额度/频率限制——网关会对该「凭证 × 模型」冷却并换绑其他凭证，详情请携带已配置的 API key 查询 `/admin/credentials`。
- **网络错误**：只对建连失败自动退避重试一次；发送后断连、读写超时及 HTTP 错误不整单重放，避免重复计费。日志包含异常类型与耗时。
- **工具参数损坏**：聚合校验失败最多重新生成 3 次，耗尽后返回错误而非损坏的调用；重新生成可能额外消耗额度。
- **上游空流**：只有 `stop` / `[DONE]`、没有内容的流按错误处理，不作为成功的空回答。
- **被内容审核拦截**：开启 `--desensitize`。配合 `--no-compact` 时，完整的非流式纯审核拒绝可在模板确实缩短后重试一次；流式请求不做审核重试，不因审核切换账号。
- **响应慢**：换更快的模型，如 `deepseek-v4-flash`。
- **同一账号多处使用**：从桌面端复制的凭据与桌面端各自刷新 token，滚动刷新场景可能互相顶掉；优先用无感登录账号，或让桌面端停用该账号。

## 免责声明

本项目仅供个人学习使用，不得用于商业用途。与腾讯、WorkBuddy、CodeBuddy、OpenAI、Anthropic 无官方关联。本项目仅调用你已登录账号的官方接口，请仅在你合法拥有订阅的前提下使用；账号与凭据的一切使用责任及风险由使用者自行承担。

## 开源协议

[MIT](./LICENSE)

## 社区

感谢 [LINUX DO](https://linux.do) 社区提供开放、友善的技术交流平台。
