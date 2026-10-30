"""Names holds the names that the factory gives to the branches, refs and Sandboxes of a Run and its Tickets."""


def integration_branch(run_id: str) -> str:
    return f"factory/{run_id}"


def abandoned_ref(run_id: str, ticket: str) -> str:
    return f"refs/factory/{run_id}/abandoned/{ticket}"


def ticket_branch(run_id: str, ticket: str) -> str:
    return f"factory/{run_id}--{ticket}"


def sandbox_name(run_id: str, ticket: str) -> str:
    return f"{run_id}-{ticket}"
