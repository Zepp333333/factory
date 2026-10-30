"""`factory limits` changes the parallelism, the round limits and the throttle of a Run."""

import argparse
import sys

from factory.cli.common import positive, send_command
from factory.commands import Limits
from factory.journal import Journal
from factory.throttle import SHARES
from factory.wiring import Machine


def add_parser(commands: "argparse._SubParsersAction[argparse.ArgumentParser]") -> None:
    limits = commands.add_parser("limits", help="change the parallelism and the round limits of a Run")
    limits.add_argument("run_id")
    limits.add_argument("--max-parallel", type=positive, help="Tickets at work at once")
    limits.add_argument(
        "--rounds", action="append", default=[], type=_stage_limit, metavar="STAGE=N", help="round limit of a Stage"
    )
    for share in SHARES:
        limits.add_argument(
            f"--{share}",
            action="append",
            default=[],
            type=_window_share,
            metavar="WINDOW=SHARE",
            help=f"{share} of a subscription window such as five_hour=0.5",
        )
    limits.add_argument(
        "--no-throttle", action="append", default=[], metavar="WINDOW", help="lift the brake on a subscription window"
    )
    limits.set_defaults(command=command)


def _stage_limit(text: str) -> tuple[str, int]:
    stage, _, limit = text.partition("=")
    if not stage:
        raise argparse.ArgumentTypeError("must be STAGE=N such as fix=3")
    return stage, positive(limit)


def _window_share(text: str) -> tuple[str, float]:
    window, _, share = text.partition("=")
    try:
        return window, float(share)
    except ValueError:
        raise argparse.ArgumentTypeError("must be WINDOW=SHARE such as five_hour=0.5") from None


def command(args: argparse.Namespace, machine: Machine) -> int:
    throttle: dict[str, dict[str, float] | None] = {window: None for window in args.no_throttle}
    for share in SHARES:
        for window, value in getattr(args, share):
            if throttle.get(window, {}) is None:
                print(f"factory: {window} is both lifted and set", file=sys.stderr)
                return 2
            throttle.setdefault(window, {})[share] = value  # type: ignore[index]
    if args.max_parallel is None and not args.rounds and not throttle:
        print("factory: limits needs --max-parallel, --rounds, --cap, --pace or --no-throttle", file=sys.stderr)
        return 2
    command = Limits(args.max_parallel, dict(args.rounds), throttle)
    return send_command(Journal(machine.journal_path, machine.clock), machine, args.run_id, command)
