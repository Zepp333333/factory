"""The catalog lists every kind of Run node, which `wiring` registers."""

from factory.pipeline.run_kinds import RunNodeKind
from factory.run.nodes import (
    ci_decision,
    describe,
    fix,
    merge_pr,
    on_tracker,
    open_pr,
    plan,
    run_gate,
    select,
    tickets,
    wait_ci,
    wait_tracker,
)

RUN_NODE_KINDS: tuple[RunNodeKind, ...] = (
    tickets.KIND,
    select.ALL_KIND,
    select.FILTER_KIND,
    plan.KIND,
    describe.KIND,
    open_pr.KIND,
    wait_ci.KIND,
    fix.CI_FIX_KIND,
    fix.ADDRESS_KIND,
    wait_tracker.KIND,
    merge_pr.KIND,
    *on_tracker.KINDS,
    run_gate.KIND,
    ci_decision.KIND,
)
