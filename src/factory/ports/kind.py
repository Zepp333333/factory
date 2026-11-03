"""A Kind owns one kind of adapter of a port, and every registry of `wiring` holds Kinds of one port."""

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

# What a kind's parser tells about a field that it rejects: the field's name and the reason.
Report = Callable[[str, str], None]


@dataclass(frozen=True)
class Choice:
    """A Choice is the block of the project file that picks the adapter of a port: the kind, and the settings that
    kind parsed from the block's other fields."""

    kind: str
    settings: Any = None


@dataclass(frozen=True)
class Problems:
    """Problems are what a kind finds when it checks its settings: `errors` stop `factory check` and the start of a
    Run, and `warnings` only tell the owner."""

    errors: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()


class Tools(Protocol):
    """Tools are what a kind takes from the Machine: the commands that reach GitHub (`gh`) and GitLab (`glab`)."""

    @property
    def gh(self) -> Sequence[str]: ...

    @property
    def glab(self) -> Sequence[str]: ...


@dataclass(frozen=True)
class CheckContext:
    """A CheckContext is what a kind checks its settings against: the Target project at `repo`, its git `remotes`
    by name, the `base` a Run starts from and the Machine's `tools`."""

    repo: Path
    remotes: Mapping[str, str]
    base: str
    tools: Tools


def no_settings(body: Mapping[str, Any], report: Report) -> None:
    """Reads the block of a kind that has no field but `kind`."""
    return None


def no_problems(settings: Any, context: CheckContext) -> Problems:
    return Problems()


@dataclass(frozen=True)
class Kind[A]:
    """A Kind owns one kind of adapter `A`: the `fields` of its port's block it reads besides `kind`,
    the `parse` of those fields into the kind's own settings, the `check` of those settings,
    and the `make` of the adapter from them and the Machine's Tools.

    A port picked by a link has kinds with a `picks` rule on that link, which then is their settings:
    a Tracker kind owns the Spec links it picks, and a PullRequests kind serves the git remote URLs it picks.
    A Tracker kind without `picks` takes every Spec the others do not own.
    `enforces_egress` tells whether a Sandbox holds back every host that `egress` does not list.
    A kind that cannot enforce it refuses a project that sets `egress`, instead of ignoring the field.
    """

    make: Callable[[Any, Tools], A]
    fields: tuple[str, ...] = ()
    parse: Callable[[dict[str, Any], Report], Any] = no_settings
    check: Callable[[Any, CheckContext], Problems] = no_problems
    picks: Callable[[str], bool] | None = None
    enforces_egress: bool = False
