from harness import AgentScript, Harness, wait_until

TICKET = "---\ntitle: Greet\n---\nWrite hello.txt.\n"

# The agent and the renderer notice within a fraction of a second that the factory died, even on a loaded machine.
ENDS_WITHIN_SECONDS = 5


def kill_factory_during_an_attempt(factory: Harness, script: AgentScript) -> None:
    """Runs the factory in a process of its own and kills that process with SIGKILL once its agent and renderer run."""
    spec = factory.write_spec("Greeting", "Say hello.", {"t1": TICKET})
    factory.executor.scripts["implement"] = script
    run = factory.spawn_run(spec)

    def both_run() -> bool:
        commands = factory.processes().values()
        return any("factory.render" in c for c in commands) and any("scripted_agent.py" in c for c in commands)

    assert wait_until(both_run, 10), (factory.root / "spawned-run.log").read_text()
    run.kill()
    run.wait()


def test_killing_the_factory_while_its_agent_waits_ends_the_agent_and_the_renderer(factory: Harness) -> None:
    kill_factory_during_an_attempt(factory, AgentScript(wait_for=factory.root / "never"))

    assert wait_until(lambda: not factory.processes(), ENDS_WITHIN_SECONDS), factory.processes()
    [log] = factory.home.glob("runs/*/tickets/t1/implement/round-1/attempt-1/terminal.log")
    assert log.with_name("terminal.log.done").exists()
    assert "model sonnet" in log.read_text()


def test_killing_the_factory_while_its_agent_hangs_ends_the_agent_and_the_renderer(factory: Harness) -> None:
    kill_factory_during_an_attempt(factory, AgentScript(fail="hang"))

    assert wait_until(lambda: not factory.processes(), ENDS_WITHIN_SECONDS), factory.processes()


def test_killing_the_factory_while_its_agent_beats_ends_the_agent_and_the_renderer(factory: Harness) -> None:
    kill_factory_during_an_attempt(factory, AgentScript(heartbeat=True))

    assert wait_until(lambda: not factory.processes(), ENDS_WITHIN_SECONDS), factory.processes()
