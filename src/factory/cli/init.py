"""`factory init` writes a project file with working defaults for the repository it runs in.

Each value carries a comment that says where it came from, and a value it could not detect is a commented placeholder.
`factory check` names a placeholder of a value a project must have, and `factory init` also prints the optional ones.
"""

import argparse
import sys
from dataclasses import dataclass
from pathlib import Path

import yaml

from factory.cli.common import repo_of
from factory.cli.stacks import Stack, detected
from factory.config import ConfigError
from factory.core.git import GitError, HostGit
from factory.wiring import SETUP_SANDBOX, Machine

PIPELINE = "tdd-review"
RUN_GRAPH = "default"

# What each `vars` of the shipped Pipeline holds, for the placeholder of a value that detection missed.
VARS = {
    "build": "the command that builds the project",
    "test": "the command that runs the tests and writes its reports into $GATE_OUT",
    "junit": "the name of the JUnit report inside $GATE_OUT",
    "coverage_format": "lcov, cobertura, jacoco or gocover",
    "coverage_report": "the name of the coverage report inside $GATE_OUT",
}


def add_parser(commands: "argparse._SubParsersAction[argparse.ArgumentParser]") -> None:
    init = commands.add_parser("init", help="write a project file with working defaults for this repository")
    init.add_argument("--project", type=Path, default=Path("factory.yaml"), help="project file")
    init.add_argument("--yes", action="store_true", help="take the detected values without asking")
    init.add_argument("--force", action="store_true", help="when the project file exists, write <file>.new next to it")
    init.set_defaults(command=command)


@dataclass
class _Value:
    """A _Value is one key the owner may be asked about: what was found for it, or the `hint` of a placeholder."""

    key: str
    value: str | None
    source: str
    hint: str = ""


def command(args: argparse.Namespace, machine: Machine) -> int:
    target = args.project
    if target.exists():
        if not args.force:
            raise ConfigError(f"{target} exists and stays as it is; `factory init --force` writes a new one next to it")
        target = target.with_name(f"{target.name}.new")
    repo = repo_of(args.project)
    base, remote_notes = _remote(repo, machine)
    stack = detected(repo)
    values = [base, *_variables(stack)]
    if not args.yes:
        for value in values:
            _ask(value, machine)
    target.write_text(_text(values[0], values[1:], remote_notes, stack))
    print(f"Wrote {target}")
    missing = [value.key for value in values if value.value is None]
    if stack is None:
        missing += ["sandbox.egress", "caches"]
    if missing:
        print(f"Fill in what it could not detect: {', '.join(missing)}. `factory check` names what is still missing.")
    return 0


def _remote(repo: Path, machine: Machine) -> tuple[_Value, list[str]]:
    """Returns `base` from the default branch of the first git remote, and the lines about what follows from that."""
    hint = "the branch a Run starts from"
    try:
        remotes = machine.remotes_of(repo)
    except GitError as error:
        return _Value("base", None, f"not detected, because git failed: {error}", hint), []
    if not remotes:
        notes = ["# No git remote, so a Run takes a Spec folder and keeps its PR there."]
        return _Value("base", None, "not detected, because the repository has no git remote", hint), notes
    name, url = remotes[0]
    host = machine.code_host_kind(url)
    if host is None:
        kinds = ", ".join(machine.kinds.code_hosts)
        notes = [f"# code_host: <one of {kinds}>  # not detected, because no kind serves the remote {name}, {url}"]
    else:
        notes = [f"# The Tracker and the code host, {host}, follow from the git remote {name}, {url}."]
    try:
        branch = HostGit(repo).default_branch(name)
    except GitError as error:
        return _Value("base", None, f"not detected, because `git ls-remote {name}` failed: {error}", hint), notes
    if branch is None:
        return _Value("base", None, f"not detected, because the remote {name} names no default branch", hint), notes
    return _Value("base", branch, f"the default branch of the remote {name}"), notes


def _variables(stack: Stack | None) -> list[_Value]:
    if stack is None:
        return [
            _Value(f"vars.{key}", None, "not detected, because no stack matched", hint) for key, hint in VARS.items()
        ]
    source = f"the stack {stack.name}, from {' and '.join(stack.marks)}"
    return [_Value(f"vars.{key}", getattr(stack, key), source) for key in VARS]


def _ask(value: _Value, machine: Machine) -> None:
    """Asks the owner for one value, and an empty answer or the end of the input keeps what was detected."""
    print(f"{value.key} [{value.value if value.value is not None else value.hint}]: ", end="", flush=True)
    answer = machine.stdin.readline().strip()
    if answer:
        value.value, value.source = answer, "given by the owner"


def _yaml(value: object) -> str:
    """Returns the value as YAML of one line."""
    return yaml.safe_dump(value, default_flow_style=True, width=sys.maxsize).removesuffix("\n...\n").strip()


def _line(indent: str, key: str, value: _Value) -> str:
    if value.value is None:
        # What git printed may run over several lines, and a comment holds one.
        return f"{indent}# {key}: <{value.hint}>  # {' '.join(value.source.split())}"
    return f"{indent}{key}: {_yaml(value.value)}  # {value.source}"


def _text(base: _Value, variables: list[_Value], notes: list[str], stack: Stack | None) -> str:
    lines = [
        "# Written by `factory init`: each value says where it came from, and `factory check` checks them.",
        "# Change what is wrong for this project, and `factory doctor --gates` runs its Gates on the base branch.",
        f"pipeline: {PIPELINE}  # the shipped Pipeline for a Target project",
        f"run: {RUN_GRAPH}  # the shipped Run graph, which `factory library show run {RUN_GRAPH}` prints",
        _line("", "base", base),
        *notes,
        "",
        "# `release` is optional and no file tells it: redeploy when a bad change can be deployed over, and",
        "# shipped_binary when it may stay on the users' machines.",
        "# release: redeploy",
        "",
        "vars:",
        *(_line("  ", value.key.removeprefix("vars."), value) for value in variables),
        "  scope: 'true'  # the default, which limits nothing: a command that fails when a change leaves its Ticket",
        "",
        *_sandbox(stack),
    ]
    return "\n".join(lines) + "\n"


def _sandbox(stack: Stack | None) -> list[str]:
    """Returns the lines of the Sandbox, which reaches the package registry of the stack, and of the `caches`."""
    lines = ["sandbox:", f"  kind: {SETUP_SANDBOX}  # a Sandbox that reaches nothing but what `egress` lists"]
    if stack is None:
        return [
            *lines,
            "  # egress: [<the host of your package registry>]  # not detected, because no stack matched",
            "",
            "# caches: [<the directories your tools fill>]  # not detected, because no stack matched",
        ]
    source = f"the package registry of the stack {stack.name}" if stack.egress else f"the stack {stack.name} has none"
    lines.append(f"  egress: {_yaml(list(stack.egress))}  # {source}")
    if stack.env:
        lines.append(f"  env: {_yaml(stack.env)}  # the stack {stack.name}")
    if stack.caches:
        lines += ["", f"caches: {_yaml(list(stack.caches))}  # the tool directories of the stack {stack.name}"]
    return lines
