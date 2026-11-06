"""`factory doctor` checks what a Run needs on this machine and in this repository, and prints each check with its fix.

Each check stands on its own, so one that fails hides no other, and the exit code tells whether any failed.
"""

import argparse
import shutil
from pathlib import Path

from factory.cli.check import prepared, verified
from factory.cli.common import HOST_TOOL_ERRORS, project_of, repo_of, warn
from factory.config import ConfigError, Project
from factory.core.git import HostGit
from factory.run.run_entry import base_commit, try_gates
from factory.wiring import Machine


def add_parser(commands: "argparse._SubParsersAction[argparse.ArgumentParser]") -> None:
    doctor = commands.add_parser("doctor", help="check what a Run needs on this machine and in this repository")
    doctor.add_argument("--project", type=Path, default=Path("factory.yaml"), help="project file")
    doctor.add_argument("--gates", action="store_true", help="also run the Gates once on the base branch in a Sandbox")
    doctor.set_defaults(command=command)


class _Report:
    """_Report prints each check as it ends and remembers whether one failed."""

    def __init__(self) -> None:
        self.failed = 0

    def check(self, name: str, problem: str | None = None, fix: str = "", more: tuple[str, ...] = ()) -> None:
        if problem is None:
            print(f"ok    {name}")
            return
        self.failed += 1
        first, *rest = problem.splitlines() or [""]
        print(f"FAIL  {name}: {first}")
        for line in (*rest, *more):
            print(f"      {line}")
        print(f"      fix: {fix}")


def command(args: argparse.Namespace, machine: Machine) -> int:
    report = _Report()
    report.check("uv is installed", None if shutil.which("uv") else "uv is not on the PATH", "brew install uv")
    project = _check_project(args.project, machine, report)
    try:
        diagnoses = machine.diagnose(repo_of(args.project), project)
    except HOST_TOOL_ERRORS as error:
        report.check("the repository", str(error), "run `factory doctor` in a git repository")
        diagnoses = []
    for diagnosis in diagnoses:
        report.check(diagnosis.check, diagnosis.problem, diagnosis.fix)
    if args.gates:
        _check_gates(project, machine, report)
    if report.failed:
        print(f"{report.failed} of the checks failed.")
        return 1
    print("Every check passed.")
    return 0


def _check_project(path: Path, machine: Machine, report: _Report) -> Project | None:
    """Reports what `factory check` finds in the project file and returns the project, if it loads."""
    fix = "change what `factory check` names" if path.exists() else "factory init"
    project = None
    try:
        project = project_of(path, machine)
        prepared(project, machine)
        warn(verified(project, machine))
    except ConfigError as error:
        report.check("the project file", "\n".join(error.errors), fix)
    except HOST_TOOL_ERRORS as error:
        report.check("the project file", str(error), fix)
    else:
        report.check("the project file")
    return project


def _check_gates(project: Project | None, machine: Machine, report: _Report) -> None:
    """Reports how each Gate of the Pipeline ends on the base branch."""
    if project is None:
        report.check("the Gates", "the project file does not load, so no Gate ran", "fix the project file first")
        return
    try:
        commit = base_commit(project, machine.code_host_of(project.repo, project.code_host))
        ports = machine.ports_of(project)
        trials = try_gates(project.pipeline, commit, HostGit(project.repo), ports, machine.home / "doctor")
    except HOST_TOOL_ERRORS as error:
        report.check(
            f"the Gates on {project.base}", str(error), "fix the host, then run `factory doctor --gates` again"
        )
        return
    for trial in trials:
        report.check(
            f"Gate {trial.gate} on {project.base}",
            "\n".join(trial.reasons) or None,
            f"make the Gate pass on {project.base} before a Run, and do not loosen it to get there",
            (*trial.tail, f"log: {trial.log}"),
        )
