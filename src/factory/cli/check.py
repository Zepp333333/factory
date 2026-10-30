"""`factory check` checks the project file and its Pipeline before a Run."""

import argparse
from pathlib import Path

from factory.cli.common import preflight, tracker_of
from factory.config import ConfigError, display_path, load_project
from factory.library_lookup import LibraryError
from factory.wiring import Machine


def add_parser(commands: "argparse._SubParsersAction[argparse.ArgumentParser]") -> None:
    check = commands.add_parser("check", help="check the project file and its Pipeline before a Run")
    check.add_argument(
        "spec", nargs="?", help="link to the GitLab issue of the Spec, whose `glab` access and git remote to check"
    )
    check.add_argument("--project", type=Path, default=Path("factory.yaml"), help="project file")
    check.add_argument("--show", metavar="ROLE", help="print the final prompt and schema of a Role of the Library")
    check.set_defaults(command=command)


def command(args: argparse.Namespace, machine: Machine) -> int:
    project = load_project(args.project, machine.library, machine.kinds)
    library = machine.library(project.repo)
    if errors := library.check():
        raise ConfigError(*errors)
    if args.show is not None:
        try:
            print(library.show(args.show))
        except LibraryError as error:
            raise ConfigError(str(error)) from error
        return 0
    if remotes := machine.unserved_remotes(project):
        raise ConfigError(
            f"no kind of PullRequests serves the git remotes of {project.repo}: {', '.join(remotes)};"
            f" name the code host with `code_host` in the project file, one of {', '.join(machine.kinds.code_hosts)}"
        )
    if args.spec is not None:
        preflight(args.spec, tracker_of(args.spec, None, machine), project, machine)
    print(f"{display_path(project.path)}: OK")
    return 0
