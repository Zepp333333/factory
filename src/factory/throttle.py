"""The throttle is the brake that slows a Run down when it spends a subscription window faster than the config allows.

A `Brake` of a window sets a `cap`, the share of the window above which the Run starts no new Attempts,
and a `pace`, the share of the window the Run may spend by its end, evenly over time.
The brake knows windows only through the Executor port's `WindowUsage`, not through any CLI's format.
"""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from factory import values
from factory.executor import WindowUsage

# The length of each window the brake knows, in seconds.
WINDOW_SECONDS = {"five_hour": 5 * 3600, "seven_day": 7 * 86400}
SHARES = ("cap", "pace")


@dataclass(frozen=True)
class Brake:
    """A Brake is what the config sets for one window: its `cap`, its `pace`, or both."""

    cap: float | None = None
    pace: float | None = None

    def payload(self) -> dict[str, float]:
        return {k: v for k in SHARES if (v := getattr(self, k)) is not None}


@dataclass(frozen=True)
class Braked:
    """Braked says the Run starts no new Attempts until `until`, because `window` is used up to `used`.

    `kind` is the share that holds the Run back, and `limit` is what it allows: the cap, or the pace's allowance now.
    """

    window: str
    kind: str
    used: float
    limit: float
    until: datetime

    @property
    def text(self) -> str:
        what = "cap" if self.kind == "cap" else "pace"
        return f"{self.window} at {self.used:.0%}, above its {what} {self.limit:.0%}"

    def payload(self) -> dict[str, Any]:
        return {
            "window": self.window,
            "kind": self.kind,
            "used": self.used,
            "limit": self.limit,
            "until": self.until.isoformat(),
        }

    @classmethod
    def of(cls, payload: dict[str, Any]) -> "Braked":
        window, kind, used, limit = payload["window"], payload["kind"], payload["used"], payload["limit"]
        return cls(window, kind, used, limit, datetime.fromisoformat(payload["until"]))


def parse_share(value: Any) -> float:
    """Reads a share of a window, from 0 to 1."""
    if not isinstance(value, int | float) or isinstance(value, bool) or not 0 <= value <= 1:
        raise values.Invalid("must be a share from 0 to 1")
    return float(value)


def parse_brake(body: Any) -> Brake:
    """Reads the brake of one window, which sets a `cap`, a `pace` or both."""
    if not isinstance(body, dict):
        raise values.Invalid("must be a mapping with cap, pace or both")
    unknown = sorted(str(k) for k in body.keys() - set(SHARES))
    if unknown:
        raise values.Invalid(f"unknown field {', '.join(unknown)}, only cap and pace")
    if not body:
        raise values.Invalid("must set cap, pace or both")
    shares = {}
    for key in SHARES:
        if key in body:
            try:
                shares[key] = parse_share(body[key])
            except values.Invalid as error:
                raise values.Invalid(f"{key} {error}") from None
    return Brake(**shares)


class Throttle:
    """A Throttle decides from the windows' usage and the time whether the Run is held back.

    For `pace` it remembers the usage it first saw in each window, because the Run may spend its share on top of that.
    A window whose `resets_at` has passed has started over, so what is known of it is stale and counts for nothing.
    """

    def __init__(self) -> None:
        self._starts: dict[str, tuple[datetime, float]] = {}

    def check(self, brakes: dict[str, Brake], usage: dict[str, WindowUsage], now: datetime) -> Braked | None:
        """Returns the brake that holds the Run back for the longest, or None when every window is within bounds."""
        held = [b for name, brake in brakes.items() if (b := self._check(name, brake, usage.get(name), now))]
        return max(held, key=lambda b: b.until, default=None)

    def _check(self, name: str, brake: Brake, window: WindowUsage | None, now: datetime) -> Braked | None:
        if window is None or now >= window.resets_at:
            return None
        start = self._starts.get(name)
        if start is None or start[0] != window.resets_at:
            self._starts[name] = start = (window.resets_at, window.used)
        if brake.cap is not None and window.used > brake.cap:
            return Braked(name, "cap", window.used, brake.cap, window.resets_at)
        length = WINDOW_SECONDS[name]
        opened = window.resets_at - timedelta(seconds=length)
        if brake.pace is None:
            return None
        elapsed = min(max((now - opened).total_seconds() / length, 0), 1)
        allowed = start[1] + brake.pace * elapsed
        if window.used <= allowed:
            return None
        spent = window.used - start[1]
        if spent >= brake.pace:
            return Braked(name, "pace", window.used, allowed, window.resets_at)
        until = opened + timedelta(seconds=length * spent / brake.pace)
        return Braked(name, "pace", window.used, allowed, min(until.astimezone(UTC), window.resets_at))
