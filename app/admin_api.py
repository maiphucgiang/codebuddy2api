"""Explicitly installed v1 management API; importing this module has no side effects."""
from __future__ import annotations

from collections import OrderedDict
import io
import json
from pathlib import Path
import threading
import time
from urllib.parse import quote, urlsplit
import zipfile

from fastapi import HTTPException, Request
from starlette.concurrency import run_in_threadpool
from starlette.responses import JSONResponse, Response

from . import auth_oauth
from .admin_auth import AdminAuth, AdminMiddleware, COOKIE_NAME, SESSION_TTL, error_response, same_origin
from .control_store import ConflictError, validate_model
from .credential_io import CredentialFileError, MAX_CREDENTIAL_BYTES, _valid_name, read_import_file
from .settings import SCHEMA, resolve_settings, validate_settings

MAX_UPLOAD_BYTES = 32 * 1024 * 1024
CLEAR_CONFIRMATION = "清空全部日志与统计"


async def _body(request, maximum=65536):
    data = bytearray()
    async for chunk in request.stream():
        data.extend(chunk)
        if len(data) > maximum:
            raise ValueError("请求体超过大小限制")
    try:
        value = json.loads(data)
    except (ValueError, UnicodeError, RecursionError):
        raise ValueError("请求体必须是有效 JSON 对象") from None
    if not isinstance(value, dict):
        raise ValueError("请求体必须是 JSON 对象")
    return value


def _public_credential(item):
    # Never pass arbitrary credential manager fields through to the browser.
    fields = {"id", "account_key", "name", "filename", "enabled", "label", "profile", "region", "site",
              "uid", "nickname", "expires_at", "expiresAt", "health", "cooldown", "cooldowns", "credits",
              "credits_by_profile", "catalog", "catalog_sync", "sync", "generation", "auth_broken",
              "models", "remaining", "enterprise_id", "product", "status", "sync_pending", "sync_error",
              "fail_until", "cooldown_until", "cooldown_remaining", "last_failure_at", "catalog_ready", "bindings",
              "token_expired", "token_expires_at", "last_refresh_time", "sessions", "sticky_sessions", "last_error_code"}
    result = {key: value for key, value in item.items() if key in fields}
    identity = item.get("account_key") or item.get("id")
    if identity and ("/" in str(identity) or "\\" in str(identity)):
        identity = item.get("account_key")
    result["id"] = identity
    for field in ("name", "filename"):
        if field in result:
            result[field] = Path(str(result[field])).name
    return result


