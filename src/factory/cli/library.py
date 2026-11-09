"""`factory library` shows what the Library of a project holds."""

import argparse
from pathlib import Path

from factory.cli.common import repo_of
from factory.config import ConfigError
from factory.pipeline.library_lookup import GRAPH_KINDS, LibraryError
from factory.wiring import Machine


def add_parser(commands: "argparse._SubParsersAction[argparse.ArgumentParser]") -> None:
    library = commands.add_parser("library", help="show what the Library of a project holds")
    shows = library.add_subparsers(required=True)
    show = shows.add_parser("show", help="print an item of the Library and the Layer it came from")
    show.add_argument("kind", choices=tuple(GRAPH_KINDS), help="the kind of the item, where `run` is a Run graph")
    show.add_argument("name", help="the name of the item")
    show.add_argument("--project", type=Path, default=Path("factory.yaml"), help="project file")
    show.set_defaults(command=command)


def command(args: argparse.Namespace, machine: Machine) -> int:
    """Prints the Pipeline or Run graph of that name as the Layers of the project resolve it, to start a project's own
    copy from."""
    try:
        print(machine.library(repo_of(args.project)).show_graph(args.kind, args.name), end="")
    except LibraryError as error:
        raise ConfigError(str(error)) from error
    return 0
