# 部署指南

[返回首页](../README.zh-CN.md) · [English](deployment.md)

日常账号、模型和日志管理优先使用 [WebUI](webui.zh-CN.md)。以下命令从仓库根目录执行。

## 配置与数据

首次运行先执行 `cp .env.example .env`，编辑 `CODEBUDDY2API_KEY` 为自己的随机密钥；已有 `.env` 请保留，只补充所需配置。

| 设置 | 用途 |
|------|------|
| `CODEBUDDY2API_IMAGE` | Compose 镜像；模板为 `codebuddy2api:local` |
| `CODEBUDDY2API_BIND` / `CODEBUDDY2API_PORT` | Compose 的宿主机监听地址 / 端口；模板为 `127.0.0.1:8787` |
| `CODEBUDDY2API_AUTH_PATH` | Compose 宿主机数据目录，默认 `./auth`，挂载至容器 `/data/auth` |
| `CODEBUDDY_AUTH_DIR` | 本地 Python 的数据目录，默认仓库下 `auth/`；Compose 容器内固定为 `/data/auth` |

Compose 自动读取 `.env` 中已声明的变量，Shell 环境优先。不要省略模板配置：Compose 在缺少 `.env` 时为兼容旧部署可能监听全部网卡。对外访问前设置随机 key、HTTPS 和访问限制。

整个数据目录必须可写，且应位于本地文件系统；不要只挂载一个 SQLite 文件，也不要让多个实例共用目录。升级前停止服务并备份整个目录，详见 [数据与备份](webui.zh-CN.md)。

## Docker Compose

### 构建当前源码

```bash
docker compose build
docker compose up -d
```

构建包含 WebUI，无需在宿主机安装 Node.js 或 Python。启动后访问 `http://127.0.0.1:8787/dashboard` 添加账号。

### 使用发布镜像

在 `.env` 中将 `CODEBUDDY2API_IMAGE` 改为 `ghcr.io/maiphucgiang/codebuddy2api:<版本>`，选择已有的发布版本，然后执行：

```bash
docker compose pull
docker compose up -d --no-build
```

镜像支持 `linux/amd64` 和 `linux/arm64`。版本标签固定版本，`latest` 跟随稳定版，`edge` 跟随 main；功能以所选版本为准，不要假设旧镜像包含当前源码的 WebUI。

修改 `.env` 后重新执行对应的 `docker compose up -d` 命令，使配置有变化的容器重建。升级本地源码时重新构建，升级发布镜像时先修改版本并拉取；保留数据目录即可保留登录状态。

## 本地 Python 运行

需要 Python、uv，以及构建界面的 Node.js 和 vp CLI：

```bash
uv sync --locked --no-build --python 3.12
(cd web && vp install --frozen-lockfile && vp build)
uv run --locked --no-build --env-file .env converter.py --desensitize
```

先按上文配置 `.env`，再启动服务并进入 `/dashboard` 添加账号。更改前端源码后需重新构建 WebUI。

不使用 uv 时，可执行 `python3 -m venv .venv`，激活环境后用 `pip install --require-hashes --only-binary=:all: -r requirements.txt` 安装依赖，将运行命令换为 `python3 converter.py --desensitize`。**普通 Python 不自动读取 `.env`**，须显式导出环境变量或传入 CLI 参数。

本地 Python 的监听地址和端口由 `--host`、`--port` 控制；Compose 专用的 `CODEBUDDY2API_BIND`、`CODEBUDDY2API_PORT`、`CODEBUDDY2API_AUTH_PATH` 不改变本地监听和数据目录。

## 依赖锁定

`pyproject.toml` 是直接依赖入口，`uv.lock` 锁定完整依赖；`requirements.in` 和带哈希的 `requirements.txt` 是供 pip、Docker、CI 使用的生成文件：

```bash
uv lock
python3 scripts/export_requirements.py
```

有意改动依赖时使用 `uv add`/`uv remove`，然后导出并审阅锁文件差异。日常启动使用 `--locked`，不升级依赖；项目元数据版本保持与 `VERSION` 一致，发版时同步两处。pip/Docker 安装仍要求二进制 wheel 与哈希匹配，不关闭检查。

Docker 构建前端、Node 和 Python 镜像按多架构 digest 固定。更新时保留 `linux/amd64`、`linux/arm64` 并验证构建。锁定防止漂移，不代替后续安全更新。


## 命令行登录

无法使用 WebUI 时也可扫码登录，无需先启动服务：

```bash
uv run --locked --no-build --env-file .env converter.py login
uv run --locked --no-build --env-file .env converter.py login --site intl --no-browser
uv run --locked --no-build --env-file .env converter.py login --site intl-codebuddy --no-browser
```

第一条默认国内站；`--site intl` 登录国际 WorkBuddy（`www.workbuddy.ai`），`--site intl-codebuddy` 登录国际 CodeBuddy（`www.codebuddy.ai`）。`--no-browser` 只显示链接，可在其他设备打开扫码。网页显示登录成功后，仍需等待终端确认「账号已保存」。链接 10 分钟内有效，`Ctrl+C` 可取消。

Docker 中使用 `docker compose exec codebuddy2api python3 converter.py login --no-browser`，国际账号按产品追加 `--site intl` 或 `--site intl-codebuddy`；镜像需包含对应入口。

登录与服务必须使用同一 `CODEBUDDY_AUTH_DIR`。默认目录扫描模式下，新账号会自动加载；重复登录同一身份更新其凭证。以 `--auth-file` 启动时只使用指定文件。

未设置 `CODEBUDDY_AUTH_DIR` 的本地默认模式下，首次启动会从已登录桌面端补充导入凭证；也可向自管目录添加 `.info` 文件。桌面端和网关各自刷新 token 可能互相顶掉，优先使用独立扫码登录。

## 不使用 Compose

准备好 `.env` 后，可使用本地源码镜像：

```bash
docker build -t codebuddy2api:local .
docker run -d --name codebuddy2api -p 127.0.0.1:8787:8787 \
  --env-file .env -v "$PWD/auth:/data/auth" \
  -e CODEBUDDY_AUTH_DIR=/data/auth codebuddy2api:local
```

此命令显式使用默认端口和目录，不读取 Compose 专用的端口映射设置。启动后通过 WebUI 添加账号，或运行 `docker exec -it codebuddy2api python3 converter.py login --no-browser`。

## 验证与排查

`curl http://127.0.0.1:8787/health` 应返回 `{"status":"ok"}`，仅表示服务存活，不代表账号或模型可用。账号状态请查看 WebUI；客户端接入见 [客户端配置](clients.zh-CN.md)，错误与重试边界见 [进阶参考](advanced.zh-CN.md)。
