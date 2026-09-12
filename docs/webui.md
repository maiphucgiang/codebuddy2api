# WebUI 与数据管理 / WebUI and data management

## 启动 / Start

```bash
# 仓库根目录 / repository root
(cd web && vp install && vp build)
uv run --env-file .env converter.py --desensitize
```

打开 `/dashboard`，使用当前 `CODEBUDDY2API_KEY` 登录。未设置 key 时管理功能锁定；客户端 `/v1/*` 的无 key 兼容行为不变。使用 HTTPS 或可信本机连接。

Open `/dashboard` and sign in with the configured API key. Management is locked without a key. Existing unauthenticated client API behavior is unchanged. Use HTTPS or a trusted loopback connection.

- 页面 / pages: `/dashboard`, `/dashboard/models`, `/dashboard/credentials`, `/dashboard/logs`, `/dashboard/settings`, `/dashboard/login`.
- 管理 / management: `/admin/*`；原有接口方法和地址不迁移 / existing methods and paths remain.
- 客户端 / clients: `/v1/*`；管理 Cookie 不授权推理请求 / management cookies do not authorize inference.
- `/` 与不存在的页面跳转 `/dashboard`；未知 API、缺失资源返回 JSON/资源错误，不返回 SPA HTML。

## 数据目录 / Data directory

默认目录为 `auth/`，可用 `CODEBUDDY_AUTH_DIR` 指定。Docker 默认 `/data/auth`，Compose 已挂载此目录。

| 文件 / File | 用途 / Purpose |
|---|---|
| `*.info` | 官方凭证原文，仅文件存储 / official credentials, files only |
| `control.sqlite3` | 配置、模型规则、凭证启停元数据 / settings and policies |
| `logs.sqlite3` | 请求、运行、管理事件与聚合 / audit details and aggregates |
| `*-ledger.json`, `model-catalog.json` | 原有积分、Trial、模型目录状态 / existing ledgers and catalog |

SQLite 不保存凭证原文、accessToken、refreshToken 或登录 key。上传和 OAuth 继续使用受控文件写入；导出只包含明确选择的 `.info`，不是整个目录。

Credentials and API keys are not stored in SQLite. Imports and OAuth update the existing controlled files. Exports contain selected `.info` files only, not the entire data directory.

Docker 请挂载完整目录（含 SQLite `-wal`、`-shm`），使用可写本地文件系统。不要让多个网关服务共用一个数据目录；独立 CLI 登录仍通过文件锁协调。重建容器不会删除挂载数据。

Mount the whole writable local directory, including SQLite WAL/SHM files. Use one gateway instance per data directory; standalone CLI login still coordinates via file locks. Container recreation preserves mounted data.

## 模型与凭证 / Models and credentials

- 模型规则默认自动调度；指定区域、产品或凭证后仅在该集合内选路，不静默越界回退。
- 修改对外 ID 默认不保留原 ID；如选择保留，两者共享启停与路由规则。公开 ID 唯一，不允许别名链、保留名称冲突或凭据形状的名称。
- 模型 ID 使用至多 160 字符的安全 ASCII 标识。`auto` 保留动态上游语义，不能被其他模型的别名覆盖。
- 人工停用与临时认证熔断、模型 429 冷却分开；停用不删除文件，不清理同账号配额冷却。已发送请求不因此重放。
- OAuth 在凭证页选择国内/国际站。成功只入库一次；浏览器重启、会话失效或授权过期需要重新发起。
- 上传单文件最多 1 MiB，批量最多 100 个、合计 32 MiB；ZIP 逐项校验，不接受路径穿越或链接。
- 导出包含明文认证信息，需明确确认并妥善保管。

Routing restrictions are strict intersections with account-owned capabilities, balances and cooldowns. Renamed IDs and retained originals share the same policy. Manual disabling does not delete credentials or clear model quota cooldowns. Exports contain plaintext authentication material.

## 配置 / Settings

优先级：显式 CLI > 环境变量 > WebUI 持久化配置 > 默认值。页面显示当前值、保存值、来源及生效方式。

Precedence: explicit CLI > environment > persisted UI settings > defaults. The UI distinguishes effective values, saved values, sources and restart requirements.

- 请求处理、计费显示、目录 TTL、日志策略等可热更新。
- host/port 等启动参数保存后等待重启；CLI 或环境已控制的字段只读。
- API key、凭证目录、导入目录和兼容文本日志路径继续由启动来源管理；不通过 WebUI 改写 `.env` 或任意路径。
- key 生效变化使旧会话失效。模型/设置保存遇到 409 时刷新后重新编辑。
- 若审计策略应用失败，接口返回 503，运行值保持不变；已保存值可在存储恢复后重试或重启应用。

