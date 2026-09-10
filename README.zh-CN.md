# codebuddy2api

把 **WorkBuddy / CodeBuddy（腾讯代码助手）** 订阅变成本机可直接使用的 **OpenAI / Anthropic 兼容 API**。

[English](README.md)

## 功能

- 用你已登录的账号对外提供 `POST /v1/chat/completions`、`POST /v1/responses`、`POST /v1/messages`、`GET /v1/models`，支持原生 tools / tool_calls 与流式 SSE
- **无感登录**：浏览器扫码即可添加账号，**无需安装桌面端**
- 多账号凭证池：会话黏绑、快过期积分优先调度、401/429 自动熔断换绑
- token 自动刷新 + 每日保活，凭证不会因过期失效
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

### 2. 启动

```bash
uv run converter.py --desensitize --log converter.log
```

看到监听 `http://127.0.0.1:8787` 即启动成功。

### 3. 添加账号（无需桌面端）

```bash
# 1）申请登录链接
curl -X POST http://127.0.0.1:8787/admin/oauth/start
# → {"login_id": "oa_...", "verification_uri": "https://www.codebuddy.cn/login?...", "expires_in": 600}

# 2）浏览器打开 verification_uri，扫码授权

# 3）轮询直到完成——凭证自动入库并热加载入池
curl "http://127.0.0.1:8787/admin/oauth/poll?login_id=oa_..."
# → {"done": true, "uid": "...", "nickname": "...", "imported": ".../auth/<uid>.info"}
```

- 国际站（workbuddy.ai）用 `POST /admin/oauth/start?site=intl`。
- 本机桌面端已登录的话，首次启动会自动导入其凭据。
- 也可以直接把其他机器/账号的 `*.info` 文件放进 `auth/` 目录，即热加载入池。
- 所有入库渠道都会校验账号 uid 与签发站点，异站或损坏文件会被拒收。

### 4. 自检

```bash
curl http://127.0.0.1:8787/health
curl http://127.0.0.1:8787/v1/models
```

## 客户端接入

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

### Claude Code / CC Switch

Claude Code 走 `/v1/messages`。在 CC Switch 里配置：

```json
{
  "DeepSeek-V4-Pro": {
    "base_url": "http://127.0.0.1:8787/v1/messages",
    "api_key": "",
    "model": "deepseek-v4-pro"
  }
}
```

模型名必须填腾讯后端真实模型名（不做 Anthropic→腾讯映射）。Claude Code 场景建议保持 `--desensitize` 开启。

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
| `POST /v1/messages` | Anthropic Messages（适配 Claude Code / CC Switch） |
| `GET /v1/models` | 可用模型（云端模型表同步 + 本地缓存） |
| `GET /health` | 公开存活检查，仅返回 `{"status":"ok"}` |
| `GET /v1/dashboard/billing/subscription` | 总积分折算余额（`hard_limit_usd`） |
| `GET /v1/dashboard/billing/usage` | 用量（美分）与按日明细 |
| `GET/POST/DELETE /admin/credentials` | 查看 / 导入 / 移除凭证 |
| `POST /admin/oauth/start` · `GET /admin/oauth/poll` | 无感登录（见上文） |
| `GET /admin/credits` · `POST /admin/checkin` | 积分余额 / 手动签到 |

启用 `--api-key` 后，admin 接口均需携带该 key。详细凭证池状态请查 `/admin/credentials`；`/health` 不返回账号、路径或异常信息。

### 凭据文件导入

`POST /admin/credentials` 保留 `{"path":"account.info"}` 格式。源文件必须是服务端 `CODEBUDDY_IMPORT_DIR`（默认自管凭据目录下的 `imports/`）的直接子级；请先创建目录并放入源文件，再用文件名或该文件的绝对路径导入。不再接受任意服务器路径、子目录、符号链接或非 `.info` 文件，文件上限 1 MiB。

文件只读取一次，校验凭据结构及允许站点（不验证 token 真伪），再以私有权限原子保存。同名更新保持兼容，同 UID 异名返回 409。输入/读取失败返回 400，保存失败返回 500，不返回内部异常详情。已有自动化需将源文件移入该目录，或在重启前显式配置导入目录。

会话键与 Conversation ID 改用 SHA-256，会话键为 128 位；升级重启后 ID 会变化，黏绑表原本就只保存在内存。

## 命令行参数

| 参数 | 默认值 | 说明 |
|------|------|------|
| `--host` | `127.0.0.1` | 监听地址 |
| `--port` | `8787` | 监听端口 |
| `--api-key` | 无 | 要求本地客户端携带该 key |
| `--log` | 无 | 记录请求与响应日志（单文件 50MB 轮转，保留 2 份） |
| `--desensitize` | 关 | 压缩运行时提示、零宽脱敏高风险词（agent 客户端建议开启） |
| `--no-compact` | 关 | 配合 `--desensitize`，保留更完整的 system prompt |
| `--auth-file` | 扫描 `auth/` | 显式指定凭据文件，可重复传入 |
| `--credit-price-cny` | `0.014` | 积分折算单价（元/Credit） |
| `--credit-price-usd` | `0.03` | 国际站积分折算单价（美元/Credit） |
| `--usd-rate` | `7.15` | 人民币→美元汇率（billing 端点用） |
| `--model-catalog-ttl` | `21600` | 云端模型表缓存有效期（秒） |
| `--no-model-guard` | 关 | 关闭表外模型本地拦截 |

