"""Scripted agent that plays one Attempt from a scenario file.

It writes the scenario's files into the current directory, which is the Workspace,
and prints an NDJSON stream shaped like `claude -p --output-format stream-json`.
"""

import json
import shutil
import subprocess
import sys
import time
from pathlib import Path


def emit(event: dict[str, object]) -> None:
    print(json.dumps(event), flush=True)


def main() -> None:
    scenario = json.loads(Path(sys.argv[1]).read_text())
    for name in scenario["remove"]:
        path = Path(name)
        if path.is_dir() and not path.is_symlink():
            shutil.rmtree(path)
        else:
            path.unlink(missing_ok=True)
    for name, content in scenario["files"].items():
        path = Path(name)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    for command in scenario["run"]:
        subprocess.run(command, shell=True, check=True)
    fail = scenario.get("fail")
    if fail == "hang":
        # The child stands in for a tool the agent started, which must die with the agent.
        subprocess.Popen([sys.executable, "-c", "import time; time.sleep(0.3); open('late.txt', 'w').write('x')"])

    # The harness waits for this event, so the files are on disk before the fake clock runs.
    emit({"type": "system", "subtype": "init", "model": scenario.get("model")})
    if fail == "crash":
        sys.exit(1)
    if fail == "hang":
        time.sleep(3600)
    if fail == "auth":
        emit({"type": "result", "subtype": "success", "is_error": True, "api_error_status": 401})
        return
    emit({"type": "assistant", "message": {"content": [{"type": "text", "text": "Done."}]}})
    emit(
        {
            "type": "result",
            "subtype": "success",
            "is_error": False,
            "structured_output": scenario["output"],
        }
    )


if __name__ == "__main__":
    main()
