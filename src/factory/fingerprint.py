"""The variant fingerprint tells which prompt, schema, Pipeline, Agent kit, model and factory an Attempt worked with.

Every hash is the first `LENGTH` hex digits of a SHA-256 over the content of files, never over their paths or times,
so the same files give the same hash on any machine.
Each field of the fingerprint is a separate value with few distinct values, so a later exporter can use it as a label.
"""

import hashlib
from functools import cache, cached_property
from importlib.metadata import version
from pathlib import Path
from typing import Any

from factory.executor import Executor
from factory.git import GitError, host_env, run
from factory.pipeline import AgentWork, Decision, Pipeline, Role, Stage

LENGTH = 12


def short_hash(*parts: bytes) -> str:
    """Returns the short hash of `parts`, which differ from a different split of the same bytes."""
    digest = hashlib.sha256()
    for part in parts:
        digest.update(len(part).to_bytes(8, "big"))
        digest.update(part)
    return digest.hexdigest()[:LENGTH]


def file_sha(path: Path) -> str:
    return short_hash(path.read_bytes())


def tree_sha(root: Path) -> str:
    """Returns the hash of every file under `root` by its relative path and content, leaving out Python caches."""
    parts: list[bytes] = []
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root)
        if path.is_file() and "__pycache__" not in relative.parts:
            parts += [relative.as_posix().encode(), path.read_bytes()]
    return short_hash(*parts)


def roles(pipeline: Pipeline) -> list[Role]:
    """Returns the Roles that the Pipeline's Stages, model Deciders and Describer use, by name."""
    found: dict[str, Role] = {}
    for node in pipeline.nodes.values():
        if isinstance(node, Stage):
            found[node.role.name] = node.role
        elif isinstance(node, Decision) and node.decider.role is not None:
            found[node.decider.role.name] = node.decider.role
    if pipeline.describer is not None:
        found[pipeline.describer.role.name] = pipeline.describer.role
    return [found[name] for name in sorted(found)]


def pipeline_sha(pipeline: Pipeline) -> str:
    """Returns the hash of `pipeline.yaml` together with the prompt and schema files of its Roles."""
    parts = [pipeline.path.read_bytes()]
    for role in roles(pipeline):
        parts += [role.name.encode(), role.prompt.read_bytes(), role.output.read_bytes()]
    return short_hash(*parts)


def prompt_sha(work: AgentWork) -> str:
    return file_sha(work.role.prompt)


def schema_sha(work: AgentWork) -> str:
    return file_sha(work.role.output)


@cache
def factory_version() -> str:
    """Returns the version of the factory and the commit it runs from, such as `0.1.0+a1f3c9d`.

    A factory that does not run from a git checkout of its own has only its version.
    """
    package = version("software-factory")
    source = Path(__file__).resolve().parent
    try:
        top = Path(run(["rev-parse", "--show-toplevel"], host_env(), cwd=source))
        if top.resolve() / "src" / "factory" != source:
            return package
        return f"{package}+{run(['rev-parse', '--short=7', 'HEAD'], host_env(), cwd=source)}"
    except (GitError, OSError):
        return package


class Variant:
    """A Variant is the fingerprint of the Pipeline and the Executor that a Run works with.

    `run` is what `RunStarted` records, and `of` adds what is specific to one Attempt for `AttemptStarted`.
    """

    def __init__(self, pipeline: Pipeline, executor: Executor) -> None:
        self.pipeline = pipeline
        self.executor = executor

    @cached_property
    def run(self) -> dict[str, Any]:
        return {
            "pipeline_sha": pipeline_sha(self.pipeline),
            "kit_sha": self.executor.kit_sha(),
            "factory_version": factory_version(),
        }

    def of(self, work: AgentWork, model: str) -> dict[str, Any]:
        """Returns the fingerprint of an Attempt of `work` that starts with `model`, after the Stage's `escalate`."""
        return {"prompt_sha": prompt_sha(work), "schema_sha": schema_sha(work), **self.run, "model": model}
