"""Shared API-key checks and header-only admission before inference buffering."""

import secrets

from fastapi import HTTPException
from starlette.datastructures import Headers
from starlette.responses import JSONResponse

from .inbound_limits import _BODY_PATHS, anthropic_path
from .api_keys import ApiKeyError, api_principal



# Anthropic error types: https://platform.claude.com/docs/en/api/errors
_ANTHROPIC_ERROR_TYPES = {
    "auth_error": "authentication_error",
    "permission_error": "permission_error",
    "rate_limit_error": "rate_limit_error",
    "invalid_request_error": "invalid_request_error",
    "not_found_error": "not_found_error",
    "upstream_error": "api_error",
}

def require_api_key(key, authorization=None, x_api_key=None):
    """Preserve Bearer precedence, X-Api-Key fallback, and optional empty keys."""
    if not key:
        return
    token = ""
    if isinstance(authorization, str) and authorization.startswith("Bearer "):
        token = authorization[7:].strip()
    if not token and isinstance(x_api_key, str):
        token = x_api_key
    if not secrets.compare_digest(token.encode(), key.encode()):
        raise HTTPException(status_code=401, detail={"error": {"message": "invalid api key", "type": "auth_error"}})


class InferenceAuthMiddleware:
    def __init__(self, app, config):
        self.app = app
        self.config = config

    async def __call__(self, scope, receive, send):
        path = scope.get("path", "")
        if (scope["type"] != "http" or scope.get("method") != "POST"
                or path not in _BODY_PATHS):
            return await self.app(scope, receive, send)
        headers = Headers(scope=scope)
        key_store = self.config.get("key_store")
        if key_store is None:
            try:
                require_api_key(self.config.get("api_key"), headers.get("authorization"), headers.get("x-api-key"))
            except HTTPException as error:
                payload = error.detail
                if anthropic_path(scope["path"]):
                    payload = {"type": "error", "error": {**payload["error"], "type": "authentication_error"}}
                response = JSONResponse(payload, status_code=error.status_code, headers=error.headers)
                return await response(scope, receive, send)
            await self.app(scope, receive, send)
            return
        try:
            principal = key_store.authenticate(scope, self.config,
                                               count_rpm=scope.get("method") == "POST")
        except ApiKeyError as error:
            payload = error.detail
            if anthropic_path(scope["path"]):
                error_type = payload["error"]["type"]
                error_type = _ANTHROPIC_ERROR_TYPES.get(error_type, error_type)
                payload = {"type": "error", "error": {**payload["error"], "type": error_type}}
            response = JSONResponse(payload, status_code=error.status_code, headers=error.headers)
            return await response(scope, receive, send)
        state = scope.setdefault("state", {})
        state["api_principal"] = principal
        token = api_principal.set(principal)
        try:
            await self.app(scope, receive, send)
        finally:
            api_principal.reset(token)
