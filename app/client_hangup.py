"""非流式（聚合）端点的下游断连监听。

`StreamingResponse` 自带这层保护：Starlette 在 `spec_version < 2.4`（uvicorn 的两个 HTTP 协议
都是 2.3）把 `stream_response` 和 `listen_for_disconnect` 放进同一个任务组，客户端中途挂断，
挂着的上游读就一起被取消。非流式端点没有对应机制 —— 端点直接 `await` 完整聚合，拿到响应对象
之后才第一次 `send`，于是整个聚合窗口里没有任何人消费 `http.disconnect`。

后果不是「回不去」而已（响应本来就发不出去了）：这枪上游照旧跑到完，最长是读超时那样长；额度
照旧烧；`ConcurrencyLimitMiddleware` 的名额照旧被这个已经没有客户端的请求占着，名额用满之后
网关对所有人回 503；审计照旧写 `outcome=success`，因为它确实完整收到了一次上游响应。

本模块把「等一次上游调用」和「等一次断连」放在同一场竞速里：谁先到算谁。断连先到就取消那次
调用，并把取消等干净 —— httpx 的 `async with` 收尾要在 `finally` 里跑完，连接才不会半开着。
"""
from __future__ import annotations

import asyncio


class ClientHungUp(Exception):
    """客户端在响应成形之前挂断了。不是错误，是「没人听了」。"""


async def _listen_for_hangup(request):
    """只在真正收到 `http.disconnect` 时才返回。

    多余的一份 `http.request`（体已读完之后的残留消息）不算断连，继续等 —— 早退会把一个还活着
    的请求误判成挂断。
    """
    while True:
        message = await request.receive()
        if message.get("type") == "http.disconnect":
            return


async def await_or_hangup(awaitable, request):
    """等待一次上游调用；期间下游挂断就取消它。返回原调用的结果，异常原样抛出。

    `request is None` 时退化成普通的 `await`（调用方拿不到 ASGI 请求的场合）。
    """
    if request is None:
        return await awaitable
    work = asyncio.ensure_future(awaitable)
    watch = asyncio.ensure_future(_listen_for_hangup(request))
    try:
        done, _ = await asyncio.wait((work, watch), return_when=asyncio.FIRST_COMPLETED)
        if work in done:
            return work.result()      # 上游先回来：断连与返回之间的窗口里客户端可能已经走了，
                                      # 但这一次结果是真的完整，照旧交给服务端去投递
        work.cancel()
        await asyncio.wait((work,))   # 等收尾跑完再交出去，别把上游连接留在半关状态
        if not work.cancelled():
            work.exception()          # 取消途中它自己又报了错：那也属于「没人听了」，但不留未取回的异常
        raise ClientHungUp
    finally:
        work.cancel()
        watch.cancel()
        await asyncio.gather(work, watch, return_exceptions=True)
