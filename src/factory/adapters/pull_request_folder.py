"""PullRequestFolder keeps the PRs of a Run whose project has no git remote to land through as files of the Spec."""

from pathlib import Path

from factory.ports.tracker import CiStatus, TrackerError


class PullRequestFolder:
    """PullRequestFolder writes each PR of a Run as a file of its folder, which no code host serves and no CI reads.

    A file holds the PR's title, its branches and its body, and the URL of the PR is the file's path.
    """

    pushes = False
    ci_config_paths: tuple[str, ...] = ()

    def __init__(self, folder: Path) -> None:
        self.folder = folder

    def accepts(self, remote_url: str) -> bool:
        """Accepts no git remote, because the PRs stay on the machine."""
        return False

    def url(self) -> str:
        return str(self.folder)

    def open(self, head: str, base: str, title: str, body: Path, layer: int | None = None) -> str:
        """Writes the PR's title, branches and body into its file and returns the file's path.

        The file of a layer in a stack of PRs starts with its number, so the files sort in stack order.
        """
        prefix = f"{layer:02d}-" if layer is not None else ""
        path = self.folder / f"{prefix}{head.replace('/', '--')}.md"
        path.parent.mkdir(exist_ok=True)
        path.write_text(f"# {title}\n\nFrom `{head}` into `{base}`.\n\n{body.read_text()}")
        return str(path)

    def open_command(self, head: str, base: str, title: str, body: Path) -> list[str]:
        """Returns no command, because the factory writes the file on the machine and nothing is left to do by hand."""
        return []

    def edit(self, pr: str, body: Path) -> None:
        """Rewrites the body of the PR's file and keeps its title and branches."""
        title, branches = Path(pr).read_text().split("\n\n", 2)[:2]
        Path(pr).write_text(f"{title}\n\n{branches}\n\n{body.read_text()}")

    def head(self, pr: str) -> str:
        raise TrackerError(f"{pr} is a PR file, whose branch no code host holds")

    def ci(self, pr: str, commit: str) -> CiStatus:
        return CiStatus("absent")


def of_folder(folder: str, tools: object) -> PullRequestFolder:
    """Makes the PullRequestFolder of a folder, which needs no tool of the Machine."""
    return PullRequestFolder(Path(folder))
