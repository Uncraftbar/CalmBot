"""Connection reuse, session keep-alive and call accounting for cc-ampapi.

cc-ampapi 1.3 has two habits that multiply the bot's load on the AMP panel:

* It opens a brand-new aiohttp.ClientSession for every API call
  (ampapi/base.py: ``async with aiohttp.ClientSession() as session``), so each
  call pays a fresh TCP + TLS handshake. ``install()`` routes those calls
  through one shared keep-alive session instead.
* It logs in again 240 s after each login even while the session is in active
  use, which starts a new session and a new console cursor. AMP sessions only
  expire after 240 s of *inactivity*, so ``install()`` also slides the
  library's expiry clock forward after every successful call.

Calls are counted per API method and summarised in the log every 10 minutes,
so the bot's load on the panel stays visible.
"""
from __future__ import annotations

import asyncio
import inspect
import time
from collections import Counter
from datetime import datetime
from urllib.parse import urlsplit

import aiohttp
import ampapi.base as _ampapi_base

from cogs.utils import get_logger

log = get_logger("amp.http")

# Close idle connections before the panel does, so a pooled connection is
# never one the server has just dropped.
KEEPALIVE_SECONDS = 3.5  # the panel drops idle connections after ~5 s
STATS_LOG_SECONDS = 600.0

_state: dict = {"session": None, "loop": None}
_calls: Counter = Counter()
_new_connections = 0
_window_start = time.monotonic()


def _api_name(url) -> str:
    """'…/API/ADSModule/Servers/<id>/API/Core/GetUpdates' -> 'Core/GetUpdates'."""
    path = urlsplit(str(url)).path
    return path.rsplit("/API/", 1)[-1] or path


async def _on_connection_created(session, trace_ctx, params) -> None:
    global _new_connections
    _new_connections += 1


def _shared_session() -> aiohttp.ClientSession:
    loop = asyncio.get_running_loop()
    session = _state["session"]
    if session is None or session.closed or _state["loop"] is not loop:
        trace = aiohttp.TraceConfig()
        trace.on_connection_create_end.append(_on_connection_created)
        session = aiohttp.ClientSession(
            connector=aiohttp.TCPConnector(keepalive_timeout=KEEPALIVE_SECONDS),
            trace_configs=[trace],
        )
        _state.update(session=session, loop=loop)
    return session


def _record_call(url) -> None:
    global _new_connections, _window_start
    _calls[_api_name(url)] += 1
    elapsed = time.monotonic() - _window_start
    if elapsed < STATS_LOG_SECONDS:
        return
    total = sum(_calls.values())
    breakdown = ", ".join(f"{name} {count}" for name, count in _calls.most_common(6))
    log.info("AMP API usage over the last %.0f min: %d call(s), ~%d/day (%s); %d new connection(s)",
             elapsed / 60, total, total * 86400 / elapsed, breakdown, _new_connections)
    _calls.clear()
    _new_connections = 0
    _window_start = time.monotonic()


class _TrackedSession:
    """The part of ClientSession that ampapi's _call_api uses: ``post``."""
    __slots__ = ("_session", "_responses")

    def __init__(self, session: aiohttp.ClientSession, responses: list):
        self._session = session
        self._responses = responses

    async def post(self, url, **kwargs):
        _record_call(url)
        response = await self._session.post(url, **kwargs)
        self._responses.append(response)
        return response


class _PooledSessionContext:
    """Drop-in for ``async with aiohttp.ClientSession() as session`` in ampapi."""
    __slots__ = ("_responses",)

    def __init__(self, *args, **kwargs):
        self._responses = []

    async def __aenter__(self):
        return _TrackedSession(_shared_session(), self._responses)

    async def __aexit__(self, exc_type, exc, tb):
        # Hand each connection back to the pool. aiohttp closes it instead if
        # the body was not fully read (ampapi's error paths skip reading it).
        for response in self._responses:
            released = response.release()
            if inspect.isawaitable(released):
                await released
        return False


class _AiohttpForAmpapi:
    """Stands in for the ``aiohttp`` module inside ampapi.base only."""
    ClientSession = _PooledSessionContext

    def __getattr__(self, name):
        return getattr(aiohttp, name)


def _with_sliding_session_expiry(call_api):
    async def _call_api(self, *args, **kwargs):
        result = await call_api(self, *args, **kwargs)
        api = kwargs.get("api", args[0] if args else "")
        session = self._bridge._sessions.get(self.instance_id)
        if api != "Core/Login" and session is not None and session.id != "0":
            session.ttl = datetime.now()  # ampapi compares naive local times
        return result

    _call_api.calmbot_sliding_expiry = True
    return _call_api


def install() -> None:
    """Patch cc-ampapi (pooled connections, sliding session expiry). Idempotent."""
    if not isinstance(getattr(_ampapi_base, "aiohttp", None), _AiohttpForAmpapi):
        _ampapi_base.aiohttp = _AiohttpForAmpapi()
    if not getattr(_ampapi_base.Base._call_api, "calmbot_sliding_expiry", False):
        _ampapi_base.Base._call_api = _with_sliding_session_expiry(_ampapi_base.Base._call_api)
    log.info("AMP API: pooled keep-alive connections and sliding session expiry enabled")


async def close() -> None:
    session = _state["session"]
    _state.update(session=None, loop=None)
    if session is not None and not session.closed:
        await session.close()
