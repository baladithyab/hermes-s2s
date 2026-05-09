"""Tests for s2s-server STT/TTS provider: scheme dispatch, health_check, S2SServerUnavailable."""

from __future__ import annotations

import logging
from pathlib import Path

import httpx
import pytest

from hermes_s2s.providers.stt.s2s_server import S2SServerSTT, S2SServerUnavailable as SttUnavail
from hermes_s2s.providers.tts.s2s_server import S2SServerTTS, S2SServerUnavailable as TtsUnavail


def _patch_httpx(monkeypatch, transport: httpx.MockTransport):
    """Replace httpx.post/get/ConnectError so providers use our mock transport."""
    client_factory = lambda **kw: httpx.Client(transport=transport, **{k: v for k, v in kw.items() if k == "timeout"})

    def _post(url, **kw):
        with client_factory(timeout=kw.get("timeout")) as c:
            return c.post(url, **{k: v for k, v in kw.items() if k != "timeout"})

    def _get(url, **kw):
        with client_factory(timeout=kw.get("timeout")) as c:
            return c.get(url, **{k: v for k, v in kw.items() if k != "timeout"})

    monkeypatch.setattr(httpx, "post", _post)
    monkeypatch.setattr(httpx, "get", _get)


def test_http_happy_path(tmp_path, monkeypatch):
    audio = tmp_path / "a.wav"
    audio.write_bytes(b"RIFF....fake")

    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path.endswith("/asr"):
            return httpx.Response(200, json={"transcript": "hello world"})
        if req.url.path.endswith("/tts"):
            return httpx.Response(200, content=b"WAVDATA")
        return httpx.Response(404)

    _patch_httpx(monkeypatch, httpx.MockTransport(handler))

    stt = S2SServerSTT(endpoint="http://localhost:9000/asr")
    out = stt.transcribe(audio)
    assert out["success"] and out["transcript"] == "hello world"

    tts = S2SServerTTS(endpoint="http://localhost:9000/tts")
    out_path = tts.synthesize("hi", tmp_path / "o.wav")
    assert Path(out_path).read_bytes() == b"WAVDATA"


def test_websocket_scheme_warns_and_raises(tmp_path, caplog):
    with caplog.at_level(logging.WARNING):
        stt = S2SServerSTT(endpoint="ws://localhost:9000/asr")
        tts = S2SServerTTS(endpoint="wss://localhost:9000/tts")
    assert any("0.3.0" in r.message or "WebSocket" in r.message for r in caplog.records)
    assert stt._scheme == "ws" and tts._scheme == "ws"

    audio = tmp_path / "a.wav"
    audio.write_bytes(b"x")
    with pytest.raises(SttUnavail, match="0.3.0"):
        stt.transcribe(audio)
    with pytest.raises(TtsUnavail, match="0.3.0"):
        tts.synthesize("hi", tmp_path / "o.wav")


def test_connection_refused_raises(tmp_path, monkeypatch):
    def handler(req: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("Connection refused", request=req)

    _patch_httpx(monkeypatch, httpx.MockTransport(handler))
    audio = tmp_path / "a.wav"
    audio.write_bytes(b"x")

    stt = S2SServerSTT(endpoint="http://localhost:9/asr")
    with pytest.raises(SttUnavail, match="unreachable"):
        stt.transcribe(audio)

    tts = S2SServerTTS(endpoint="http://localhost:9/tts")
    with pytest.raises(TtsUnavail, match="unreachable"):
        tts.synthesize("hi", tmp_path / "o.wav")


def test_health_check_happy_and_sad(monkeypatch):
    calls = {"n": 0}

    def handler(req: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(200, text="ok")
        raise httpx.ConnectError("down", request=req)

    _patch_httpx(monkeypatch, httpx.MockTransport(handler))

    stt = S2SServerSTT(endpoint="http://localhost:9000/asr")
    ok, msg = stt.health_check()
    assert ok is True and "200" in msg

    tts = S2SServerTTS(endpoint="http://localhost:9000/tts")
    ok, msg = tts.health_check()
    assert ok is False and "ConnectError" in msg
