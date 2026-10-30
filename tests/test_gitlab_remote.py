import pytest

from factory.gitlab import GitLabPullRequests, GitLabTracker, is_hosted_remote_url

TRACKER = GitLabTracker("https://git.example.com/Group/sub/project/-/issues/21")
PULL_REQUESTS = GitLabPullRequests("git.example.com", "Group/sub/project")
PORTED = GitLabPullRequests("git.example.com:8443", "group/sub/project")


@pytest.mark.parametrize(
    "url",
    [
        "git@git.example.com:group/sub/project.git",
        "git@git.example.com:Group/sub/project",
        "https://git.example.com/group/sub/project.git",
        "https://git.example.com/group/sub/project",
        "https://oauth2:token@git.example.com/group/sub/project.git",
        "ssh://git@git.example.com/group/sub/project.git",
        "ssh://git@git.example.com:2222/group/sub/project.git",
        "https://git.example.com:8443/group/sub/project.git",
        "https://GIT.example.com/group/sub/project/",
    ],
)
def test_remote_of_the_project_over_https_or_ssh_matches(url: str) -> None:
    assert PULL_REQUESTS.accepts(url)
    assert PORTED.accepts(url)


@pytest.mark.parametrize(
    "url",
    [
        "git@git.example.com:group/sub/project-fork.git",
        "git@git.example.com:group/project.git",
        "git@git.example.com:other/sub/project.git",
        "git@git.example.com:group/sub/project/extra.git",
        "https://gitlab.com/group/sub/project.git",
        "https://git.example.com.evil.example/group/sub/project.git",
        "git@github.com:group/sub/project.git",
    ],
)
def test_remote_of_any_other_project_does_not_match(url: str) -> None:
    assert not PULL_REQUESTS.accepts(url)


def test_url_is_the_https_git_url_of_the_project() -> None:
    assert PULL_REQUESTS.url() == "https://git.example.com/Group/sub/project.git"
    assert PORTED.url() == "https://git.example.com:8443/group/sub/project.git"


@pytest.mark.parametrize(
    "keyword",
    [
        *("Close", "Closes", "Closed", "Closing"),
        *("Fix", "Fixes", "Fixed", "Fixing"),
        *("Resolve", "Resolves", "Resolved", "Resolving"),
        *("Implement", "Implements", "Implemented", "Implementing"),
    ],
)
@pytest.mark.parametrize(
    "reference",
    [
        "#12",
        "group/project#12",
        "group/sub/project#12",
        "https://git.example.com/group/sub/project/-/issues/12",
        "https://git.example.com/group/sub/project/-/work_items/12",
    ],
)
def test_defuse_turns_each_closing_keyword_before_a_reference_into_refs(keyword: str, reference: str) -> None:
    assert TRACKER.defuse(f"{keyword} {reference}.") == f"Refs {reference}."
    assert TRACKER.defuse(f"{keyword.upper()}: {reference}") == f"Refs: {reference}"
    assert TRACKER.defuse(f"x {keyword.lower()}\n{reference}") == f"x Refs\n{reference}"


def test_defuse_leaves_the_rest_of_the_text_alone() -> None:
    text = "Closes the loop. Fixes nothing, see #12 and the closure of https://git.example.com/a/b/-/merge_requests/3"

    assert TRACKER.defuse(text) == text


@pytest.mark.parametrize(
    ("text", "defused"),
    [
        ("Closes issue #12", "Refs issue #12"),
        ("Fixes issues #12 and #13", "Refs issues #12 and #13"),
        ("Resolves: Issue group/project#12", "Refs: Issue group/project#12"),
    ],
)
def test_defuse_turns_a_closing_keyword_before_the_word_issue_into_refs(text: str, defused: str) -> None:
    assert TRACKER.defuse(text) == defused


def test_pull_requests_of_a_remote_keep_the_port_of_an_https_remote_only() -> None:
    assert GitLabPullRequests.of_remote("https://git.example.com:8443/group/project.git").url() == (
        "https://git.example.com:8443/group/project.git"
    )
    assert GitLabPullRequests.of_remote("ssh://git@git.example.com:2222/group/project.git").url() == (
        "https://git.example.com/group/project.git"
    )


@pytest.mark.parametrize(
    ("url", "hosted"),
    [
        ("https://gitlab.com/group/project.git", True),
        ("git@gitlab.example.com:group/project.git", True),
        ("git@git.example.com:group/project.git", False),
        ("git@github.com:group/project.git", False),
    ],
)
def test_a_host_named_gitlab_is_served_without_the_project_file_naming_it(url: str, hosted: bool) -> None:
    assert is_hosted_remote_url(url) is hosted
