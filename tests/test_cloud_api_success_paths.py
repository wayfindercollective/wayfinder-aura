"""Groq / OpenAI / Anthropic success paths through the REAL SDKs, against a
local stand-in for each API (their base URLs honour *_BASE_URL env vars).

Proves the request shape the providers expect (endpoint, Bearer / x-api-key
auth, multipart audio, chat payload) and that the reply is parsed into text,
without any real key or network access.
"""
import json
import sys
import threading
import wave
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))


class _Api(BaseHTTPRequestHandler):
    seen: list = []

    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length)
        _Api.seen.append({
            "path": self.path,
            "auth": self.headers.get("Authorization"),
            "x_api_key": self.headers.get("x-api-key"),
            "body": body,
        })
        if self.path.endswith("/audio/transcriptions"):
            self._reply("Send the report to Maria by Friday.", "text/plain")
        elif self.path.endswith("/chat/completions"):
            self._reply(json.dumps({
                "id": "c1", "object": "chat.completion", "created": 0, "model": "gpt-4o-mini",
                "choices": [{"index": 0, "finish_reason": "stop",
                             "message": {"role": "assistant", "content": "Hello there."}}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
            }), "application/json")
        elif self.path.endswith("/messages"):
            self._reply(json.dumps({
                "id": "m1", "type": "message", "role": "assistant",
                "model": "claude-haiku-4-5-20251001", "stop_reason": "end_turn",
                "content": [{"type": "text", "text": "Hello there."}],
                "usage": {"input_tokens": 1, "output_tokens": 1},
            }), "application/json")
        else:
            self.send_response(404)
            self.end_headers()

    def _reply(self, text, ctype):
        data = text.encode()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args):
        pass


class _FastServer(ThreadingHTTPServer):
    def server_bind(self):
        # HTTPServer.server_bind does a reverse-DNS getfqdn(), which can stall
        # ~30s on a Mac with no PTR record for 127.0.0.1.
        import socketserver
        socketserver.TCPServer.server_bind(self)
        self.server_name, self.server_port = "127.0.0.1", self.server_address[1]


@pytest.fixture
def api(monkeypatch):
    _Api.seen = []
    server = _FastServer(("127.0.0.1", 0), _Api)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_port}"
    monkeypatch.setenv("GROQ_BASE_URL", base)
    monkeypatch.setenv("OPENAI_BASE_URL", base + "/v1")
    monkeypatch.setenv("ANTHROPIC_BASE_URL", base)
    for name in ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "all_proxy"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    yield _Api.seen
    server.shutdown()


@pytest.fixture
def wav(tmp_path):
    path = tmp_path / "clip.wav"
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes(b"\x00\x00" * 16000)
    return str(path)


def test_groq_transcription(api, wav):
    pytest.importorskip("groq")
    from wayfinder.core.transcriber import GroqWhisperBackend
    text = GroqWhisperBackend(api_key="gsk_test_key").transcribe(wav)
    assert "Maria" in text
    (req,) = api
    assert req["path"] == "/openai/v1/audio/transcriptions"
    assert req["auth"] == "Bearer gsk_test_key"
    assert b'name="model"' in req["body"] and b"whisper-large-v3" in req["body"]
    assert b'filename="' in req["body"]


def test_openai_transcription(api, wav):
    pytest.importorskip("openai")
    from wayfinder.core.transcriber import OpenAIWhisperBackend
    text = OpenAIWhisperBackend(api_key="sk-test-key").transcribe(wav)
    assert "Maria" in text
    (req,) = api
    assert req["path"] == "/v1/audio/transcriptions"
    assert req["auth"] == "Bearer sk-test-key"
    assert b"whisper-1" in req["body"]


def test_openai_cleanup(api):
    pytest.importorskip("openai")
    from wayfinder.core.postprocessor import OpenAIBackend
    out = OpenAIBackend(api_key="sk-test-key", model="gpt-4o-mini").process(
        "um hello there", "Clean this dictation: um hello there")
    assert out == "Hello there."
    (req,) = api
    assert req["path"] == "/v1/chat/completions" and req["auth"] == "Bearer sk-test-key"
    payload = json.loads(req["body"])
    assert payload["model"] == "gpt-4o-mini" and "hello there" in json.dumps(payload["messages"])


def test_anthropic_cleanup(api, monkeypatch):
    pytest.importorskip("anthropic")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    from wayfinder.core.postprocessor import AnthropicBackend
    out = AnthropicBackend(api_key="", model="claude-haiku-4-5-20251001").process(
        "um hello there", "Clean this dictation: um hello there")
    assert out == "Hello there."
    (req,) = api
    assert req["path"] == "/v1/messages" and req["x_api_key"] == "sk-ant-test"
    assert json.loads(req["body"])["model"] == "claude-haiku-4-5-20251001"
