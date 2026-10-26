# codebuddy2api

把 **WorkBuddy / CodeBuddy（腾讯代码助手）** 订阅变成本机可用的 **OpenAI / Anthropic 兼容 API**。

[English](README.md)

- 支持 Chat Completions、Responses 和 Anthropic Messages，包含工具调用与流式输出。
- 三种推理协议统一兼容工具 Schema 与上游渠道响应。
- 内置 **WebUI**：扫码添加账号，管理模型、凭证、日志与设置，无需桌面端。
- 多账号自动选路，兼容国内／国际站，自动刷新凭证。
- 按账号配置自动任务：国内个人账号默认签到后派 Buddy 旅行，可分别关闭；国际自动签到默认关闭。成长中心仅面向个人账号：已完成任务自动领取积分与能量，行为链按任务开关执行；企业账号不参与。

**在线预览：**[codebuddy2api.elaina-love.com](https://codebuddy2api.elaina-love.com/dashboard)，密钥 `preview`。它让 WebUI 与各个 API 对接模拟上游和合成账号运行，不含任何真实凭证，每 6 小时重置一次；其中已停用凭证导入导出、修改设置和清空日志。在本地运行见[开发预览](docs/development.zh-CN.md)。

## 快速开始

需要 Git 和 Docker Compose；直接拉取 GHCR 已构建镜像（内含 WebUI），无需在本机编译。

```bash
git clone https://github.com/maiphucgiang/codebuddy2api.git
cd codebuddy2api
cp .env.example .env
```

编辑 `.env`：将 `CODEBUDDY2API_KEY` 设置为自己的随机密钥。Compose 会把整个文件加载进容器，每个键都会生效，无关的键也无影响。升级时保留已有 `.env`。

```bash
docker compose pull
docker compose up -d --no-build
```

Compose 默认拉取 `latest`（`CODEBUDDY2API_IMAGE=ghcr.io/maiphucgiang/codebuddy2api:latest`）；镜像同时发布发行版本（例如 `1.3.6`）与跟随 `main` 的 `edge`。需固定部署时改用版本标签；镜像功能以对应版本为准，不包含尚未合并的源码分支改动。

首次使用：

1. 打开 **http://127.0.0.1:8787/dashboard**，使用刚设置的 API key 登录。
2. 在「凭证管理」扫码添加国内或国际账号，也可导入 `.info` 文件，或按端点粘贴刷新令牌批量导入。
3. 在「模型路由」查看可用模型，将其对外 ID 填入客户端。

按模板配置时仅允许本机访问。远程访问前请配置 HTTPS 并限制网络访问；保留并妥善备份 `auth/` 数据目录。如需直接运行源码或在终端登录，见[部署指南](docs/deployment.zh-CN.md)。

## 客户端接入

| 协议 | Base URL |
|------|----------|
| OpenAI Chat / Responses | `http://127.0.0.1:8787/v1` |
| Anthropic Messages | `http://127.0.0.1:8787` |
| MCP（web_search + web_fetch 工具） | `http://127.0.0.1:8787/v1/mcp` |
| REST 联网搜索 / 抓取 | `POST http://127.0.0.1:8787/v1/search` / `POST http://127.0.0.1:8787/v1/fetch` |

- **API Key**：与 WebUI 登录使用同一个密钥。
- **模型**：使用 WebUI 中的对外 ID，或查询 `GET /v1/models`。
- 国内、国际账号共用这些地址，无需额外的地域参数。Anthropic SDK 会自行追加 `/v1/messages`，不要把它写入 Base URL。
- 搜索与抓取端点不调用模型。在请求里声明 Anthropic `web_search_*`／`web_fetch_*` 或 OpenAI `web_search` 服务端工具时，网关会自行执行，见[联网搜索与抓取](docs/clients.zh-CN.md#联网搜索与抓取)。
- 工具参数：三种推理协议均会将空参数根转换为对象 Schema；带字段级布尔 `required` 标记的旧式参数映射会包入 `properties`，必填字段名汇总到根部。`pattern` 等 Schema 关键字若对应的是字段，仍按字段名处理。标准 Schema、含 `$`／`x-` 扩展的根以及嵌套 Schema 保持原样。

[Codex CLI、Claude Code / CC Switch 等配置示例 →](docs/clients.zh-CN.md)

## 项目结构

```
.
├── converter.py         # 网关入口：协议适配、选路、调度
├── app/                 # 管理 API、凭证、目录、审计与策略模块
├── web/                 # WebUI 前端（React/TypeScript，构建产物由网关托管）
├── tests/               # 离线回归测试
├── preview/             # 开发预览：真实网关 + 模拟上游 + 合成账号
├── docs/                # 用户文档（中英双语）
├── examples/            # 客户端配置示例
├── scripts/             # 版本与依赖锁定工具
├── Dockerfile           # 多阶段镜像：前端构建 + Python 运行时
├── docker-compose.yml   # 推荐部署方式
├── .env.example         # 全部运行时环境变量及注释
└── .github/workflows/   # CI：测试、镜像发布、CodeQL
```

## 文档

| 指南 | 内容 |
|------|------|
| [WebUI 使用指南](docs/webui.zh-CN.md) | 添加账号、模型路由、日志审计、设置与备份 |
| [部署指南](docs/deployment.zh-CN.md) | 发布镜像、升级、反向代理/HTTPS、本地运行与命令行登录 |
| [客户端配置](docs/clients.zh-CN.md) | Codex CLI、Claude Code、CC Switch 与通用客户端 |
| [进阶参考](docs/advanced.zh-CN.md) | 参数、API、模型调度、请求限制与故障排查 |
| [开发预览](docs/development.zh-CN.md) | 不用真实账号，对接模拟上游运行 WebUI 与各个 API |

## 常见问题

- **绑定域名后经 HTTPS 反代无法登录 WebUI？** 将对外来源加入 `admin_allowed_origins` 信任列表——见[管理 Origin 校验](docs/advanced.zh-CN.md#管理-origin--csrf-开关)。
- **没有 Docker？** 准备依赖和 WebUI 后，直接 `uv run --locked --no-build converter.py` 或 `python3 converter.py`，无需 `.env`；首次本地启动保存默认 key 并仅在终端显示一次——见[本地 Python 运行](docs/deployment.zh-CN.md#本地-python-运行)。
- **数据在哪里？** 全部位于 `auth/`（Docker 中为 `/data/auth`）：凭证、设置与日志数据库——见[数据与备份](docs/webui.zh-CN.md#数据与备份)。
- **镜像标签怎么选？** `latest` 跟随稳定版，`edge` 跟随 main，版本标签固定某一发行版——见[使用已发布镜像](docs/deployment.zh-CN.md#使用已发布镜像)。
- **Responses 工具输出/参数被压缩或需要完全原样？** 设置 `responses_projection_mode`（默认 `balanced`，设 `passthrough` 可完全关闭投影）及 `responses_projection_max_bytes`（默认 `40000`，`0` 禁用单项裁剪）。balanced 裁剪保留头尾并显示原始 bytes、估算 tokens 与总行数，客户端地址不变——见 [Responses 投影](docs/clients.zh-CN.md#responses-投影)及[详细说明](docs/advanced.zh-CN.md#responses-投影)。

## 免责声明

本项目仅供个人学习使用，不得用于商业用途。与腾讯、WorkBuddy、CodeBuddy、OpenAI、Anthropic 无官方关联。本项目仅调用你已登录账号的官方接口，请仅在你合法拥有订阅的前提下使用；账号与凭据的一切使用责任及风险由使用者自行承担。

## 开源协议

[MIT](LICENSE)

## 社区

感谢 [LINUX DO](https://linux.do) 社区提供开放、友善的技术交流平台。
