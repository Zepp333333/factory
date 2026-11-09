"""`factory skill install` copies the skills for the owner's own agent into the Target project or the owner's home."""

import argparse
import shutil
from pathlib import Path

from factory.cli.common import repo_of
from factory.wiring import OWNER_SKILL_HOME, OWNER_SKILLS, Machine


def add_parser(commands: "argparse._SubParsersAction[argparse.ArgumentParser]") -> None:
    skill = commands.add_parser("skill", help="manage the skills for your own agent")
    actions = skill.add_subparsers(required=True)
    install = actions.add_parser("install", help="copy the skills to where your agent finds them, or update them")
    install.add_argument("--project", type=Path, default=Path("factory.yaml"), help="project file")
    install.add_argument("--user", action="store_true", help="install in your home, for every repository")
    install.set_defaults(command=command)


def command(args: argparse.Namespace, machine: Machine) -> int:
    """Replaces each skill where it goes whole, so a file that a skill no longer has goes with the update."""
    home = (Path.home() if args.user else repo_of(args.project)) / OWNER_SKILL_HOME
    for source in sorted(path.parent for path in OWNER_SKILLS.glob("*/SKILL.md")):
        target = home / source.name
        verb = "Updated" if target.exists() else "Installed"
        if target.exists():
            shutil.rmtree(target)
        shutil.copytree(source, target)
        print(f"{verb} skill {source.name} in {target}")
    return 0
