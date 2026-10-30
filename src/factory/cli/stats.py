"""`factory stats` compares Runs and variants on their metrics."""

import argparse
import json
import sys

from factory.journal import Journal
from factory.metrics import run_metrics
from factory.stats import GROUPS, render, report
from factory.wiring import Machine


def add_parser(commands: "argparse._SubParsersAction[argparse.ArgumentParser]") -> None:
    stats = commands.add_parser("stats", help="compare Runs and variants on their metrics")
    stats.add_argument("run_ids", nargs="+", metavar="run")
    stats.add_argument("--by", choices=sorted(GROUPS), help="group Attempts and Tickets by a fingerprint field")
    stats.add_argument("--stage", help="show only this Stage")
    stats.add_argument("--json", action="store_true", help="print the data as JSON")
    stats.set_defaults(command=command)


def command(args: argparse.Namespace, machine: Machine) -> int:
    journal = Journal(machine.journal_path, machine.clock)
    try:
        missing = [run_id for run_id in args.run_ids if journal.run(run_id) is None]
        if missing:
            print(f"factory: no Run {', '.join(missing)}", file=sys.stderr)
            return 2
        runs = [run_metrics(run_id, journal.events(run_id)) for run_id in args.run_ids]
    finally:
        journal.close()
    data = report(runs, args.by, args.stage)
    print(json.dumps(data, indent=2) if args.json else "\n".join(render(data)))
    return 0
