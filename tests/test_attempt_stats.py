"""AttemptStats: what the Executor folds out of the stream of a `claude -p` that ran subagents.

The recorded stream has the shape of a real one: every content block is its own `assistant` event that repeats
the `message.id`, and the messages of a subagent carry the `parent_tool_use_id` of the `Task` that started it.
"""

import json
from dataclasses import asdict
from pathlib import Path
from typing import Any

from factory.claude_code import attempt_result, attempt_stats

STREAMS = Path(__file__).parent / "fixtures" / "streams"


def stream(name: str) -> list[dict[str, Any]]:
    return [json.loads(line) for line in (STREAMS / name).read_text().splitlines()]


def test_requests_are_unique_message_ids_with_the_subagents_while_turns_count_the_main_agent_only() -> None:
    stats = attempt_stats(stream("subagents.ndjson"))

    assert stats.turns == 4
    assert stats.api_requests == 7


def test_tokens_come_from_model_usage_by_model_with_unknown_thinking() -> None:
    stats = attempt_stats(stream("subagents.ndjson"))

    assert stats.tokens is not None
    assert asdict(stats.tokens["claude-sonnet-5-5"]) == {
        "input_tokens": 26,
        "output_tokens": 195,
        "thinking_tokens": None,
        "cache_read_tokens": 8000,
        "cache_write_tokens": 500,
    }
    assert stats.tokens["claude-haiku-5-5"].output_tokens == 75
    assert stats.context_window == 200000


def test_durations_subagents_tools_and_denials() -> None:
    stats = attempt_stats(stream("subagents.ndjson"))

    assert (stats.duration_ms, stats.duration_api_ms) == (52000, 47000)
    assert (stats.subagents_started, stats.subagents_failed, stats.subagents_by_type) == (1, 0, {"Explore": 1})
    assert stats.tool_calls == {"Task": 1, "Grep": 1, "Read": 1, "Bash": 2, "Edit": 1}
    assert stats.tool_errors == 2
    assert stats.permission_denials == 1


def test_peak_context_is_the_main_agents_largest_request() -> None:
    assert attempt_stats(stream("subagents.ndjson")).peak_context_tokens == 8 + 4300


def test_utilization_is_the_first_and_last_reading_of_each_window() -> None:
    stats = attempt_stats(stream("subagents.ndjson"))

    assert stats.utilization_start == {"five_hour": 0.31, "seven_day": 0.12}
    assert stats.utilization_end == {"five_hour": 0.36, "seven_day": 0.12}


def test_a_field_missing_from_the_stream_is_unknown_not_zero() -> None:
    events = [e for e in stream("session.ndjson")]

    stats = attempt_stats(events)

    assert stats.turns == 3
    assert stats.duration_ms == 4200
    assert stats.duration_api_ms is None
    assert stats.api_requests is None
    assert stats.tokens is None
    assert stats.permission_denials is None
    assert stats.subagents_started is None
    assert stats.peak_context_tokens is None
    assert stats.utilization_start is None


def test_an_agent_without_a_result_still_has_the_stats_its_stream_shows() -> None:
    events = [e for e in stream("subagents.ndjson") if e["type"] != "result"]

    result = attempt_result(events, 1, "")

    assert result.failure == "executor_crash"
    assert result.usage is None
    assert result.stats is not None
    assert result.stats.api_requests == 7
    assert result.stats.turns is None
    assert result.stats.tokens is None
