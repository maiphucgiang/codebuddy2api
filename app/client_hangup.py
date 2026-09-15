"""Cancel non-streaming upstream work when the downstream client disconnects."""
from __future__ import annotations

import asyncio


class ClientHungUp(Exception):
    """Signal client cancellation before a response is ready."""


async def _listen_for_hangup(request):
    """Wait for http.disconnect while ignoring remaining request-body messages."""
    while True:
        message = await request.receive()
        if message.get("type") == "http.disconnect":
            return


async def await_or_hangup(awaitable, request):
    """Await upstream work, cancelling and draining it on disconnect; await directly without a request."""
    if request is None:
        return await awaitable
    work = asyncio.ensure_future(awaitable)
    watch = asyncio.ensure_future(_listen_for_hangup(request))
    try:
        done, _ = await asyncio.wait((work, watch), return_when=asyncio.FIRST_COMPLETED)
        if work in done:
            return work.result()   # Preserve a result that completed first.
        work.cancel()
        await asyncio.wait((work,))
        if not work.cancelled():
            work.exception()       # Retrieve errors raised during cancellation.
        raise ClientHungUp
    finally:
        work.cancel()
        watch.cancel()
        await asyncio.gather(work, watch, return_exceptions=True)
