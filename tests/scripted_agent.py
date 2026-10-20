"""Scripted agent that plays one Attempt from a scenario file.

It writes the scenario's files into the current directory, which is the Workspace,
and prints an NDJSON stream shaped like `claude -p --output-format stream-json`.
"""

import json
import sys
from pathlib import Path


def emit(event: dict[str, object]) -> None:
    print(json.dumps(event), flush=True)


def main() -> None:
    scenario = json.loads(Path(sys.argv[1]).read_text())
    emit({"type": "system", "subtype": "init", "model": scenario.get("model")})
    for name, content in scenario["files"].items():
        path = Path(name)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
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
