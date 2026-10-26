# 开发预览

[首页](../README.zh-CN.md) · [English](development.md)

预览服务用真实网关对接一个模拟的 CodeBuddy / WorkBuddy 上游。它不需要账号、不含任何真实凭证，但 WebUI 的每个页面和每个 API 都能正常响应，适合开发界面、调试客户端和做演示。

## 启动

```bash
uv run --locked --no-build python -B -m preview                   # http://127.0.0.1:8790，密钥 preview
uv run --locked --no-build python -B -m preview --port 8790 --data-dir /tmp/cb-preview
```

| 参数 | 默认值 | 作用 |
|------|--------|------|
| `--host` / `--port` | `127.0.0.1` / `8790` | 监听地址 |
| `--api-key` | `preview` | WebUI 与 API 使用的密钥 |
| `--data-dir` | 每次新建临时目录 | 跨重启保留预览数据；目录必须为空或由预览服务创建 |
| `--public` | 关闭 | 拒绝不适合共享演示的操作，见[公开部署](#公开部署) |
| `--reset-hours` | `0`（不重置） | 运行指定小时后退出，由进程管理器用全新数据重启 |
| 其他参数 | — | 原样传给网关，例如 `--admin-allowed-origins` |

打开 `http://127.0.0.1:8790/dashboard`，用密钥登录。修改前端需要热更新时，保持预览服务运行，再让开发服务器代理到它：

```bash
cd web
CODEBUDDY_WEBUI_PROXY=http://127.0.0.1:8790 vp dev   # http://127.0.0.1:5174
```

## 模拟范围

网关之下全部是模拟的，网关本身全部是真实代码：请求、故障转移、冷却、计费、审计记录和后台自动化都与正式运行一致。

| 方面 | 表现 |
|------|------|
| 账号 | 8 个合成账号：国内与国际、CodeBuddy 与 WorkBuddy、不限额与按额度计的企业租户，以及一个刷新令牌已失效的掉线账号 |
| 历史 | 一个月的请求记录，概览、日志和统计打开即有数据 |
| 余额 | 不同到期时间的积分包；请求会消耗，签到、旅行、任务和体验积分会增加 |
| 模型 | 按区域区分的模型目录，包含倍率、能力和上限 |
| 推理 | Chat Completions、Responses 和 Anthropic Messages 都会流式返回引用你消息的模拟回复，支持推理的模型带推理内容 |
| 自动化 | 签到、Buddy 旅行（一趟 3 分钟）、成长任务、体验积分和国际站活跃打卡 |
| 登录 | 添加账号会在 4 秒后自动登录一个新的合成账号；界面上显示的官方登录链接无需打开 |
| 搜索、抓取与 MCP | `/v1/search`、`/v1/fetch` 及 MCP 的 `web_search`／`web_fetch` 工具返回 `example.com` 上的模拟结果 |
| 更新 | 发布列表只有当前版本，镜像测速下载的是模拟发布包，安装已停用 |

上游状态保存在内存里，每次启动都会重置；网关自己的数据保存在数据目录中。

### 消息指令

在最后一条用户消息里任意位置写入指令，可以控制模拟模型的行为：

| 指令 | 效果 |
|------|------|
| `[preview:tool]` / `[preview:tool=Name]` | 用占位参数调用第一个或指定名称的工具。没有这条指令时绝不调用工具，因为客户端可能会执行收到的任何工具调用 |
| `[preview:429]` | 返回带额度恢复时间的 429，触发冷却和故障转移 |
| `[preview:500]` | 返回上游服务错误 |
| `[preview:slow]` | 慢速流式输出，便于观察实时流 |
| `[preview:long]` | 追加一段很长的编号列表 |

## 公开部署

`--public` 让预览服务可以公开分享。它会拒绝上传、导入、导出和删除凭证，也会拒绝修改设置、清空日志、归档和更新操作，这样访客既不会留下真实凭证被下一位访客导出，也不会把共享的演示弄坏。`--reset-hours` 会在指定时间后退出，由进程管理器用全新数据重新启动。预览服务不认识的参数会原样传给网关，例如反向代理需要的受信任管理来源。

发布的 `edge` 镜像已包含网关和 WebUI，只需挂载本目录：

```yaml
services:
  preview:
    image: ghcr.io/maiphucgiang/codebuddy2api:edge
    restart: unless-stopped
    command: ["python3", "-B", "-m", "preview", "--public", "--reset-hours", "6", "--host", "0.0.0.0",
              "--port", "8790", "--admin-allowed-origins", "https://preview.example.com"]
    working_dir: /app
    volumes:
      - ./preview:/app/preview:ro
    ports:
      - "127.0.0.1:8790:8790"
    user: "65534:65534"
    read_only: true
    tmpfs:
      - /tmp:size=256m
```

在 `127.0.0.1:8790` 前面放一个启用 TLS 的反向代理，并把它的公网来源写进 `--admin-allowed-origins`，否则 WebUI 登录会被 Origin 校验拒绝。密钥本就是公开的，随链接一起提供即可。

## 隔离

- 预览服务从不读取仓库的 `.env` 或 `auth/`：它会清除环境中所有 `CODEBUDDY*` 变量，只在自己的数据目录中运行，并拒绝使用 `auth/`。
- 所有 httpx 请求都由模拟上游应答；其他对外连接和域名解析一律拒绝，绕过 httpx 的活跃打卡通道也被替换。未模拟的接口返回 501，并在日志中记录一次 `[preview] 未模拟的上游接口`。
- 一键更新已停用，工作区文件不会被替换。
