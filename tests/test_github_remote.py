import pytest

from factory.github import Repository

REPO = Repository("Owner", "target")


@pytest.mark.parametrize(
    "url",
    [
        "git@github.com:Owner/target.git",
        "git@github.com:owner/Target",
        "https://github.com/Owner/target.git",
        "https://github.com/Owner/target",
        "https://token@github.com/Owner/target.git",
        "ssh://git@github.com/Owner/target.git",
        "https://github.com/Owner/target/",
    ],
)
def test_remote_of_the_repository_over_https_or_ssh_matches(url: str) -> None:
    assert REPO.is_remote(url)


@pytest.mark.parametrize(
    "url",
    [
        "git@github.com:Owner/target-fork.git",
        "git@github.com:Other/target.git",
        "https://gitlab.com/Owner/target.git",
        "https://github.com.evil.example/Owner/target.git",
        "/srv/git/Owner/target.git",
    ],
)
def test_remote_of_any_other_repository_does_not_match(url: str) -> None:
    assert not REPO.is_remote(url)
