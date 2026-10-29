# 部署指南

[首页](../README.zh-CN.md) · [English](deployment.md)

日常账号、模型与日志管理优先使用 [WebUI](webui.zh-CN.md)。以下命令均在仓库根目录执行。

## 配置与数据

首次部署运行 `cp .env.example .env`，并将 `CODEBUDDY2API_KEY` 设置为自己的随机密钥。它是外部管理推理密钥，也是修改密码前的初始 WebUI 密码。已有 `.env` 请保留，只按需追加配置。

| 配置 | 作用 |
|------|------|
| `CODEBUDDY2API_IMAGE` | Compose 镜像；默认使用已发布的 `latest` 标签 |
| `CODEBUDDY2API_BIND` / `CODEBUDDY2API_PORT` | 本地监听与 Compose 宿主机映射；默认 `127.0.0.1:8787` |
| `CODEBUDDY2API_AUTH_PATH` | Compose 宿主机数据目录；默认 `./auth`，挂载到容器 `/data/auth` |
| `TZ` | 容器时区；每日任务按它跨天，未设置时保持 UTC |
| `CODEBUDDY2API_ENV_FILE` | 可选的 Compose 覆盖项，指定 env 文件路径；默认 `.env` |
| `CODEBUDDY2API_ALLOW_OPEN_NOAUTH` | 镜像与 Compose 的兼容选项，在其中固定为 `true`。仅当没有任何外部或数据库密钥时，无论是否携带占位 token，都会匿名开放推理**及账号池 billing**；存在任意密钥记录即失效 |
| `CODEBUDDY2API_TRUSTED_PROXIES` | 可信代理 IP/CIDR（逗号分隔）；直连地址命中时审计改记 `X-Forwarded-For`/`X-Real-IP` 里的真实客户端地址，而不是代理地址 |
| `CODEBUDDY_AUTH_DIR` | 本地 Python 数据目录；默认仓库内 `auth/`。Compose 在容器内固定为 `/data/auth` |
| `CODEBUDDY_IMPORT_DIR` | 可选导入目录；默认数据目录下的 `imports/`。Compose 中请使用容器路径 |

`.env.example` 列出了全部生效的运行时变量及其默认值。Compose 会整体加载该文件，注释掉的键仍可在 WebUI 配置（工具元数据、Responses 投影、来源白名单、故障转移等）。取零表示关闭聚合/并发限制、Responses 单项裁剪或额外重试，而入站上限必须为正值。

Compose 会把 `.env` 加载进容器；镜像、宿主机映射与数据路径仍以 shell 变量优先。默认宿主机映射仅回环。开放远程访问前请设置随机密钥、HTTPS 与访问限制。容器内监听保持 `0.0.0.0:8787`；调整对外暴露用 `BIND/PORT`，不要改容器监听参数。

