"""The readers of the Journal take the handler of each event from a registry its writer fills."""

import importlib.util
from typing import Any

import pytest

import factory.run.attempt
import factory.wiring  # noqa: F401  the composition root imports every writer of an event
from factory.core.events import Event, Registry, applies


def test_every_event_has_a_handler_of_the_run_state() -> None:
    assert [event for event in Event if event not in applies] == []


def test_a_registry_rejects_a_second_handler_for_one_event() -> None:
    registry = Registry("reader")

    @registry(Event.RunStarted)
    def first(memory: Any, payload: dict[str, Any]) -> None: ...

    with pytest.raises(ValueError, match="reader has a handler for RunStarted already"):
        registry(Event.RunStarted)(first)


def test_importing_a_second_writer_of_a_handler_fails() -> None:
    spec = importlib.util.spec_from_file_location("second_attempt", factory.run.attempt.__file__)
    assert spec is not None and spec.loader is not None

    with pytest.raises(ValueError, match="run_state has a handler for StageBoundaryCommitted already"):
        spec.loader.exec_module(importlib.util.module_from_spec(spec))
