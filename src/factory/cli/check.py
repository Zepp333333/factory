"""`factory check` checks the project file and its Pipeline before a Run."""

import argparse
from pathlib import Path

from factory.cli.common import add_set, checked, invocation_of, project_of, warn
from factory.cli.diagram import stale_blocks
from factory.config import ConfigError, Project, display_path
from factory.pipeline.library_lookup import LibraryError
from factory.ports.tracker import SpecLink
from factory.wiring import Machine


def add_parser(commands: "argparse._SubParsersAction[argparse.ArgumentParser]", trackers: tuple[str, ...]) -> None:
    check = commands.add_parser("check", help="check the project file and its Pipeline before a Run")
    check.add_argument(
        "spec",
        nargs="?",
        help=f"the link or folder of a Spec, whose Tracker kind checks it: {', '.join(trackers)}",
    )
    check.add_argument("--project", type=Path, default=Path("factory.yaml"), help="project file")
    check.add_argument("--show", metavar="ROLE", help="print the final prompt and schema of a Role of the Library")
    add_set(check)
    check.set_defaults(command=command)


def command(args: argparse.Namespace, machine: Machine) -> int:
    project = project_of(args.project, machine, invocation=invocation_of(args.settings))
    library = machine.library(project.repo)
    prepared(project, machine)
    if args.show is not None:
        try:
            print(library.show(args.show))
        except LibraryError as error:
            raise ConfigError(str(error)) from error
        return 0
    link = SpecLink(args.spec) if args.spec is not None else None
    warn(verified(project, machine, link))
    print(f"{display_path(project.path)}: OK")
    return 0


def prepared(project: Project, machine: Machine) -> None:
    """Refuses a Library with errors and a Sandbox the project file cannot have."""
    if errors := machine.library(project.repo).check():
        raise ConfigError(*errors)
    # Making the Sandbox refuses a project `env` that clashes with what the Executor needs.
    machine.sandbox_of(project)


def verified(project: Project, machine: Machine, link: SpecLink | None = None) -> tuple[str, ...]:
    """Refuses what `factory check` finds in a prepared project and, with a `link`, in its Spec's Tracker kind, and
    returns the warnings."""
    if remotes := machine.unserved_remotes(project):
        raise ConfigError(
            f"no kind of PullRequests serves the git remotes of {project.repo}: {', '.join(remotes)};"
            f" name the code host with `code_host` in the project file, one of {', '.join(machine.kinds.code_hosts)}"
        )
    if link is not None:
        machine.tracker_of(link)
    warnings = checked(project, machine, link)
    graphs = (project.pipeline.path, *((project.run_graph.path,) if project.run_graph else ()))
    if stale := stale_blocks(machine.library(project.repo), graphs):
        raise ConfigError(*stale)
    return warnings