环境变量：`CODEBUDDY_AUTH_DIR`（凭据目录）、`CODEBUDDY_IMPORT_DIR`（API 允许导入目录）、`CODEBUDDY2API_KEY`、`CODEBUDDY2API_LOG`。

## Docker

当前版本为 **1.0.0**，API 从 `VERSION` 读取，CI 同步校验。首次标签发布后，预构建镜像地址为 `ghcr.io/maiphucgiang/codebuddy2api:1.0.0`，包含 `linux/amd64` 和 `linux/arm64`，拉取时自动选择架构。可用它替换下方本地构建示例中的镜像名。

```bash
docker build -t codebuddy2api .
docker run -d --name codebuddy2api -p 8787:8787 \
  -v /path/to/auth:/data/auth \
  -e CODEBUDDY_AUTH_DIR=/data/auth \
  codebuddy2api
```

挂载任意含 `*.info` 的目录即可；没有凭据时，启动后用无感登录添加。也可用 `docker compose up -d --build`（先改 `docker-compose.yml` 里的挂载路径）。

### 镜像发布工作流

`.github/workflows/docker.yml` 先执行全部回归测试，再通过 Buildx/QEMU 构建两个平台。Actions 固定到提交 SHA；仅镜像任务申请 `packages: write`，使用仓库自动提供的 `GITHUB_TOKEN`，无需额外仓库密码。镜像附带构建来源记录及 SBOM，`.dockerignore` 仅允许运行所需文件进入构建上下文。

| 触发方式 | 镜像标签 |
|------|------|
| 面向 `main` 的 PR | 只构建，不登录、不推送 |
| 推送 `main` | `edge`、`sha-<commit>` |
| 推送 `v1.0.0` 标签 | `1.0.0`、`1.0`、`1`、`latest`、`sha-<commit>` |

手动运行仅在选择 `main` 或版本标签时推送。发布标签必须与 `VERSION` 一致，开发镜像不会覆盖 `latest`。以后发布时，先把改动提交到 `main`，再创建并推送 `v1.0.0`；推送标签不会自动创建 GitHub Release。首次发布后，请检查 GHCR 包的公开可见性及该仓库的 Actions 访问权限，需要匿名拉取时将包设为 Public。

## 模型列表

运行时 `/v1/models` 以云端模型表为准（每 6 小时同步，缓存于 `auth/model-catalog.json`）；国际模型只在池内存在有额度的国际凭证时暴露。云端不可达时使用内置兜底表：

`hy4-preview`、`hy4-preview-x`、`hy3`、`hy3-x`、`deepseek-v4-pro`、`deepseek-v4-flash`、`deepseek-v4.1-flash`、`deepseek-v3-2-volc`、`glm-5.3`、`glm-5.3-flash`、`glm-5.2`、`glm-5.1`、`glm-5.0`、`glm-5.0-turbo`、`glm-5v-turbo`、`glm-4.7`、`glm-4.6`、`glm-4.6v`、`minimax-m3`、`minimax-m2.7`、`minimax-m2.5`、`kimi-k3-1`、`kimi-k2.7`、`kimi-k2.6`、`kimi-k2.5`、`kimi-k2-thinking`、`hunyuan-chat`、`default`、`auto`

`auto` 为网关侧调度别名；模型可用性取决于你的账号权限。

## 常见问题

- **本地 401**：启用了 `--api-key` 但客户端没带同一个 key。
- **上游 401**：账号 token 失效，用无感登录重新添加账号。
- **429**：额度/频率限制——网关会对该「凭证 × 模型」冷却并换绑其他凭证，详情请携带已配置的 API key 查询 `/admin/credentials`。
- **被内容审核拦截**：多为 agent runtime 文本触发，开 `--desensitize`，仍不稳再试 `--desensitize --no-compact`。
- **响应慢**：换更快的模型，如 `deepseek-v4-flash`。
- **同一账号多处使用**：从桌面端复制的凭据与桌面端各自刷新 token，滚动刷新场景可能互相顶掉；优先用无感登录账号，或让桌面端停用该账号。

## 免责声明

本项目仅供个人学习使用，不得用于商业用途。与腾讯、WorkBuddy、CodeBuddy、OpenAI、Anthropic 无官方关联。本项目仅调用你已登录账号的官方接口，请仅在你合法拥有订阅的前提下使用；账号与凭据的一切使用责任及风险由使用者自行承担。

## 开源协议

[MIT](./LICENSE)
