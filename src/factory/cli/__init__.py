"""The `factory` CLI is the only interface that starts and inspects Runs."""

import argparse
import sys
from importlib.metadata import version

from factory.cli import attach, cancel, check, clean, decide, finish, limits, override, resume, run, stats, status
from factory.cli.common import HOST_TOOL_ERRORS
from factory.config import ConfigError
from factory.custody import RunTakenOver
from factory.wiring import Machine


def main(argv: list[str] | None = None, machine: Machine | None = None) -> int:
    try:
        args = _parser().parse_args(argv)
    except SystemExit as exit:
        return exit.code if isinstance(exit.code, int) else 1
    machine = machine or Machine()
    try:
        code: int = args.command(args, machine)
    except (ConfigError, *HOST_TOOL_ERRORS, RunTakenOver) as error:
        for message in error.errors if isinstance(error, ConfigError) else [str(error)]:
            print(f"factory: {message}", file=sys.stderr)
        return 2
    return code


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="factory")
    parser.add_argument("--version", action="version", version=f"factory {version('software-factory')}")
    commands = parser.add_subparsers(required=True)
    for module in (run, check, status, stats, decide, cancel, override, limits, resume, finish, clean, attach):
        module.add_parser(commands)
    return parser
