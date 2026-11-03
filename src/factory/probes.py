"""Probes run the Probe of a Gate in the Ticket's Sandbox and turn the finished Probe into the Gate's verdict.

A Gate with `from` runs no Probe of its own and reads the reports of the latest Probe of its source Gate.
A Gate that finds a pass of its Ticket with the same kind, `with` and git tree ends `pass` without a Probe.
"""

import json
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import Any

from factory import gates
from factory.artifacts import write_json_once
from factory.events import Event
from factory.git import HostGit
from factory.isolation import Isolation
from factory.live import LiveTicket, RunningProbe
from factory.names import sandbox_name
from factory.pipeline import Gate
from factory.run_context import Ports, RunContext
from factory.sandbox import SandboxError


class Probes:
    """Probes starts, polls and judges the Probes of a Run.

    `append` journals an event and applies it to the Run state, and `leave` takes the route of a Gate's outcome.
    """

    def __init__(
        self,
        append: Callable[[Event, dict[str, Any]], None],
        ports: Ports,
        git: HostGit,
        isolation: Isolation,
        leave: Callable[[LiveTicket, Gate, str, dict[str, Any] | None], None],
        context: RunContext,
    ) -> None:
        self.append = append
        self.ports = ports
        self.git = git
        self.isolation = isolation
        self.leave = leave
        self.context = context

    def start(self, ticket: LiveTicket, gate: Gate) -> None:
        """Starts the Gate's Probe in the Ticket's Sandbox on the committed state of the Workspace.

        A Gate that can reuse an earlier pass of the Ticket on the same tree ends `pass` instead and starts none.
        A Probe the Sandbox cannot start is lost.
        """
        assert ticket.workspace is not None
        self.git.reset(ticket.workspace, ticket.branch, ignored=gate.clean)
        reuse = {}
        if gate.reuse:
            reuse_key = self._reuse_key(ticket, gate)
            if reuse_key in ticket.passed:
                self._reuse(ticket, gate, reuse_key)
                return
            reuse = {"reuse_key": reuse_key}
        number = ticket.probes.get(gate.name, 0) + 1
        probe_dir = self.context.run_dir.probe(ticket.id, gate.name, number)
        key = {"ticket": ticket.id, "node": gate.name, "attempt": ticket.gate_attempt + 1, "probe": number, **reuse}
        sandbox = sandbox_name(self.context.run_id, ticket.id)
        self.isolation.reap(ticket, sandbox)
        self.append(
            Event.ProbeStarted,
            {
                **key,
                "head": self.git.head(ticket.workspace, ticket.branch),
                "clean": gate.clean,
                "gate_out": str(probe_dir.gate_out),
                "log": str(probe_dir.log),
            },
        )
        probe = gates.probe(gate.kind, gate.with_)
        env = {"GATE_OUT": str(probe_dir.gate_out)}
        try:
            process = self.ports.sandbox.exec(sandbox, probe.argv, stdout=probe_dir.log, env=env)
        except SandboxError as error:
            self.lose(ticket, gate, key, str(error))
            return
        ticket.probe = RunningProbe(
            process=process, dir=probe_dir, key=key, timeout=probe.timeout, started=self.ports.clock.now()
        )

    def _reuse_key(self, ticket: LiveTicket, gate: Gate) -> str:
        """Returns what a pass of the Gate depends on: its kind, its `with` and the git tree of the clean Workspace.

        A kind that judges the lines the Ticket changed depends on the Integration branch tip they are counted from.
        """
        assert ticket.workspace is not None
        tree = self.git.tree(ticket.workspace, ticket.branch)
        synced = ticket.synced if gates.KINDS[gate.kind].evaluates_changed_lines else ""
        return json.dumps([gate.kind, gate.with_, tree, synced], sort_keys=True, default=str)

    def _reuse(self, ticket: LiveTicket, gate: Gate, reuse_key: str) -> None:
        """Ends the Gate `pass` on the Probe of an earlier Gate that passed the same kind and `with` on this tree.

        Like a Gate with `from`, its `probe` and verdict are those of the earlier Gate, which `reused` names.
        """
        passed = ticket.passed[reuse_key]
        artifact = {
            "kind": "gate",
            "node": gate.name,
            "reused": passed.gate,
            "probe": passed.probe,
            "path": passed.verdict,
        }
        self.append(
            Event.GateFinished,
            {
                "ticket": ticket.id,
                "node": gate.name,
                "attempt": ticket.gate_attempt + 1,
                "probe": passed.probe,
                "reused": {"gate": passed.gate, "probe": passed.probe},
                "reuse_key": reuse_key,
                "verdict": passed.verdict,
                "outcome": "pass",
                "reasons": [],
                "artifact": artifact,
                "duration": ticket.seconds_in_node(self.ports.clock.now()),
            },
        )
        self.leave(ticket, gate, "pass", artifact)

    def lose(self, ticket: LiveTicket, gate: Gate, key: dict[str, Any], reason: str) -> None:
        """Records a Probe that left no result, because its Sandbox did not start it or died under it.

        A lost Probe gets no verdict and spends no Round, and the Gate starts another on the next tick.
        The next Probe takes the next `attempt` number, so no two `ProbeStarted` events of a Gate look alike.
        Once the Ticket has lost its Sandbox more often than `sandbox_retries` the Gate escalates.
        The Workspace goes back to its committed state, as after a finished Probe.
        """
        assert ticket.workspace is not None
        ticket.probe = None
        self.append(Event.ProbeLost, {**key, "reason": reason})
        self.git.reset(ticket.workspace, ticket.branch, ignored=False)
        if self.isolation.sandbox_gone_too_often(ticket, "probe_lost", reason):
            self.leave(ticket, gate, "escalate", None)

    def poll(self, ticket: LiveTicket) -> gates.ProbeResult | None:
        """Returns the result of a finished Probe, killing it once it runs past its timeout.

        The Workspace then goes back to its committed state, so nothing the Probe wrote reaches a later commit.
        Raises SandboxLost when the Sandbox died under the Probe.
        """
        assert ticket.probe is not None and ticket.workspace is not None
        running = ticket.probe
        code = running.process.poll()
        elapsed = (self.ports.clock.now() - running.started).total_seconds()
        timed_out = code is None and elapsed >= running.timeout
        if code is None and not timed_out:
            return None
        if timed_out:
            running.process.kill()
        self.git.reset(ticket.workspace, ticket.branch, ignored=False)
        return gates.ProbeResult(
            gate_out=running.dir.gate_out,
            log=running.dir.log,
            exit_code=None if timed_out else code,
            timed_out=timed_out,
            duration=elapsed,
        )

    def evaluate(self, ticket: LiveTicket, gate: Gate, probe: gates.ProbeResult) -> tuple[str, dict[str, Any]]:
        """Writes the verdict on the finished Probe and returns the Gate's outcome with the verdict as its artifact."""
        assert ticket.probe is not None
        running, ticket.probe = ticket.probe, None
        return self._finish(ticket, gate, probe, running.dir.verdict, running.key)

    def evaluate_borrowed(self, ticket: LiveTicket, gate: Gate, source: str) -> tuple[str, dict[str, Any]]:
        """Evaluates a Gate with `from` on the latest Probe of its source Gate, and runs no Probe of its own.

        The source Gate judged the Probe's exit code and timeout, so this verdict reads only its reports.
        Config makes every path pass through the source, so the Workspace is as the source's Probe left it.
        """
        assert source in ticket.probe_results, "Config proves that every path to a Gate passes through its from"
        number, probe = ticket.probe_results[source]
        key = {
            "ticket": ticket.id,
            "node": gate.name,
            "attempt": ticket.gate_attempt + 1,
            "from": source,
            "probe": number,
        }
        verdict = self.context.run_dir.borrowed_verdict(ticket.id, gate.name, source, number)
        return self._finish(ticket, gate, replace(probe, exit_code=0, timed_out=False), verdict, key)

    def _finish(
        self, ticket: LiveTicket, gate: Gate, probe: gates.ProbeResult, verdict_path: Path, key: dict[str, Any]
    ) -> tuple[str, dict[str, Any]]:
        if gates.KINDS[gate.kind].evaluates_changed_lines:
            assert ticket.workspace is not None
            changed = self.git.changed_lines(ticket.workspace, ticket.branch, ticket.synced)
            probe = replace(probe, changed=changed)
        verdict = gates.evaluate(gate.name, gate.kind, gate.with_, probe)
        path = write_json_once(verdict_path, verdict)
        source = {"from": key["from"]} if "from" in key else {}
        artifact = {"kind": "gate", "node": gate.name, **source, "probe": key["probe"], "path": str(path)}
        payload = {
            **key,
            "verdict": str(path),
            "outcome": verdict["outcome"],
            "reasons": verdict["reasons"],
            "artifact": artifact,
            "duration": ticket.seconds_in_node(self.ports.clock.now()),
            "probe_duration": verdict.get("duration"),
            "failed_lines": (verdict.get("summary") or {}).get("matched_count"),
        }
        self.append(Event.GateFinished, payload)
        return str(verdict["outcome"]), artifact
