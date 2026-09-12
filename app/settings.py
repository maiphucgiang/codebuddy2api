"""Public management settings; never reads or writes .env or credential files."""
from __future__ import annotations

import math
import os


def _item(default, type_, label, *, mode="hot", env=None, minimum=None, maximum=None,
          choices=None, sensitive=False):
    value = {"default": default, "type": type_, "label": label, "mode": mode,
             "env": env, "sensitive": sensitive}
    if minimum is not None:
        value["min"] = minimum
    if maximum is not None:
        value["max"] = maximum
    if choices is not None:
        value["choices"] = choices
    return value


SCHEMA = {
    "host": _item("127.0.0.1", "string", "监听地址", mode="restart"),
    "port": _item(8787, "integer", "监听端口", mode="restart", minimum=1, maximum=65535),
    "api_key": _item(None, "secret", "管理与推理密钥", mode="startup", env="CODEBUDDY2API_KEY", sensitive=True),
    "auth_file": _item(None, "paths", "显式凭证文件", mode="startup", sensitive=True),
    "auth_dir": _item(None, "path", "凭证目录", mode="startup", env="CODEBUDDY_AUTH_DIR", sensitive=True),
    "import_dir": _item(None, "path", "导入目录", mode="startup", env="CODEBUDDY_IMPORT_DIR", sensitive=True),
    "log_path": _item(None, "path", "兼容文本日志", mode="startup", env="CODEBUDDY2API_LOG", sensitive=True),
    "desensitize": _item(False, "boolean", "提示词脱敏"),
    "no_compact": _item(False, "boolean", "保留提示词全文"),
    "skip_check": _item(False, "boolean", "跳过启动预检", mode="restart"),
    "credit_price_cny": _item(0.014, "number", "国内积分单价", minimum=0),
    "usd_rate": _item(7.15, "number", "美元人民币折算率", minimum=0.000001),
    "credit_price_usd": _item(0.03, "number", "国际积分单价", minimum=0),
    "model_catalog_ttl": _item(21600, "integer", "模型目录缓存秒数", minimum=0, maximum=31536000),
    "model_guard": _item(True, "boolean", "表外模型拦截"),
    "max_images": _item(16, "integer", "单请求图片上限", env="CODEBUDDY2API_MAX_IMAGES", minimum=0, maximum=10000),
    "image_policy": _item("truncate", "string", "超额图片策略", env="CODEBUDDY2API_IMAGE_POLICY", choices=["truncate", "error"]),
    "max_request_bytes": _item(32 * 1024 * 1024, "integer", "请求字节上限", env="CODEBUDDY2API_MAX_REQUEST_BYTES", minimum=1, maximum=1024**3),
    "log_body_limit": _item(65536, "integer", "文本正文预览字节", env="CODEBUDDY2API_LOG_BODY_LIMIT", minimum=0, maximum=1024**2),
    "auto_trial": _item(False, "boolean", "自动领取体验积分", env="CODEBUDDY2API_AUTO_TRIAL"),
    "audit_max_bytes": _item(256 * 1024 * 1024, "integer", "审计明细预算", minimum=1024**2, maximum=1024**4),
    "audit_retention_days": _item(30, "integer", "审计明细保留天数", minimum=1, maximum=36500),
    "audit_diagnostic_bytes": _item(8192, "integer", "失败诊断最大字节", minimum=0, maximum=8192),
}


def validate_settings(values):
    if not isinstance(values, dict):
        raise ValueError("values 必须是对象")
    clean = {}
    for key, value in values.items():
        spec = SCHEMA.get(key)
        if spec is None or spec["sensitive"]:
            raise ValueError("未知或启动来源锁定的配置项")
        kind = spec["type"]
        valid = ((kind == "boolean" and type(value) is bool)
                 or (kind == "integer" and type(value) is int)
                 or (kind == "number" and type(value) in (int, float) and math.isfinite(value))
                 or (kind == "string" and isinstance(value, str) and 0 < len(value) <= 255 and not any(ord(c) < 32 for c in value)))
        if not valid:
            raise ValueError(f"{key}: 类型或值无效")
        if "min" in spec and value < spec["min"] or "max" in spec and value > spec["max"]:
            raise ValueError(f"{key}: 超出允许范围")
        if "choices" in spec and value not in spec["choices"]:
            raise ValueError(f"{key}: 不支持的选项")
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
        source = sources.get(key, "default")
        locked = spec["sensitive"] or source in ("cli", "environment", "env")
        item = {"key": key, "value": None if spec["sensitive"] else (config[key] if config.get(key) is not None else spec["default"]),
                "stored": None if spec["sensitive"] else saved.get(key), "source": source,
                "mode": spec["mode"], "type": spec["type"], "label": spec["label"], "locked": locked}
        item.update({field: spec[field] for field in ("choices", "min", "max") if field in spec})
        result.append(item)
    return result
