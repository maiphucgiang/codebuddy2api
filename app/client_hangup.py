"""`stream=false` 的聚合窗口：监听下游断连，并把没听完的那一枪取消掉。"""
from __future__ import annotations

import asyncio


class ClientHungUp(Exception):
    """客户端在响应成形之前挂断了。不是错误，是「没人听了」。"""


async def _listen_for_hangup(request):
    """等 `http.disconnect`；多余的 `http.request` 残留消息不算断连。"""
    while True:
        message = await request.receive()
        if message.get("type") == "http.disconnect":
            return


async def await_or_hangup(awaitable, request):
    """等待一次上游调用；期间下游挂断就取消它。返回原调用的结果，异常原样抛出。

    `request is None` 时退化成普通的 `await`。取消必须等收尾跑完再交出去：httpx 的
    `async with` 在 `finally` 里才 `aclose()` 响应，否则上游连接会留在半关状态。
    """
    if request is None:
        return await awaitable
    work = asyncio.ensure_future(awaitable)
    watch = asyncio.ensure_future(_listen_for_hangup(request))
    try:
        done, _ = await asyncio.wait((work, watch), return_when=asyncio.FIRST_COMPLETED)
        if work in done:
            return work.result()   # 上游先回来：这一次结果是真的完整，照旧交给服务端投递
        work.cancel()
        await asyncio.wait((work,))
        if not work.cancelled():
            work.exception()       # 取消途中自己报了错：也属于「没人听了」，但不留未取回的异常
        raise ClientHungUp
    finally:
        work.cancel()
        watch.cancel()
        await asyncio.gather(work, watch, return_exceptions=True)