数据目录需整体挂载在可写本地存储，不要只挂载单个 SQLite 文件，也不要在多个实例间共享。升级前停止网关并备份整个目录，见[数据与备份](webui.zh-CN.md#数据与备份)。

## Docker Compose

### 使用已发布镜像

Compose 会把 `.env` 中的每个键加载进容器，可选配置无需在 Compose 里重复变量名；缺少 `.env` 时仍以网关默认值和 SQLite 中保存的设置为准。将 `CODEBUDDY2API_IMAGE` 设为 `ghcr.io/maiphucgiang/codebuddy2api:<版本>` 可固定使用已发布版本，然后运行：

```bash
docker compose pull
docker compose up -d --no-build
```

镜像支持 `linux/amd64` 与 `linux/arm64`。版本标签固定发行版，`latest` 跟随稳定版，`edge` 跟随 main。功能以所选版本为准；旧镜像可能不包含当前源码的 WebUI。

### 构建当前源码

```bash
docker compose build
docker compose up -d
```

构建包含 WebUI；宿主机无需安装 Node.js 和 Python。启动后打开 `http://127.0.0.1:8787/dashboard` 添加账号。没有外部密钥时，从 `docker compose logs codebuddy2api` 查看初始 `admin` 密码，随后在「系统设置 → 管理账户」修改，并在「密钥管理」创建推理密钥。

### 升级与重建

WebUI「版本更新」页也可在运行中的容器里一键更新或回滚，见[版本更新](webui.zh-CN.md#版本更新)。这些改动写在容器可写层，重新创建容器后回到镜像版本。若要跟随新镜像，可在宿主机 crontab 定期执行 `docker compose pull -q && docker compose up -d --no-build`。

修改 `.env` 后重新执行对应的 `docker compose up -d` 以重建配置发生变化的容器。更新本地源码后需重新构建；使用发布镜像时选择并拉取新版本。仅执行 `docker compose restart` 不会应用新的环境变量。保留数据目录即可保留登录状态。

## 反向代理与 HTTPS

默认的回环映射是唯一无需额外工作的安全暴露方式。将网关放到域名反代之后时：

- 在反代终止 TLS，并转发原始 Host 与协议（nginx 配置 `proxy_set_header Host $host` 与 `X-Forwarded-Proto $scheme`）。
- WebUI 会比较浏览器 `Origin` 与网关实际看到的地址。反代改写转发的 Host/协议时（例如域名 HTTPS 访问而容器内看到 HTTP），登录会报 Origin 校验失败。此时把对外地址加入「系统设置 → 管理页额外信任来源」，或设置 `CODEBUDDY2API_ADMIN_ORIGINS` / `--admin-allowed-origins`，而不是关闭 CSRF 保护；见[管理 Origin 校验](advanced.zh-CN.md#管理-origin--csrf-开关)。
- 非回环暴露前配置外部密钥或先创建数据库密钥。匿名模式还会开放账号池 billing，公网部署不得使用。

## 本地 Python 运行

需要 Python 3.12+ 与 uv。仅在发行包未自带界面时才需构建：Node.js 24 与 `web/package.json` 中 `vite-plus` 锁定的 vp CLI。

```bash
uv sync --locked --no-build --python 3.12
(cd web && vp install --frozen-lockfile && vp build)
uv run --locked --no-build converter.py --host 127.0.0.1 --port 8787
```

`uv.lock` 过期时 `--locked` 会直接失败而不是重新解析，因此源码启动不会升级依赖。当前工作目录的 `.env` 会被自动读取，`uv run` 无需 `--env-file`。发行包已包含 WebUI，可跳过界面构建。

本地启动无需 `.env`。通过 CLI、环境变量或 `.env` 设置 `CODEBUDDY2API_KEY` 时，使用账号 `admin` 和该值登录；它仍是外部管理推理密钥，直到移除。没有外部密钥时，启动会把 `admin` 和随机初始密码写到控制终端；没有 TTY 时写到 stdout，Docker 中可用 `docker compose logs codebuddy2api` 查看。不会自动创建推理密钥，请登录后在「密钥管理」创建。

旧版保存的默认 key 会在首次启动时迁移为初始管理密码和数据库管理密钥；数据库密钥不会提升为 `CODEBUDDY2API_KEY`。修改管理密码后，后续登录不再跟随外部密钥变化，并撤销已有会话。密码只保存 scrypt 哈希。

密码先输出再提交账户；输出失败会停止启动且不写账户，提交失败则下次启动重试初始化。终端或容器日志的保留不受网关控制，首次登录后请尽快修改初始密码。

没有 uv 时，用虚拟环境安装同一份带哈希的依赖：

```bash
python3 -m venv .venv
. .venv/bin/activate          # Windows：.venv\Scripts\activate
python3 -m pip install --require-hashes --only-binary=:all: -r requirements.txt
python3 converter.py --host 127.0.0.1 --port 8787
```

两种启动方式都会读取当前工作目录的 `.env`（若存在），不搜索父目录。优先级：显式 CLI > 进程环境变量 > `.env` > SQLite 保存值 > 默认值。数据库密钥不会提升为外部密钥；空白外部值按未设置处理。修改监听需重启，`CODEBUDDY2API_IMAGE/AUTH_PATH` 只作用于 Compose。

### 管理密码恢复

使用与网关相同的数据目录执行交互式恢复命令：

```bash
uv run --locked --no-build converter.py admin reset-password
uv run --locked --no-build converter.py admin reset-password --username admin
```

命令在 TTY 中要求输入两次密码，不接受密码参数。服务运行时也可执行；下一次管理请求即使用新的账户 epoch，并撤销所有已有会话。Docker 中执行 `docker compose exec codebuddy2api python3 converter.py admin reset-password`。

### 配置迁移

`.env` 中的值仍然优先，因此任何写在里面的运行参数都会占住 WebUI 的对应项。要统一到配置库，先停止服务，再执行离线命令；命令不会改动 `.env` 本身：

```bash
python3 converter.py config migrate            # 当前目录 .env -> ./auth/control.sqlite3
python3 converter.py config migrate --env-file /srv/app/.env --data-dir /srv/app/auth
python3 converter.py config status --db /srv/app/auth/control.sqlite3   # 只读
```

两条命令都先解析 env 文件，再采用其中声明的数据目录（`CODEBUDDY_AUTH_DIR`），`--data-dir`/`--db` 只覆盖解析结果。写入前 `migrate` 会验证目标确实是本项目的控制库（SQLite 魔数、`user_version=3`、`control`/`runtime_state`/`gateway_secrets`/`api_keys`、可解析的 payload、`quick_check`），并在检测到运行中实例的租约或写锁时拒绝；`--force` 只跳过租约提示。部署类键（密钥、监听地址、数据路径）不会迁移。容器内没有 `.env` 文件，改用进程环境变量：

v2 控制库必须先由新版服务启动一次并完成 v3 升级，才能执行 `config migrate`。

```bash
docker compose stop
docker compose run --rm --no-deps -e CODEBUDDY_AUTH_DIR=/data/auth \
  codebuddy2api python3 converter.py config migrate --no-env --data-dir /data/auth
```

成功时值写入与“关闭 Dashboard 提醒”在同一次 revision 内提交；按提示删除 `.env` 中对应行后，这些项改由配置库提供、可在 WebUI 修改。旧版本启动的实例不写租约，升级后首次迁移前请自行确认服务已停止。`config status` 不写库。`--lang zh|en|auto` 决定 CLI 输出语言，并在 `serve`、`login`、`config migrate` 时把界面语言一并保存；见[配置与 CLI](advanced.zh-CN.md#配置与-cli)。

> 审计库为单写者设计：升级新版本前先停止旧实例，不要让两个版本的网关共用同一 auth 目录。

### 升级与数据迁移

发布包安装与 Git 工作区可在 WebUI「版本更新」页一键更新或回滚：校验发布包、备份配置库后原地替换程序文件，并按原启动参数重启；见[版本更新](webui.zh-CN.md#版本更新)。手动升级时，先停止网关并备份整个数据目录。首次启动将旧 JSON 会话、冷却、积分/领取、目录和用量状态一次性导入 `control.sqlite3`；原文件保留作备份，不再参与读写。关键数据损坏或迁移失败会阻止启动，请修复或恢复备份，不要删除账本绕过检查。

SQLite 状态读取或校验失败会阻止启动，不删除会话、不回退为空状态；修复控制库后再启动。仅可重建缓存的写入失败允许保留当前内存数据并告警。

所有运行日志使用 `logs.sqlite3`；旧 `--log` / `CODEBUDDY2API_LOG` 仅提示弃用，不再输出文本文件。初始密码只进入终端或启动输出，审计只保存密钥 ID 而非明文。限制数据目录访问权限；Windows 应使用当前用户的私有目录。

启动会把控制库 schema v2 升级为 v3，并迁移旧默认 key。旧版本无法读取 v3 控制库；降级须在停机后从升级前备份恢复，不能只切代码后复用旧 JSON。升级后发生的领取、派遣或会话撤销不能用过期备份覆盖。

## 依赖锁定

`pyproject.toml` 声明直接依赖；`uv.lock` 锁定全部解析版本。带哈希的 `requirements.txt` 是供 pip、Docker 与 CI 使用的生成文件：

```bash
uv lock
python3 scripts/export_requirements.py
```

有意调整依赖时使用 `uv add`/`uv remove`，然后导出并核对锁定文件。日常启动使用 `--locked`，不会升级任何包。元数据与当前 `VERSION` 保持一致；发版必须同时更新两处版本字段。pip/Docker 仍要求哈希匹配与二进制 wheel，不要关闭这些校验。

Docker 构建所用的前端、Node 与 Python 镜像均按多平台摘要锁定。刷新它们时保留 `linux/amd64` 与 `linux/arm64` 支持并验证构建。锁定只能防止漂移，不能免疫未来漏洞；安全更新仍需评审后刷新。

## 命令行登录

WebUI 不可用时不启动服务也能完成浏览器登录：

```bash
uv run --locked --no-build converter.py login
uv run --locked --no-build converter.py login --site intl --no-browser
uv run --locked --no-build converter.py login --site intl-codebuddy --no-browser
```

第一条使用国内站。`--site intl` 选择国际 WorkBuddy（`www.workbuddy.ai`）；`--site intl-codebuddy` 选择国际 CodeBuddy（`www.codebuddy.ai`）。`--no-browser` 打印授权链接，可在其他设备打开。即使浏览器显示成功，也要等终端确认凭证已保存。链接 10 分钟过期；按 `Ctrl+C` 取消。

Docker 中使用 `docker compose exec codebuddy2api python3 converter.py login --no-browser`；按所选国际产品追加 `--site intl` 或 `--site intl-codebuddy`。镜像版本需包含对应登录选项。

登录与服务必须使用同一个 `CODEBUDDY_AUTH_DIR`。默认目录扫描会自动加载新账号；重复登录会更新同一身份。以 `--auth-file` 启动的服务只使用指定文件。

在默认本地模式且未设置 `CODEBUDDY_AUTH_DIR` 时，启动会从已登录的桌面客户端导入缺失凭证。也可以直接向管理目录放入 `.info` 文件。桌面端与网关各自的独立刷新可能互相挤掉 token；建议使用独立的浏览器登录。

## 不使用 Compose

准备 `.env` 后，构建并运行本地源码镜像：

```bash
docker build -t codebuddy2api:local .
docker run -d --name codebuddy2api -p 127.0.0.1:8787:8787 \
  --env-file .env -v "$PWD/auth:/data/auth" \
  -e CODEBUDDY_AUTH_DIR=/data/auth codebuddy2api:local
```

该命令显式使用默认端口与目录，不依赖 Compose 的端口映射。在 WebUI 添加账号，或运行 `docker exec -it codebuddy2api python3 converter.py login --no-browser`。

## 验证

`curl http://127.0.0.1:8787/health` 应返回 `{"status":"ok"}`。它只表示存活，不代表账号或模型可用；请在 WebUI 中查看。API 接入见[客户端配置](clients.zh-CN.md)，错误与重试边界见[进阶参考](advanced.zh-CN.md)。
