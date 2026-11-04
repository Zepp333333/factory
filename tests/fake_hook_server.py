"""Fake hook server of Orca on loopback, which keeps what a post with the right token sends it.

Like Orca, it answers a wrong token with 403, and it can be made to fail, to answer late or to hang instead.
"""

import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qsl


@dataclass
class Post:
    """A Post is one request the server took: its `path` and the fields of its form-encoded body."""

    path: str
    form: dict[str, str]


@dataclass
class HookServer:
    """A HookServer runs on a port of its own until `stop`, and `on_forbidden` runs before it answers 403."""

    token: str = "token"
    status: int = 204
    on_forbidden: Callable[[], None] = lambda: None
    posts: list[Post] = field(default_factory=list)
    release: threading.Event = field(default_factory=threading.Event)
    hangs: bool = False
    answer_seconds: float = 0

    def __post_init__(self) -> None:
        hook = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                body = self.rfile.read(int(self.headers["Content-Length"])).decode()
                if self.headers["X-Orca-Agent-Hook-Token"] != hook.token:
                    hook.on_forbidden()
                    self.send_response(403)
                    self.end_headers()
                    return
                hook.posts.append(Post(self.path, dict(parse_qsl(body))))
                if hook.hangs or hook.answer_seconds:
                    hook.release.wait(hook.answer_seconds or 30)
                self.send_response(hook.status)
                self.end_headers()

            def log_message(self, format: str, *args: Any) -> None:
                pass

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._server.daemon_threads = True
        threading.Thread(target=self._server.serve_forever, daemon=True).start()

    @property
    def port(self) -> int:
        return self._server.server_address[1]

    def stop(self) -> None:
        self.release.set()
        self._server.shutdown()
        self._server.server_close()
