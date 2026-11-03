"""Names holds the names that the factory gives to the branches, refs and Sandboxes of a Run and its Tickets."""

import re


def integration_branch(run_id: str) -> str:
    return f"factory/{run_id}"


def abandoned_ref(run_id: str, ticket: str) -> str:
    return f"refs/factory/{run_id}/abandoned/{ticket}"


def layer_branch(run_id: str, layer: int, title: str) -> str:
    """Returns the branch of layer `layer` of a stacked Run, named after the Ticket it lands.

    It sits beside the Integration branch, because a ref cannot be both `factory/<run-id>` and a directory.
    """
    slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")[:40].strip("-")
    return f"factory/{run_id}--{layer}-{slug}" if slug else f"factory/{run_id}--{layer}"


def ticket_branch(run_id: str, ticket: str) -> str:
    return f"factory/{run_id}--{ticket}"


def sandbox_name(run_id: str, ticket: str) -> str:
    return f"{run_id}-{ticket}"
