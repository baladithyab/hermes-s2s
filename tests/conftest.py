"""Shared pytest fixtures for hermes-s2s tests.

`mock_ws_server` boots an in-process `websockets.serve` server on an ephemeral
port the moment the fixture is requested. The returned object supports three
usage patterns, all of which appear in R2-A (Gemini) and R2-B (OpenAI) tests:

    # Pattern 1 — scripted + callable-with-None:
    server = await mock_ws_server()                  # explicit start, scripted
    server.add_reply({...})
    server.add_proactive({...})

    # Pattern 2 — user-supplied handler:
    async def handler(ws):
        raw = await ws.recv(); ...
    server = await mock_ws_server(handler)

    # Pattern 3 — direct attribute access on the fixture value:
    mock_ws_server.add_reply({...})
    mock_ws_server.server_close_after = 1
    backend = Backend(connect_url=mock_ws_server.url, ...)

Attributes:
    url              ws://127.0.0.1:<port>
    sent_messages    list[str]       raw frames the client sent
    received         list[dict]      parsed-JSON view of the same
    headers          dict            handshake request headers (first connect)
    scripted_replies list[dict]      FIFO reply queue (scripted mode)
    proactive        list[dict]      dicts pushed immediately on each connect
    server_close_after int | None    close the WS after sending N scripted
                                     replies on the current connection
    connections      int             number of client connects seen
    add_reply(d), add_proactive(d), wait_for_messages(n, timeout)
"""

from __future__ import annotations

import asyncio
import json
from typing import Any, Awaitable, Callable, Dict, List, Optional

import pytest
import pytest_asyncio


class _MockWSServer:
    def __init__(self, port: int) -> None:
        self.port = port
        self.url = f"ws://127.0.0.1:{port}"
        self.sent_messages: List[str] = []
        self.received: List[Dict[str, Any]] = []
        self.headers: Dict[str, str] = {}
        self.scripted_replies: List[Dict[str, Any]] = []
        self.proactive: List[Dict[str, Any]] = []
        self.server_close_after: Optional[int] = None
        self.connections: int = 0
        self._ws_server: Any = None
        self._message_event = asyncio.Event()
        self._user_handler: Optional[Callable[[Any], Awaitable[None]]] = None

    # ---------------- callable form ---------------- #
    async def __call__(
        self, handler: Optional[Callable[[Any], Awaitable[None]]] = None
    ) -> "_MockWSServer":
        """Install a user handler (or revert to scripted mode). Returns self."""
        self._user_handler = handler
        return self

    # ---------------- scripted helpers ---------------- #
    def add_reply(self, msg: Dict[str, Any]) -> None:
        self.scripted_replies.append(msg)

    def add_proactive(self, msg: Dict[str, Any]) -> None:
        self.proactive.append(msg)

    async def wait_for_messages(self, n: int, timeout: float = 2.0) -> None:
        loop = asyncio.get_event_loop()
        deadline = loop.time() + timeout
        while len(self.sent_messages) < n:
            remaining = deadline - loop.time()
            if remaining <= 0:
                raise asyncio.TimeoutError(
                    f"Only {len(self.sent_messages)}/{n} messages received within {timeout}s"
                )
            self._message_event.clear()
            try:
                await asyncio.wait_for(self._message_event.wait(), remaining)
            except asyncio.TimeoutError:
                raise asyncio.TimeoutError(
                    f"Only {len(self.sent_messages)}/{n} messages received within {timeout}s"
                )

    # ---------------- internal ---------------- #
    async def _capture_headers(self, ws: Any) -> None:
        try:
            req = getattr(ws, "request", None)
            if req is not None and getattr(req, "headers", None) is not None:
                if hasattr(req.headers, "raw_items"):
                    self.headers = {k: v for k, v in req.headers.raw_items()}
                else:
                    self.headers = dict(req.headers)
                return
            rh = getattr(ws, "request_headers", None)
            if rh is not None:
                self.headers = dict(rh)
        except Exception:  # noqa: BLE001
            pass

    def _capture_client_frame(self, raw: str) -> None:
        self.sent_messages.append(raw)
        try:
            self.received.append(json.loads(raw))
        except Exception:  # noqa: BLE001
            pass
        self._message_event.set()

    async def _scripted_handler(self, ws: Any) -> None:
        for p in list(self.proactive):
            await ws.send(json.dumps(p))
        replies_sent = 0
        async for raw in ws:
            self._capture_client_frame(raw)
            if self.scripted_replies:
                await ws.send(json.dumps(self.scripted_replies.pop(0)))
                replies_sent += 1
                if (
                    self.server_close_after is not None
                    and replies_sent >= self.server_close_after
                ):
                    await ws.close()
                    return

    async def _dispatch(self, ws: Any, *_extra: Any) -> None:
        self.connections += 1
        await self._capture_headers(ws)
        # Proactive frames go out on every connection regardless of mode.
        for p in list(self.proactive):
            try:
                await ws.send(json.dumps(p))
            except Exception:  # noqa: BLE001
                pass
        try:
            if self._user_handler is not None:
                # Tee `ws.recv` so tests that use a custom handler still get
                # `server.received` / `server.sent_messages` populated.
                original_recv = ws.recv

                async def teeing_recv():
                    raw = await original_recv()
                    self._capture_client_frame(raw)
                    return raw

                ws.recv = teeing_recv  # type: ignore[assignment]
                await self._user_handler(ws)
            else:
                # Scripted mode (note: proactive already sent above, so don't
                # resend inside _scripted_handler).
                replies_sent = 0
                async for raw in ws:
                    self._capture_client_frame(raw)
                    if self.scripted_replies:
                        await ws.send(json.dumps(self.scripted_replies.pop(0)))
                        replies_sent += 1
                        if (
                            self.server_close_after is not None
                            and replies_sent >= self.server_close_after
                        ):
                            await ws.close()
                            return
        except Exception:  # noqa: BLE001 - tests tolerate ungraceful close
            pass


@pytest_asyncio.fixture
async def mock_ws_server(unused_tcp_port):
    """Callable + attribute-access fixture; see module docstring."""
    websockets = pytest.importorskip("websockets")

    server = _MockWSServer(port=unused_tcp_port)

    async def dispatch(ws, *args):
        await server._dispatch(ws, *args)

    ws_server = await websockets.serve(dispatch, "127.0.0.1", unused_tcp_port)
    server._ws_server = ws_server
    try:
        yield server
    finally:
        ws_server.close()
        try:
            await asyncio.wait_for(ws_server.wait_closed(), timeout=2.0)
        except asyncio.TimeoutError:
            pass
