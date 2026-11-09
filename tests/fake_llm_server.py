"""A tiny OpenAI-compatible HTTP server replaying scripted answers — TESTS ONLY.

Usage in tests::

    with FakeLLMServer(["answer 1", "answer 2"]) as srv:
        url = srv.url   # http://127.0.0.1:<port>/v1

It honours ``stop`` sequences and returns ``usage`` like a real provider.
Status codes can be scripted as ``{"status": 429}`` entries to test retries.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, List, Union


class FakeLLMServer:
    def __init__(self, script: List[Union[str, dict]], model: str = "fake-model"):
        self.script = list(script)
        self.model = model
        self.requests: List[dict] = []
        self.auth_headers: List[str] = []
        server = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a: Any) -> None:
                pass

            def do_POST(self) -> None:  # noqa: N802
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
                server.requests.append(body)
                server.auth_headers.append(self.headers.get("Authorization", ""))
                item = server.script.pop(0) if server.script else "Thought: done\n```python\nfinal_answer('')\n```"
                if isinstance(item, dict) and "status" in item:
                    self.send_response(item["status"])
                    self.send_header("Content-Type", "application/json")
                    self.end_headers()
                    self.wfile.write(json.dumps({"error": {"message": "scripted failure"}}).encode())
                    return
                text = item
                for s in body.get("stop") or []:
                    if s in text:
                        text = text.split(s, 1)[0]
                prompt_chars = sum(len(m.get("content", "")) for m in body.get("messages", []))
                payload = {
                    "id": "fake", "object": "chat.completion", "model": body.get("model"),
                    "choices": [{"index": 0, "message": {"role": "assistant", "content": text},
                                 "finish_reason": "stop"}],
                    "usage": {"prompt_tokens": prompt_chars // 4 + 1, "completion_tokens": len(text) // 4 + 1},
                }
                data = json.dumps(payload).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}/v1"

    def __enter__(self) -> "FakeLLMServer":
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        return self

    def __exit__(self, *exc: Any) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
