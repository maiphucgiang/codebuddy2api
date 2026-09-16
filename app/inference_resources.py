"""Own bounded upstream clients and account capacity for each inference request."""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from contextvars import ContextVar
from http.cookiejar import CookieJar, DefaultCookiePolicy
import threading

import httpx

from app.site_routing import PROFILE_ENDPOINTS
from app.request_context import with_request_context


class _RejectCookies(DefaultCookiePolicy):
    def set_ok(self, cookie, request):
        return False

    def return_ok(self, cookie, request):
        return False


class UpstreamClients:
    """Keep at most one cookie-free HTTP/1.1 client per trusted origin and event loop."""

    def __init__(self):
        self._clients = {}
        self._loop = asyncio.get_running_loop()
        self._closed = False
        self._origins = {self._origin(url) for url in PROFILE_ENDPOINTS.values()}

    @staticmethod
    def _origin(url):
        value = httpx.URL(url)
        return value.scheme, value.host, value.port

    def get(self, url):
        origin = self._origin(url)
        if self._closed or asyncio.get_running_loop() is not self._loop or origin not in self._origins:
            return None
        if origin not in self._clients:
            self._clients[origin] = httpx.AsyncClient(
                cookies=CookieJar(policy=_RejectCookies()), http2=False,
                limits=httpx.Limits(max_connections=64, max_keepalive_connections=16, keepalive_expiry=30))
        return self._clients[origin]

    async def aclose(self):
        self._closed = True
        clients, self._clients = list(self._clients.values()), {}
        results = await asyncio.gather(*(client.aclose() for client in clients), return_exceptions=True)
        errors = [result for result in results if isinstance(result, BaseException)]
        if errors:
            raise BaseExceptionGroup("Upstream client shutdown failed", errors)


@asynccontextmanager
async def inference_lifespan(app):
    clients = UpstreamClients()
    try:
        yield {"upstream_clients": clients}
    finally:
        await clients.aclose()


class CredentialLease(tuple):
    """Retain the existing (manager, generation) lease shape with idempotent capacity release."""

    def __new__(cls, manager, generation, release):
        lease = super().__new__(cls, (manager, generation))
        lease._release = release
        lease._lock = threading.Lock()
        return lease

    def release(self):
        with self._lock:
            release, self._release = self._release, None
        if release is not None:
            release()


def release_credential(credential):
    if isinstance(credential, CredentialLease):
        credential.release()


class AccountCapacity:
    """Count account leases independently of credential I/O locks."""

    def __init__(self):
        self._lock = threading.Lock()
        self._counts = {}

    def count(self, identity):
        with self._lock:
            return self._counts.get(identity, 0)

    def acquire(self, identity, limit, manager, generation):
        with self._lock:
            count = self._counts.get(identity, 0)
            if limit and count >= limit:
                return None
            self._counts[identity] = count + 1
        return CredentialLease(manager, generation, lambda: self._release(identity))

    def _release(self, identity):
        with self._lock:
            count = self._counts[identity] - 1
            if count:
                self._counts[identity] = count
            else:
                del self._counts[identity]


class RequestResources:
    """Release even leases acquired by a worker after its request has already closed."""

    def __init__(self, clients=None):
        self.clients = clients
        self._lock = threading.Lock()
        self._leases = []
        self._closed = False

    def add(self, lease):
        with self._lock:
            if not self._closed:
                self._leases.append(lease)
                return
        release_credential(lease)
        raise asyncio.CancelledError()

    def close(self):
        with self._lock:
            self._closed = True
            leases, self._leases = self._leases, []
        for lease in leases:
            release_credential(lease)


request_resources = ContextVar("inference_resources", default=None)


class InferenceResourcesMiddleware:
    def __init__(self, app, config=None):
        self.app, self.config = app, config

    async def __call__(self, scope, receive, send):
        await with_request_context(self._serve, scope, receive, send, self.config)

    async def _serve(self, scope, receive, send):
        if (scope["type"] != "http" or scope.get("method") != "POST" or
                scope.get("path") not in ("/v1/chat/completions", "/v1/responses", "/v1/messages")):
            return await self.app(scope, receive, send)
        resources = RequestResources(scope.get("state", {}).get("upstream_clients"))
        token = request_resources.set(resources)
        try:
            await self.app(scope, receive, send)
        finally:
            resources.close()
            request_resources.reset(token)
