"""Public management settings; never reads or writes .env or credential files."""
from __future__ import annotations

import math
import os
import re
from urllib.parse import urlsplit


def normalize_allowed_origins(value):
    """Normalize a comma/space separated origin list; bare hosts default to HTTPS."""
    entries = [entry for entry in re.split(r"[\s,]+", value.strip()) if entry]
    if len(entries) > 32:
        raise ValueError("admin_allowed_origins: 来源数量超出上限")
    normalized = []
    for entry in entries:
        candidate = entry if "://" in entry else f"https://{entry}"
        try:
            parts = urlsplit(candidate)
            port = parts.port
        except ValueError:
            raise ValueError(f"admin_allowed_origins: 来源无效 {entry!r}") from None
        if (parts.scheme not in ("http", "https") or not parts.hostname
                or parts.username is not None or parts.password is not None
                or parts.path not in ("", "/") or parts.query or parts.fragment
                or (port is not None and not 1 <= port <= 65535)):
            raise ValueError(f"admin_allowed_origins: 来源无效 {entry!r}")
        host = f"[{parts.hostname}]" if ":" in parts.hostname else parts.hostname
        default_port = 443 if parts.scheme == "https" else 80
        origin = f"{parts.scheme}://{host}" + (f":{port}" if port is not None and port != default_port else "")
        if origin not in normalized:
            normalized.append(origin)
    return ",".join(normalized)


def normalize_update_mirror(value):
    """A GitHub download proxy prefix such as https://ghfast.top/; empty means direct."""
    value = value.strip()
    if not value:
        return ""
    try:
        parts = urlsplit(value)
        parts.port  # Raises on an invalid port.
    except ValueError:
        raise ValueError("update_mirror: 镜像地址无效") from None
    if (parts.scheme != "https" or not parts.hostname or parts.username is not None
            or parts.password is not None or parts.query or parts.fragment):
        raise ValueError("update_mirror: 镜像地址必须是 https 前缀，例如 https://ghfast.top/")
    return value if value.endswith("/") else value + "/"


def normalize_trusted_proxies(value):
    """Normalize a comma/space separated list of trusted proxy IPs or CIDRs."""
    import ipaddress
    entries = [entry for entry in re.split(r"[\s,]+", value.strip()) if entry]
    if len(entries) > 32:
        raise ValueError("trusted_proxies: 代理数量超出上限")
    normalized = []
    for entry in entries:
        try:
            network = str(ipaddress.ip_network(entry, strict=False))
        except ValueError:
            raise ValueError(f"trusted_proxies: 地址无效 {entry!r}") from None
        if network not in normalized:
            normalized.append(network)
    return ",".join(normalized)


def validate_projection_max_bytes(value):
    """Allow zero or enough room for a bounded head/tail warning."""
    if value != 0 and value < 256:
        raise ValueError("responses_projection_max_bytes 必须为 0 或至少 256")
    return value


def _item(default, type_, label, *, mode="hot", env=None, minimum=None, maximum=None,
          choices=None, sensitive=False, allow_empty=False, max_length=255, validator=None,
          hidden=False, env_scope="runtime", lockable=True):
    value = {"default": default, "type": type_, "label": label, "mode": mode,
             "env": env, "sensitive": sensitive, "hidden": hidden,
             "env_scope": env_scope, "lockable": lockable}
    if minimum is not None:
        value["min"] = minimum
    if maximum is not None:
        value["max"] = maximum
    if choices is not None:
        value["choices"] = choices
    if allow_empty:
        value["allow_empty"] = True
    if max_length != 255:
        value["max_length"] = max_length
    if validator is not None:
        value["validator"] = validator
    return value


