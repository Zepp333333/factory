"""OrcaCard fills the agent card of an Attempt's Orca pane, from the renderer that runs on the host inside that pane.

It maps the card facts to hook events of the `claude` source and posts them to Orca's loopback hook server.
The mapping and the payload fields follow a private interface of Orca, so they all live in this module,
and `docs/research/orca-attempt-card.md` records where each comes from.
A post leaves out `agentProcess`, which Orca would check against known agents and refuse for a python process,
and `transcript_path`, because the transcript lives in the Sandbox and Orca would try to read it.
The card sends no `SessionEnd`.
"""

import http.client
import json
import threading
import urllib.error
import urllib.parse
import urllib.request
from collections import deque
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

SOURCE = "claude"
PANE_VARIABLES = ("ORCA_PANE_KEY", "ORCA_TAB_ID", "ORCA_WORKTREE_ID", "ORCA_AGENT_HOOK_ENDPOINT")
TOKEN_HEADER = "X-Orca-Agent-Hook-Token"

# A post that takes longer is given up, and the renderer waits this long at its end for the posts that are left,
# and then for the post on its way and the newest event, which together stay within the Workbench's grace.
POST_SECONDS = 1.0
CLOSE_SECONDS = 2.0
LAST_POST_SECONDS = 2 * POST_SECONDS
# The most events that wait to be posted, of which a slow server loses the oldest.
BACKLOG = 256

_FORBIDDEN = "the hook server refused the token"


@dataclass(frozen=True)
class Endpoint:
    """An Endpoint is what the pane's endpoint file says of the hook server, which Orca rewrites on each start."""

    port: str
    token: str
    env: str
    version: str


def read_endpoint(path: Path) -> Endpoint | None:
    """Returns the Endpoint in the file of `KEY=VALUE` lines at `path`, or None without a file, a port or a token."""
    try:
        lines = path.read_text().splitlines()
    except (OSError, UnicodeError):
        return None
    values = dict(line.split("=", 1) for line in lines if "=" in line)
    port, token = values.get("ORCA_AGENT_HOOK_PORT"), values.get("ORCA_AGENT_HOOK_TOKEN")
    if not port or not token:
        return None
    return Endpoint(port, token, values.get("ORCA_AGENT_HOOK_ENV", ""), values.get("ORCA_AGENT_HOOK_VERSION", ""))


class HookPoster:
    """A HookPoster posts hook events on a thread of its own, so the rendering never waits for the hook server.

    After a post that failed, only the newest event waits on, because the card shows the latest state anyway.
    """

    def __init__(self, path: Path, endpoint: Endpoint, pane: Mapping[str, str]) -> None:
        self._path = path
        self._endpoint = endpoint
        self._pane = pane
        self._backlog: deque[dict[str, Any]] = deque(maxlen=BACKLOG)
        self._changed = threading.Condition()
        self._closing = False
        self.outcome: str | None = None
        # The hook server is on loopback, so a proxy of the owner's environment must not take the post.
        self._opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def post(self, payload: dict[str, Any]) -> None:
        with self._changed:
            self._backlog.append(payload)
            self._changed.notify()

    def close(self) -> None:
        """Waits a short while for the events that are left, and of those a slow hook server leaves posts the newest."""
        with self._changed:
            self._closing = True
            self._changed.notify()
        self._thread.join(CLOSE_SECONDS)
        if self._thread.is_alive():
            self._keep_newest()
            self._thread.join(LAST_POST_SECONDS)

    def _run(self) -> None:
        while True:
            with self._changed:
                while not self._backlog and not self._closing:
                    self._changed.wait()
                if not self._backlog:
                    return
                payload = self._backlog.popleft()
            why_not = self._send(payload)
            if self.outcome is None:
                self.outcome = (
                    f"Orca card connected to the hook server on port {self._endpoint.port}"
                    if why_not is None
                    else f"Orca card not connected: {why_not}"
                )
            if why_not is not None:
                self._keep_newest()

    def _keep_newest(self) -> None:
        with self._changed:
            while len(self._backlog) > 1:
                self._backlog.popleft()

    def _send(self, payload: dict[str, Any]) -> str | None:
        """Posts `payload`, again with what the endpoint file says after a 403, and returns why it failed, if it did."""
        why_not = self._post(payload)
        if why_not != _FORBIDDEN:
            return why_not
        fresh = read_endpoint(self._path)
        if fresh is None or fresh == self._endpoint:
            return why_not
        self._endpoint = fresh
        return self._post(payload)

    def _post(self, payload: dict[str, Any]) -> str | None:
        endpoint = self._endpoint
        body = urllib.parse.urlencode(
            {**self._pane, "env": endpoint.env, "version": endpoint.version, "payload": json.dumps(payload)}
        )
        request = urllib.request.Request(
            f"http://127.0.0.1:{endpoint.port}/hook/{SOURCE}",
            data=body.encode(),
            headers={TOKEN_HEADER: endpoint.token},
        )
        try:
            with self._opener.open(request, timeout=POST_SECONDS):
                return None
        except urllib.error.HTTPError as error:
            return _FORBIDDEN if error.code == 403 else f"the hook server answered {error.code}"
        except (OSError, http.client.HTTPException) as error:
            return f"the hook server cannot be reached: {error}"


