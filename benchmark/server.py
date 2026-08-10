#!/usr/bin/env python3
"""Deterministic local Codex API used by the memory benchmark."""

from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import urlparse


MODEL = "benchmark-model"


def _sse(event: dict[str, Any]) -> bytes:
    return f"data: {json.dumps(event, separators=(',', ':'))}\n\n".encode()


def _markdown_response(turn: int, target_bytes: int) -> str:
    heading = f"# Benchmark response {turn}\n\n"
    block = (
        "This paragraph contains **bold text**, `inline code`, and enough words "
        "to exercise terminal wrapping at several widths.\n\n"
        "- retained conversation detail\n"
        "- deterministic memory measurement\n"
        "- aggregate Markdown cache pressure\n\n"
        "```cpp\n"
        "std::size_t measured = retained + temporary;\n"
        "```\n\n"
    )
    text = heading
    while len(text.encode()) < target_bytes:
        text += block
    return text[:target_bytes]


class BenchmarkApi:
    """Owns the HTTP server and exposes completion counters to the PTY driver."""

    def __init__(self, response_bytes: int, stream_delay: float = 0.002) -> None:
        self.response_bytes = response_bytes
        self.stream_delay = stream_delay
        self.turns_completed = 0
        self.summaries_completed = 0
        self.models_requested = 0
        self.failure: str | None = None
        self._next_turn = 1
        self._condition = threading.Condition()

        owner = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, _format: str, *_args: object) -> None:
                return

            def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
                owner._handle_get(self)

            def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
                owner._handle_post(self)

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    @property
    def endpoint(self) -> str:
        host, port = self._server.server_address
        return f"http://{host}:{port}/responses"

    def start(self) -> None:
        self._thread.start()

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=2)

    def wait_for_models(self, timeout: float = 10) -> None:
        self._wait_for(lambda: self.models_requested > 0, "models request", timeout)

    def wait_for_turns(self, count: int, timeout: float = 30) -> None:
        self._wait_for(lambda: self.turns_completed >= count, f"turn {count}", timeout)

    def _wait_for(self, predicate: Any, label: str, timeout: float) -> None:
        deadline = time.monotonic() + timeout
        with self._condition:
            while not predicate():
                if self.failure:
                    raise RuntimeError(f"benchmark API failed: {self.failure}")
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError(f"timed out waiting for {label}")
                self._condition.wait(min(remaining, 0.1))

    def _fail(self, error: Exception) -> None:
        with self._condition:
            self.failure = str(error)
            self._condition.notify_all()

    def _handle_get(self, request: BaseHTTPRequestHandler) -> None:
        try:
            if urlparse(request.path).path != "/models":
                self._send_json(request, 404, {"error": {"message": "not found"}})
                return
            payload = {
                "models": [
                    {
                        "slug": MODEL,
                        "context_window": 272_000,
                        "max_context_window": 272_000,
                        "effective_context_window_percent": 95,
                        "auto_compact_token_limit": None,
                    }
                ]
            }
            self._send_json(request, 200, payload)
            with self._condition:
                self.models_requested += 1
                self._condition.notify_all()
        except Exception as error:  # HTTP handler exceptions otherwise disappear in a thread.
            self._fail(error)

    def _handle_post(self, request: BaseHTTPRequestHandler) -> None:
        try:
            if urlparse(request.path).path != "/responses":
                self._send_json(request, 404, {"error": {"message": "not found"}})
                return
            length = int(request.headers.get("Content-Length", "0"))
            if length > 16 * 1024 * 1024:
                raise ValueError("benchmark request exceeded 16 MiB")
            payload = json.loads(request.rfile.read(length))
            if payload.get("model") != MODEL:
                raise ValueError(f"unexpected model {payload.get('model')!r}")

            summary = payload.get("tools") == []
            if summary:
                text = "The benchmark conversation retained its important deterministic details."
                turn = None
            else:
                with self._condition:
                    turn = self._next_turn
                    self._next_turn += 1
                text = _markdown_response(turn, self.response_bytes)

            input_bytes = len(json.dumps(payload.get("input", []), separators=(",", ":")))
            chunks = [text[index : index + 2048] for index in range(0, len(text), 2048)]
            events = [
                _sse({"type": "response.output_text.delta", "delta": chunk})
                for chunk in chunks
            ]
            events.append(
                _sse(
                    {
                        "type": "response.output_item.done",
                        "item": {
                            "type": "message",
                            "role": "assistant",
                            "content": [{"type": "output_text", "text": text}],
                        },
                    }
                )
            )
            events.append(
                _sse(
                    {
                        "type": "response.completed",
                        "response": {"usage": {"input_tokens": max(10, input_bytes // 3)}},
                    }
                )
            )
            body_size = sum(map(len, events))
            request.send_response(200)
            request.send_header("Content-Type", "text/event-stream")
            request.send_header("Content-Length", str(body_size))
            request.send_header("Connection", "close")
            request.end_headers()
            for event in events:
                request.wfile.write(event)
                request.wfile.flush()
                if self.stream_delay:
                    time.sleep(self.stream_delay)

            with self._condition:
                if summary:
                    self.summaries_completed += 1
                else:
                    self.turns_completed += 1
                self._condition.notify_all()
        except Exception as error:
            self._fail(error)
            try:
                self._send_json(request, 500, {"error": {"message": str(error)}})
            except Exception:
                pass

    @staticmethod
    def _send_json(request: BaseHTTPRequestHandler, status: int, payload: object) -> None:
        body = json.dumps(payload, separators=(",", ":")).encode()
        request.send_response(status)
        request.send_header("Content-Type", "application/json")
        request.send_header("Content-Length", str(len(body)))
        request.send_header("Connection", "close")
        request.end_headers()
        request.wfile.write(body)