## 日志与统计 / Logs and statistics

SQLite 审计默认开启。初始明细逻辑预算 256 MiB、保留 30 天、诊断元数据预算 8 KiB；在设置页调整。

Auditing is enabled by default: 256 MiB logical detail budget, 30-day retention, and an 8 KiB diagnostic-metadata budget, configurable in the UI.

- 记录白名单元数据、状态、路由、用量及实际尝试，不存完整请求/回答、失败正文、认证头或图片内容。
- `--log` / `CODEBUDDY2API_LOG` 是可选额外文本输出；文本预览并不等于结构化审计。
- 一个客户端请求计一次，内部尝试单列；HTTP 200 后中断不算成功，取消单独记录。
- 未知用量不是零；明确的 `credit=0` 保留。官方账号账单可能包含桌面端流量，与本网关用量分开。
- 小时、每日、累计聚合与明细分表；普通清理不重新计算或删除历史统计。
- 预算仅约束明细逻辑占用，不是整个数据库物理大小上限；聚合及去重记录默认保留。
- 大幅降低预算时按批次清理，`pending_cleanup` 表示尚未收敛；后续请求、查询或存储状态读取会继续推进。
- SQLite 删除行不一定立即缩小文件。当前不自动 VACUUM；不要删除活动 WAL/SHM 文件。
- 存储异常显示降级和丢弃计数（进程内），不承诺异常退出零丢失，也不因此重放上游。
- 旧文本日志保留，不自动导入或伪造完整历史统计；结构化统计从启用时开始。

Metadata, aggregates, unknown-versus-zero usage and streaming failures are preserved distinctly. Detail retention never deletes aggregates. Physical DB/WAL size may exceed the logical detail budget. Large budget reductions converge incrementally; storage failures are explicitly reported. Old text logs are retained, not automatically imported as exact historical usage.

### 清理 / Clearing

| 操作 / Action | 范围 / Effect |
|---|---|
| 清空明细 / Clear details | 删除日志与诊断明细，保留聚合、累计、配置和凭证 |
| 全部清空日志与统计 / Clear all audit data | 删除日志及聚合、推进统计代次；不删除凭证或网关配置 |

全清需要当前 key 和确认文字 `清空全部日志与统计`。此操作不可撤销；建议先备份。在途旧代次记录不会把已清数据写回来。

Full clearing requires the current key and the exact confirmation phrase. It is irreversible; back up first. Old in-flight records cannot resurrect cleared data.

## 备份与回退 / Backup and rollback

停止服务后备份完整数据目录，或使用 SQLite 的在线 backup API。不要仅复制仍在写入的 `.sqlite3` 文件。分别保管凭证和日志备份。

Back up the complete directory after stopping the server, or use SQLite's online backup API. Do not copy only a live database file. Protect credential backups separately.

旧代码不认识新模型规则。回退前暂停推理入口，确认旧版本下的可用模型和账号范围；不要在回退代码时自动覆盖最新 `.info`。已有管理库损坏或 schema 不受支持时拒绝启动，不静默初始化空策略。

Older code does not enforce new policies. Isolate inference before rollback and verify its allowed accounts/models. Preserve the latest credential files; damaged or unsupported control databases must not silently reset policies.

## 开发验证 / Development checks

开发服务器需显式指定管理后端，避免默认连接正在运行的实例：`CODEBUDDY_WEBUI_PROXY=http://127.0.0.1:8787 vp dev`（在 `web/` 下运行）。测试不设置此变量。

For live development, explicitly set `CODEBUDDY_WEBUI_PROXY` to the intended backend; no proxy is enabled by default.

```bash
cd web
vp check
vp test run
vp build
PLAYWRIGHT_BROWSERS_PATH="$PWD/.cache/playwright" vp exec playwright install chromium-headless-shell
PLAYWRIGHT_BROWSERS_PATH="$PWD/.cache/playwright" vp exec playwright test
PLAYWRIGHT_BROWSERS_PATH="$PWD/.cache/playwright" vp exec playwright test -c playwright.integration.config.ts
```

浏览器测试分别使用 mock 管理 API 和临时 FastAPI/SQLite/.info 夹具，禁止真实上游连接，不访问当前运行服务或真实凭证。Python 夹具解释器可用 `WEBUI_PYTHON` 指定。

Browser checks use mocked APIs or a disposable real management backend with synthetic credentials and blocked upstream connections. They never use the running gateway or real credentials. Set `WEBUI_PYTHON` to override the fixture interpreter.
