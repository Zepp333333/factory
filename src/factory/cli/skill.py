"""`factory skill install` copies the skills for the owner's own agent into the Target project or the owner's home."""

import argparse
import shutil
from pathlib import Path

from factory.cli.common import repo_of
from factory.wiring import DEFAULT_EXECUTOR, OWNER_SKILLS, Machine, made


def add_parser(commands: "argparse._SubParsersAction[argparse.ArgumentParser]", agents: tuple[str, ...]) -> None:
    skill = commands.add_parser("skill", help="manage the skills for your own agent")
    actions = skill.add_subparsers(required=True)
    install = actions.add_parser("install", help="copy the skills to where your agent finds them, or update them")
    install.add_argument("--project", type=Path, default=Path("factory.yaml"), help="project file")
    install.add_argument("--user", action="store_true", help="install in your home, for every repository")
    install.add_argument(
        "--agent",
        choices=agents,
        default=DEFAULT_EXECUTOR,
        help=f"the Executor kind of the agent you work in, by default {DEFAULT_EXECUTOR}",
    )
    install.set_defaults(command=command)


def command(args: argparse.Namespace, machine: Machine) -> int:
    """Replaces each skill where the Executor of the owner's agent says it goes, whole, so a file that a skill no
    longer has goes with the update."""
    agent = made(machine.registries.executors[args.agent], machine)
    home = agent.skills_dir(None if args.user else repo_of(args.project))
    for source in sorted(path.parent for path in OWNER_SKILLS.glob("*/SKILL.md")):
        target = home / source.name
        verb = "Updated" if target.exists() else "Installed"
        if target.exists():
            shutil.rmtree(target)
        shutil.copytree(source, target)
        print(f"{verb} skill {source.name} in {target}")
    return 0
