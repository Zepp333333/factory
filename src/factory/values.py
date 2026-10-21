"""Values parses the scalar fields config shares between Stages and Gate kinds."""

import re
from typing import Any

DURATION = re.compile(r"(\d+)([smh])")
SECONDS = {"s": 1, "m": 60, "h": 3600}


class Invalid(ValueError):
    pass


def duration(value: Any) -> float:
    """Reads a positive duration given as seconds or as a number with a unit `s`, `m` or `h`."""
    if isinstance(value, int | float) and not isinstance(value, bool) and value > 0:
        return float(value)
    match = DURATION.fullmatch(value) if isinstance(value, str) else None
    if match is None or int(match.group(1)) == 0:
        raise Invalid("must be a duration such as 90s, 45m or 2h")
    return float(int(match.group(1)) * SECONDS[match.group(2)])


def positive(value: Any) -> int:
    if not _integer(value) or value < 1:
        raise Invalid("must be a positive integer")
    return int(value)


def non_negative(value: Any) -> int:
    if not _integer(value) or value < 0:
        raise Invalid("must be a non-negative integer")
    return int(value)


def _integer(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)