SCHEMA = {
    "host": _item("127.0.0.1", "string", "监听地址", mode="restart", env="CODEBUDDY2API_BIND", env_scope="deployment"),
    "port": _item(8787, "integer", "监听端口", mode="restart", env="CODEBUDDY2API_PORT", minimum=1, maximum=65535, env_scope="deployment"),
    "api_key": _item(None, "secret", "管理与推理密钥", mode="startup", env="CODEBUDDY2API_KEY", sensitive=True, env_scope="deployment"),
    "auth_file": _item(None, "paths", "显式凭证文件", mode="startup", sensitive=True, env_scope="deployment"),
    "auth_dir": _item(None, "path", "凭证目录", mode="startup", env="CODEBUDDY_AUTH_DIR", sensitive=True, env_scope="deployment"),
    "import_dir": _item(None, "path", "导入目录", mode="startup", env="CODEBUDDY_IMPORT_DIR", sensitive=True, env_scope="deployment"),
    "log_path": _item(None, "path", "旧文本日志（已停用）", mode="startup", env="CODEBUDDY2API_LOG", sensitive=True, hidden=True, env_scope="deployment"),
    "admin_allowed_origins": _item("", "string", "管理页额外信任来源", env="CODEBUDDY2API_ADMIN_ORIGINS",
                                   allow_empty=True, max_length=2000, validator=normalize_allowed_origins),
    "desensitize": _item(False, "boolean", "提示词脱敏"),
    "no_compact": _item(False, "boolean", "保留提示词全文"),
    "keep_tool_metadata": _item(False, "boolean", "保留工具描述", env="CODEBUDDY2API_KEEP_TOOL_METADATA"),
    "skip_check": _item(False, "boolean", "跳过启动预检", mode="restart"),
    "credit_price_cny": _item(0.014, "number", "国内积分单价", minimum=0),
    "usd_rate": _item(7.15, "number", "美元人民币折算率", minimum=0.000001),
    "credit_price_usd": _item(0.03, "number", "国际积分单价", minimum=0),
    "model_catalog_ttl": _item(21600, "integer", "模型目录缓存秒数", minimum=0, maximum=31536000),
    "model_guard": _item(True, "boolean", "表外模型拦截"),
    "routing_scheduler": _item("shadow", "string", "路由调度模式",
                               env="CODEBUDDY2API_ROUTING_SCHEDULER",
                               choices=["legacy", "shadow", "indexed"]),
    "catalog_probe_mode": _item("account", "string", "目录探测模式",
                                env="CODEBUDDY2API_CATALOG_PROBE_MODE",
                                choices=["account", "representative"]),
    "housekeep_concurrency": _item(4, "integer", "后台维护并发数", env="CODEBUDDY2API_HOUSEKEEP_CONCURRENCY",
                                   minimum=1, maximum=16),
    "model_capability_guard": _item(True, "boolean", "模型能力预检", env="CODEBUDDY2API_MODEL_CAPABILITY_GUARD"),
    "max_images": _item(16, "integer", "单请求图片上限", env="CODEBUDDY2API_MAX_IMAGES", minimum=0, maximum=10000),
    "image_policy": _item("truncate", "string", "超额图片策略", env="CODEBUDDY2API_IMAGE_POLICY", choices=["truncate", "error"]),
    "max_request_bytes": _item(32 * 1024 * 1024, "integer", "请求字节上限", env="CODEBUDDY2API_MAX_REQUEST_BYTES", minimum=1, maximum=1024**3),
    "log_body_limit": _item(65536, "integer", "旧文本预览（已停用）", env="CODEBUDDY2API_LOG_BODY_LIMIT", minimum=0, maximum=1024**2, hidden=True),
    "failover_max": _item(0, "integer", "换凭证重放次数", env="CODEBUDDY2API_FAILOVER_MAX",
                          minimum=0, maximum=10),
    "retry_write_timeout": _item(False, "boolean", "写超时参与重放",
                                 env="CODEBUDDY2API_RETRY_WRITE_TIMEOUT"),
    "upstream_keepalive": _item(False, "boolean", "上游连接复用", mode="restart",
                                env="CODEBUDDY2API_UPSTREAM_KEEPALIVE"),
    "max_inflight_per_account": _item(0, "integer", "单账号在途上限（0 不限制）",
                                      env="CODEBUDDY2API_MAX_INFLIGHT_PER_ACCOUNT", minimum=0, maximum=10000),
    "request_context_mode": _item("legacy", "string", "请求上下文模式",
                                  env="CODEBUDDY2API_REQUEST_CONTEXT_MODE", choices=["legacy", "scoped"]),
    "responses_projection_mode": _item("balanced", "string", "Responses 投影模式",
                                      env="CODEBUDDY2API_RESPONSES_PROJECTION_MODE",
                                      choices=["balanced", "passthrough"]),
    "responses_projection_max_bytes": _item(
        40000, "integer", "Responses 单项字节上限（0 或 ≥256）",
        env="CODEBUDDY2API_RESPONSES_PROJECTION_MAX_BYTES", minimum=0, maximum=33554432,
        validator=validate_projection_max_bytes),
    "stream_mode": _item("compatible", "string", "流式模式（实时模式不重生成工具参数）",
                         env="CODEBUDDY2API_STREAM_MODE", choices=["compatible", "realtime"]),
    "audit_max_bytes": _item(256 * 1024 * 1024, "integer", "审计明细预算", minimum=1024**2, maximum=1024**4),
    "audit_retention_days": _item(30, "integer", "审计明细保留天数", minimum=1, maximum=36500),
    "audit_diagnostic_bytes": _item(1536 * 1024, "integer", "失败诊断最大字节", minimum=0, maximum=4 * 1024 * 1024),
    "trusted_proxies": _item("", "string", "可信代理地址（IP/CIDR，逗号分隔）",
                             env="CODEBUDDY2API_TRUSTED_PROXIES", allow_empty=True,
                             max_length=2000, validator=normalize_trusted_proxies),
    "update_mirror": _item("", "string", "GitHub 下载镜像（前缀，留空直连）", env="CODEBUDDY2API_UPDATE_MIRROR",
                           allow_empty=True, max_length=200, validator=normalize_update_mirror),
    # The WebUI's own slot for one custom mirror, remembered even while another source is in use.
    "update_mirror_custom": _item("", "string", "自定义 GitHub 下载镜像", allow_empty=True, max_length=200,
                                  validator=normalize_update_mirror, hidden=True, env_scope="none"),
    # Scheduled configuration backups (app/backups.py); only scheduled ones are pruned to the limit.
    "backup_auto": _item(False, "boolean", "自动备份配置", env="CODEBUDDY2API_BACKUP_AUTO"),
    "backup_interval_hours": _item(24, "integer", "自动备份间隔（小时）", env="CODEBUDDY2API_BACKUP_INTERVAL_HOURS",
                                   minimum=1, maximum=720),
    "backup_keep": _item(7, "integer", "自动备份保留份数", env="CODEBUDDY2API_BACKUP_KEEP", minimum=1, maximum=100),
    # Interface preference: WebUI and `--lang` write it, nothing maps it from the environment.
    "language": _item("auto", "string", "界面语言", choices=["auto", "zh", "en"],
                      env_scope="ui", lockable=False),
    # One-time migration notice; written by `config migrate` and the WebUI dismiss button.
    "config_notice_muted": _item(False, "boolean", "配置迁移提醒已关闭", hidden=True, env_scope="none"),
}