def install_admin(app, config, gateway):
    """Install once before serving; config owns control_store, audit_store, api_key.

    Gateway inventories must expose real model ``id`` and credential ``account_key``
    (or fingerprint ``id``), plus a safe ``name``/``filename`` for file operations.
    """
    if getattr(app.state, "admin_installed", False):
        return app.state.admin_auth
    control, audit = config["control_store"], config["audit_store"]
    auth = AdminAuth(config)
    mutation_lock = threading.RLock()
    oauth_lock = threading.RLock()
    oauth_tasks = OrderedDict()

    def event(action, details=None):
        try:
            audit.event("admin", action, details)
        except Exception:
            # AuditStore owns its degraded state; audit failure must not leak secrets.
            pass

    def inventory():
        return gateway.admin_credential_inventory()

    def selected(identity):
        return next((item for item in inventory() if identity == (item.get("account_key") or item.get("id"))), None)

    def known_models():
        return [item["id"] if isinstance(item, dict) else item for item in gateway.admin_model_inventory()]

    def settings_result():
        items = resolve_settings(config)
        for name, label in (("CRED_COOLDOWN", "认证熔断冷却秒数"),
                            ("MODEL_COOLDOWN", "模型冷却兜底秒数"),
                            ("MODEL_COOLDOWN_MAX", "模型冷却最大秒数"),
                            ("STICKY_TTL", "黏绑空闲期限秒数")):
            value = getattr(gateway, name, None)
            if type(value) in (int, float):
                items.append({"key": name.lower(), "value": value, "stored": None, "source": "internal",
                              "mode": "readonly", "type": "number", "label": label, "locked": True})
        return {"revision": control.snapshot()["revision"], "items": items, "audit": audit.storage()}

    def audit_settings(values):
        mapping = {"audit_max_bytes": "max_bytes", "audit_retention_days": "retention_days",
                   "audit_diagnostic_bytes": "preview_limit"}
        return {mapping[key]: value for key, value in values.items() if key in mapping}

    def oauth_dispatch(request):
        identity = request.state.admin_identity
        now = time.monotonic()
        with oauth_lock:
            for task_id, task in list(oauth_tasks.items()):
                if task["expires"] <= now:
                    del oauth_tasks[task_id]
            if request.url.path == "/admin/oauth/start":
                site = request.query_params.get("site", "cn")
                if site not in ("cn", "intl"):
                    return error_response(400, "OAuth 站点必须为 cn 或 intl")
                if len(oauth_tasks) >= 256:
                    return error_response(429, "OAuth 任务过多，请稍后重试")
                try:
                    result = gateway._OAUTH.start(site=site)
                    login_id = result.get("login_id")
                    link = result.get("verification_uri") or result.get("url") or result.get("auth_url") or result.get("login_url")
                    parsed = urlsplit(link or "")
                    if (not login_id or not isinstance(login_id, str) or parsed.scheme != "https"
                            or parsed.username or parsed.password or parsed.port not in (None, 443)
                            or f"https://{parsed.hostname}" not in auth_oauth.ALLOWED_ORIGINS):
                        return error_response(502, "OAuth 返回的授权链接无效")
                    oauth_tasks[login_id] = {"owner": identity, "expires": now + 600, "result": None}
                    return JSONResponse({"login_id": login_id, "verification_uri": link,
                                         **({"expires_in": result["expires_in"]} if "expires_in" in result else {})})
                except Exception:
                    return error_response(502, "OAuth 发起失败，请稍后重试")
            task_id = request.query_params.get("login_id", "")
            task = oauth_tasks.get(task_id)
            if not task or task["owner"] != identity:
                return error_response(404, "OAuth 任务不存在、已过期或不属于当前会话")
            if task["result"] is not None:
                return JSONResponse(task["result"])
            try:
                result = gateway._OAUTH.poll(task_id)
                if not result.get("done"):
                    return JSONResponse({"done": False})
                if result.get("error") or not result.get("cred"):
                    task["result"] = {"done": True, "error": "OAuth 登录失败，请重新发起"}
                else:
                    # Hold the task lock across save so concurrent polls cannot save twice.
                    target = gateway._save_oauth_credential(result["cred"])
                    task["result"] = {"done": True, "uid": result.get("uid"),
                                      "nickname": result.get("nickname") or "", "imported": Path(target).name}
                    event("oauth.saved")
                return JSONResponse(task["result"])
            except Exception:
                task["result"] = {"done": True, "error": "OAuth 轮询或凭证保存失败，请重新发起"}
                return JSONResponse(task["result"])

    async def dispatch(request):
        path, method = request.url.path, request.method
        if (path, method) in (("/admin/oauth/start", "POST"), ("/admin/oauth/poll", "GET")):
            return await run_in_threadpool(oauth_dispatch, request)
        if path == "/admin/credentials" and method == "GET":
            return JSONResponse({"credentials": [_public_credential(item) for item in await run_in_threadpool(inventory)]})
        if path.startswith("/admin/credentials/") and method == "DELETE":
            name = path.removeprefix("/admin/credentials/")
            if not _valid_name(name):
                return error_response(400, "凭证文件名无效")
            try:
                await run_in_threadpool(gateway.admin_delete_guard, name)
            except (ValueError, HTTPException):
                return error_response(409, "凭证仍被模型策略引用，请先移除绑定")
        return None

    # Middleware is deliberately installed only here, never on module import.
    app.add_middleware(AdminMiddleware, auth=auth, dispatch=dispatch)
    app.state.admin_auth = auth
    app.state.admin_installed = True

    def route(method, path):
        def decorate(function):
            async def guarded(request: Request):
                try:
                    result = await function(request)
                    return result
                except ConflictError:
                    return error_response(409, "配置已更新，请刷新后重试")
                except HTTPException as exc:
                    return error_response(exc.status_code, "管理操作不符合当前状态，请刷新后重试")
                except ValueError:
                    return error_response(400, "请求参数、配置或策略无效，请检查类型、范围及冲突")
                except Exception:
                    return error_response(500, "管理操作失败，请检查存储状态后重试")
            guarded.__name__ = function.__name__
            app.add_api_route(path, guarded, methods=[method])
            return function
        return decorate

    @route("POST", "/admin/session")
    async def session_login(request):
        if not same_origin(request):
            return error_response(403, "登录请求 Origin 校验失败")
        data = await _body(request, 8192)
        result, status = auth.login(request, data.get("api_key"))
        if not result:
            return error_response(status, "登录尝试过多，请稍后重试" if status == 429 else "API key 无效")
        sid, session = result
        response = JSONResponse({"authenticated": True, "csrf_token": session["csrf_token"]})
        response.set_cookie(COOKIE_NAME, sid, max_age=SESSION_TTL, httponly=True, secure=request.url.scheme == "https", samesite="strict", path="/admin")
        return response

    @route("GET", "/admin/session")
    async def session_get(request):
        _, session = auth.session(request)
        return JSONResponse({"authenticated": bool(session or auth.header_identity(request)),
                             "csrf_token": session["csrf_token"] if session else None})

    @route("DELETE", "/admin/session")
    async def session_delete(request):
        auth.logout(request)
        response = JSONResponse({"authenticated": False})
        response.delete_cookie(COOKIE_NAME, path="/admin", httponly=True, samesite="strict", secure=request.url.scheme == "https")
        return response

    @route("GET", "/admin/settings")
    async def settings_get(request):
        return JSONResponse(await run_in_threadpool(settings_result))

    @route("PATCH", "/admin/settings")
    async def settings_patch(request):
        body = await _body(request)
        values = validate_settings(body.get("values"))
        locked = {item["key"] for item in resolve_settings(config) if item["locked"]}
        if values.keys() & locked:
            raise ValueError("配置由 CLI 或环境变量锁定，请修改启动来源")
        def apply():
            with mutation_lock:
                control.update_settings(values, body.get("revision"))
                hot = {key: value for key, value in values.items() if SCHEMA[key]["mode"] == "hot"}
                if audit_settings(hot):
                    configured = audit.configure(**audit_settings(hot))
                    if isinstance(configured, dict) and configured.get("ok") is False:
                        return error_response(503, "配置已保存，但尚未应用；请检查审计存储后重试或重启")
                gateway.admin_apply_settings(hot)
                config.update(hot)
                config.setdefault("settings_sources", {}).update({key: "management" for key in hot})
            for key in values:
                event("settings.updated", {"code": key})
            return JSONResponse(settings_result())
        return await run_in_threadpool(apply)

    @route("GET", "/admin/models")
    async def models_get(request):
        snapshot = control.snapshot()
        models = []
        for item in await run_in_threadpool(gateway.admin_model_inventory):
            item = {"id": item} if isinstance(item, str) else dict(item)
            source = item["id"]
            rule = snapshot["models"].get(source, {"public_id": source, "enabled": True, "keep_original": False,
                                                   "region": None, "profile": None, "credential_ids": []})
            models.append({**item, **rule})
        return JSONResponse({"revision": snapshot["revision"], "models": models})

    def checked_rule(source, data):
        rule = validate_model(source, data, control.snapshot()["models"], known_models())
        for identity in rule["credential_ids"]:
            item = selected(identity)
            if item is None:
                raise ValueError("绑定凭证不存在")
            profile = item.get("profile")
            if profile and ((rule["profile"] and rule["profile"] != profile)
                            or (rule["region"] and not profile.startswith(rule["region"] + "-"))):
                raise ValueError("绑定凭证与区域或产品规则冲突")
        return rule

    @route("PUT", "/admin/models/{id:path}")
    async def models_put(request):
        data = await _body(request)
        revision = data.pop("revision", None)
        source = request.path_params["id"]
        def apply():
            with mutation_lock:
                rule = checked_rule(source, data)
                snapshot = control.update_model(source, rule, revision, known_models())
            event("model.updated", {"model": source})
            return JSONResponse({"revision": snapshot["revision"], "model": {"id": source, **rule}})
        return await run_in_threadpool(apply)

    @route("POST", "/admin/models/{id:path}/preview")
    async def models_preview(request):
        data = await _body(request)
        data.pop("revision", None)
        source = request.path_params["id"]
        def preview():
            return JSONResponse(gateway.admin_model_preview(source, checked_rule(source, data)))
        return await run_in_threadpool(preview)

    @route("PATCH", "/admin/credentials/{id}")
    async def credentials_patch(request):
        data = await _body(request)
        if set(data) != {"enabled"} or type(data["enabled"]) is not bool:
            raise ValueError("enabled 必须为布尔值")
        identity = request.path_params["id"]
        def apply():
            if selected(identity) is None:
                return error_response(404, "凭证不存在")
            with mutation_lock:
                # The gateway persists under the pool lock before publishing routing state.
                gateway.admin_set_credential_enabled(identity, data["enabled"])
            event("credential.enabled", {"credential": identity, "enabled": data["enabled"]})
            return JSONResponse({"id": identity, "enabled": data["enabled"], "revision": control.snapshot()["revision"]})
        return await run_in_threadpool(apply)

    def upload(body):
        files = body.get("files")
        if not isinstance(files, list) or not 1 <= len(files) <= 100 or type(body.get("replace", False)) is not bool:
            raise ValueError("files 必须包含 1 至 100 项，replace 必须为布尔值")
        total = 0
        prepared = []
        for item in files:
            if not isinstance(item, dict) or not isinstance(item.get("name"), str) or not isinstance(item.get("content"), str):
                raise ValueError("每个文件必须包含 name 和 UTF-8 content")
            name = item["name"]
            if len(name) > 255 or not _valid_name(name):
                raise ValueError("只允许安全的 .info 文件名")
            content = item["content"].encode("utf-8")
            if len(content) > MAX_CREDENTIAL_BYTES:
                raise ValueError("单文件不能超过 1 MiB")
            total += len(content)
            if total > MAX_UPLOAD_BYTES:
                raise ValueError("批量内容不能超过 32 MiB")
            prepared.append((name, content))
        if len({name for name, _ in prepared}) != len(prepared):
            raise ValueError("批量文件名重复")
        results = []
        with mutation_lock:
            directory = gateway.managed_auth_dir().resolve()
            for name, content in prepared:
                try:
                    data = json.loads(content)
                    uid, invalid = auth_oauth.validate_cred_data(data)
                    if invalid or not isinstance(data.get("account") or {}, dict) or type(data["auth"].get("expiresAt", 0)) not in (int, float):
                        raise CredentialFileError("凭据格式无效")
                    if not body.get("replace", False) and (directory / name).exists():
                        results.append({"name": name, "ok": False, "error": "文件已存在，需明确允许替换"})
                        continue
                    gateway._store_credential(directory, name, content, uid, replace_existing=body.get("replace", False))
                    results.append({"name": name, "ok": True})
                except Exception:
                    results.append({"name": name, "ok": False, "error": "凭据格式、账号冲突或保存目标不符合要求"})
        event("credentials.uploaded", {"count": len(results), "succeeded": sum(item["ok"] for item in results)})
        return results

    @route("POST", "/admin/credentials/upload")
    async def credentials_upload(request):
        # JSON escaping may expand a UTF-8 payload; independently enforce decoded totals.
        body = await _body(request, MAX_UPLOAD_BYTES * 6 + 65536)
        return JSONResponse({"results": await run_in_threadpool(upload, body)})

    def export(body):
        ids = body.get("ids")
        if body.get("confirm") is not True or not isinstance(ids, list) or not 1 <= len(ids) <= 100 or any(not isinstance(i, str) for i in ids):
            raise ValueError("导出需 confirm:true 及 1 至 100 个账号指纹；文件包含明文认证信息")
        if len(set(ids)) != len(ids):
            raise ValueError("导出账号指纹重复")
        files, total = [], 0
        directory = gateway.managed_auth_dir().resolve()
        for identity in ids:
            item = selected(identity)
            name = (item.get("name") or item.get("filename")) if item else None
            if not isinstance(name, str) or not _valid_name(name):
                raise ValueError("导出凭证不存在或不在受控目录")
            name, content = read_import_file(directory, name)
            # Recheck identity on the actual bytes, not just a potentially stale inventory.
            if gateway._credential_identity(json.loads(content)) != identity:
                raise ValueError("凭证身份已变化，请刷新后重试")
            total += len(content)
            if total > MAX_UPLOAD_BYTES:
                raise ValueError("导出内容不能超过 32 MiB")
            files.append((name, content))
        event("credentials.exported", {"count": len(files)})
        if len(files) == 1:
            name, content = files[0]
            return Response(content, media_type="application/octet-stream", headers={"Content-Disposition": "attachment; filename*=UTF-8''" + quote(name, safe="")})
        target = io.BytesIO()
        with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as archive:
            for name, content in files:
                archive.writestr(name, content)
        return Response(target.getvalue(), media_type="application/zip", headers={"Content-Disposition": 'attachment; filename="credentials.zip"'})

    @route("POST", "/admin/credentials/export")
    async def credentials_export(request):
        return await run_in_threadpool(export, await _body(request))

    @route("GET", "/admin/logs")
    async def logs_get(request):
        params = request.query_params
        kind = params.get("kind", "request")
        if kind not in ("request", "runtime", "admin"):
            raise ValueError("日志 kind 无效")
        try:
            limit = int(params.get("limit", "50"))
        except ValueError:
            raise ValueError("limit 必须为整数") from None
        if not 1 <= limit <= 200:
            raise ValueError("limit 必须为 1 至 200")
        filters = {key: params[key] for key in ("model", "credential", "profile", "status", "search") if key in params}
        if any(len(value) > 256 for value in filters.values()) or len(params.get("cursor", "")) > 2048:
            raise ValueError("日志筛选参数过长")
        result = await run_in_threadpool(audit.list_records, kind, limit, params.get("cursor"), **filters)
        if result.get("degraded"):
            return error_response(503, "日志暂时无法读取，请检查审计存储状态")
        return JSONResponse(result)

    @route("GET", "/admin/logs/{id}")
    async def logs_detail(request):
        result = await run_in_threadpool(audit.get_request, request.path_params["id"])
        if result is None and (await run_in_threadpool(audit.storage)).get("degraded"):
            return error_response(503, "请求明细暂时无法确认，请检查审计存储状态")
        return JSONResponse(result) if result is not None else error_response(404, "请求明细不存在或已清理")

    @route("POST", "/admin/logs/clear")
    async def logs_clear(request):
        body = await _body(request, 8192)
        scope = body.get("scope")
        if scope not in ("details", "all"):
            raise ValueError("清理范围必须为 details 或 all")
        if scope == "all" and (body.get("confirmation") != CLEAR_CONFIRMATION or not auth.check_key(body.get("api_key"))):
            return error_response(403, "全部清理需要确认文本及当前 API key")
        result = await run_in_threadpool(audit.clear, scope)
        if isinstance(result, dict) and result.get("ok") is False:
            return error_response(503, "日志清理未完成，请检查审计存储状态")
        return JSONResponse(result)

    @route("GET", "/admin/dashboard")
    async def dashboard_get(request):
        try:
            days = int(request.query_params.get("days", "7"))
        except ValueError:
            raise ValueError("days 必须为 1、7、30 或 90") from None
        if days not in (1, 7, 30, 90):
            raise ValueError("days 必须为 1、7、30 或 90")
        def build_dashboard():
            result = audit.dashboard(days)
            if result.get("degraded"):
                return error_response(503, "统计暂时无法读取，不能确认当前数值；请检查审计存储状态")
            rows = [_public_credential(item) for item in inventory()]
            result.setdefault("health", {"credentials": rows})
            result.setdefault("storage", audit.storage())
            result.setdefault("generated_at", time.time())
            result.setdefault("range", {"days": days})
            result["official_credits"] = {row["id"]: {"credits": row.get("credits"), "name": row.get("name"),
                                                        "fetched_at": (row.get("credits") or {}).get("fetched_at")}
                                          for row in rows}
            return result
        result = await run_in_threadpool(build_dashboard)
        return result if isinstance(result, Response) else JSONResponse(result)

    return auth