class OrcaCard:
    """An OrcaCard keeps the turn of an Attempt and posts its card facts as hook events of the pane in `env`.

    Without the pane's variables or its endpoint file it posts nothing, and its status says why.
    """

    def __init__(self, env: Mapping[str, str]) -> None:
        self._poster: HookPoster | None = None
        self._why_not: str | None = None
        self._told = False
        self._session: str | None = None
        self._tools: dict[str, dict[str, Any]] = {}
        self._reply = ""
        self._in_turn = False
        missing = [name for name in PANE_VARIABLES if not env.get(name)]
        if missing:
            self._why_not = f"{', '.join(missing)} not set"
            return
        path = Path(env["ORCA_AGENT_HOOK_ENDPOINT"])
        endpoint = read_endpoint(path)
        if endpoint is None:
            self._why_not = f"no port and token in the endpoint file {path}"
            return
        pane = {"paneKey": env["ORCA_PANE_KEY"], "tabId": env["ORCA_TAB_ID"], "worktreeId": env["ORCA_WORKTREE_ID"]}
        self._poster = HookPoster(path, endpoint, pane)

    def show(self, fact: dict[str, Any]) -> None:
        if self._poster is None:
            return
        kind = fact.get("fact")
        if kind == "session":
            self._start(fact.get("session_id"))
        elif self._session is None:
            return
        elif kind == "turn_end":
            self._end_turn()
        else:
            self._in_turn = True
            self._post_fact(kind, fact)

    def status(self) -> str | None:
        """Returns once whether the card is connected, or why not, as soon as that is known, and otherwise None."""
        said = f"Orca card not connected: {self._why_not}" if self._why_not else None
        said = said or (self._poster.outcome if self._poster else None)
        if said is None or self._told:
            return None
        self._told = True
        return said

    def close(self) -> None:
        """Ends a turn the agent left open, so the card never stays on "working", and sends what is left."""
        if self._poster is None:
            return
        self._end_turn()
        self._poster.close()
        if self._poster.outcome is None:
            why_not = "the hook server did not answer in time" if self._session else "the agent started no session"
            self._poster.outcome = f"Orca card not connected: {why_not}"

    def _start(self, session: Any) -> None:
        """Starts the session once, because an agent may repeat its init event, and not without an id to post it by."""
        if session and session != self._session:
            self._session = str(session)
            self._post("SessionStart", source="startup")

    def _post_fact(self, kind: Any, fact: dict[str, Any]) -> None:
        if kind == "prompt":
            self._post("UserPromptSubmit", prompt=fact.get("text"))
        elif kind == "tool":
            tool = {"tool_name": fact.get("name"), "tool_input": fact.get("input"), "tool_use_id": fact.get("id")}
            self._tools[str(fact.get("id"))] = tool
            self._post("PreToolUse", **tool)
        elif kind == "tool_output":
            tool = self._tools.pop(str(fact.get("id")), {"tool_use_id": fact.get("id")})
            self._post("PostToolUse", **tool, tool_response=fact.get("output"))
        elif kind == "reply":
            self._reply = str(fact.get("text"))
        elif kind == "subagent_start":
            self._post("SubagentStart", agent_id=fact.get("id"), agent_type=fact.get("agent_type"))
        elif kind == "subagent_stop":
            self._post("SubagentStop", agent_id=fact.get("id"), agent_type=fact.get("agent_type"))

    def _end_turn(self) -> None:
        if self._in_turn:
            self._in_turn = False
            self._post("Stop", last_assistant_message=self._reply)

    def _post(self, event: str, **fields: Any) -> None:
        assert self._poster is not None
        self._poster.post({"hook_event_name": event, "session_id": self._session, **fields})
