"""`factory skill install` copies the skills for the owner's own agent into the Target project."""

import argparse
import shutil
from pathlib import Path

from factory.cli.common import repo_of
from factory.wiring import Machine


def add_parser(commands: "argparse._SubParsersAction[argparse.ArgumentParser]") -> None:
    skill = commands.add_parser("skill", help="manage the skills for your own agent in the Target project")
    actions = skill.add_subparsers(required=True)
    install = actions.add_parser("install", help="copy the skills into the Target project, or update them")
    install.add_argument("--project", type=Path, default=Path("factory.yaml"), help="project file")
    install.set_defaults(command=command)


def command(args: argparse.Namespace, machine: Machine) -> int:
    """Replaces each skill in the Target project whole, so a file that a skill no longer has goes with the update."""
    home = repo_of(args.project) / machine.owner_skill_home
    for source in sorted(path.parent for path in machine.owner_skills.glob("*/SKILL.md")):
        target = home / source.name
        verb = "Updated" if target.exists() else "Installed"
        if target.exists():
            shutil.rmtree(target)
        shutil.copytree(source, target)
        print(f"{verb} skill {source.name} in {target}")
    return 0