def validate_settings(values, *, legacy=False):
    if not isinstance(values, dict):
        raise ValueError("values 必须是对象")
    clean = {}
    for key, value in values.items():
        if legacy and key == "auto_trial" and type(value) is bool:
            continue  # Retired persisted switch: accept old databases without enabling claims.
        spec = SCHEMA.get(key)
        if spec is None or spec["sensitive"]:
            raise ValueError("未知或启动来源锁定的配置项")
        kind = spec["type"]
        valid = ((kind == "boolean" and type(value) is bool)
                 or (kind == "integer" and type(value) is int)
                 or (kind == "number" and type(value) in (int, float) and math.isfinite(value))
                 or (kind == "string" and isinstance(value, str)
                     and (spec.get("allow_empty") or 0 < len(value))
                     and len(value) <= spec.get("max_length", 255)
                     and not any(ord(c) < 32 for c in value)))
        if not valid:
            raise ValueError(f"{key}: 类型或值无效")
        if "min" in spec and value < spec["min"] or "max" in spec and value > spec["max"]:
            raise ValueError(f"{key}: 超出允许范围")
        if "choices" in spec and value not in spec["choices"]:
            raise ValueError(f"{key}: 不支持的选项")
        if spec.get("validator"):
            value = spec["validator"](value)
        clean[key] = value
    return clean


def apply_persisted_settings(config, explicit=(), environ=None):
    """Resolve startup precedence after CLI parsing; config holds parsed CLI values."""
    environ = os.environ if environ is None else environ
    saved = config["control_store"].snapshot()["settings"] if config.get("control_store") else {}
    sources = dict(config.get("settings_sources", {}))
    for key, spec in SCHEMA.items():
        if key in explicit:
            sources[key] = "cli"
        elif spec["env"] and spec["env"] in environ:
            sources[key] = "environment"
            if not spec["sensitive"]:
                raw = environ[spec["env"]]
                if spec["type"] == "boolean":
                    if raw.lower() not in ("1", "0", "true", "false", "yes", "no", "on", "off"):
                        raise ValueError(f"{key}: 环境变量布尔值无效")
                    raw = raw.lower() in ("1", "true", "yes", "on")
                elif spec["type"] == "integer":
                    raw = int(raw)
                elif spec["type"] == "number":
                    raw = float(raw)
                config.update(validate_settings({key: raw}))
        elif key in saved:
            config[key] = saved[key]
            sources[key] = "management"
        else:
            if key not in config or config[key] is None:
                config[key] = spec["default"]
            sources.setdefault(key, "default")
    config["settings_sources"] = sources
    return config


def resolve_settings(config):
    saved = config["control_store"].snapshot()["settings"] if config.get("control_store") else {}
    sources = config.get("settings_sources", {})
    result = []
    for key, spec in SCHEMA.items():
        if spec.get("hidden"):  # Retired knobs stay out of the management UI.
            continue
        source = sources.get(key, "default")
        locked = spec.get("lockable", True) and (
            spec["sensitive"] or source in ("cli", "environment", "env", "dotenv"))
        item = {"key": key, "value": None if spec["sensitive"] else (config[key] if config.get(key) is not None else spec["default"]),
                "stored": None if spec["sensitive"] else saved.get(key), "source": source,
                "mode": spec["mode"], "type": spec["type"], "label": spec["label"], "locked": locked}
        item.update({field: spec[field] for field in ("choices", "min", "max") if field in spec})
        result.append(item)
    return result
