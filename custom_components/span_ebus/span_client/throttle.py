"""When a fast reading is worth a state write, and what value to write.

The stream brings a frame or two a second, and every circuit's power moves a
little in each one. Written straight through, that is a new row in Home
Assistant's history for nearly every sensor every second — on a two-panel
account about 25 rows a second, which filled a Home Assistant Green's 32 GB in
days. History does not need that: what it needs is the shape of the load.

So a sensor's published value follows four rules:

* **Average, not sample.** Between writes the readings are averaged over time,
  and the average is what gets written. A 30 s figure that is the mean of the
  frames inside it keeps statistics honest; one frame picked every 30 s would not.
* **Deadband.** When the window closes, the average is written only if it moved
  by more than a little from the last value written. A fridge holding 120 W
  writes nothing while it holds.
* **Jump.** A reading that moves far from the last written value is written at
  once, as itself, so a kettle switching on or a breaker tripping shows up
  without waiting for the window.
* **Heartbeat.** However steady the load, the average goes out after this long,
  so the history never has a gap long enough to look like an outage.

Plain functions and a small class, with no Home Assistant import, so the rules
can be tested on their own. The raw readings are untouched — the coordinator
still holds every frame, for anything that wants the live figure.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Policy:
    """How one kind of reading is written."""

    # Shortest time between routine writes, in seconds.
    interval: float
    # Longest time between writes, however steady the reading.
    heartbeat: float
    # A window's average must move at least this much (absolute, or as a
    # fraction of the last written value, whichever is larger) to be written.
    deadband_abs: float
    deadband_rel: float
    # A single reading this far from the last written value is written at once.
    jump_abs: float
    jump_rel: float
    # Values are rounded to this many decimals before they are compared or written.
    decimals: int

    def threshold(self, absolute: float, relative: float, reference: float) -> float:
        return max(absolute, relative * abs(reference))


# Per unit. Power is what history is mostly made of, so it is the one tuned with
# care; the rest change slowly and are diagnostics.
POWER = Policy(
    interval=30.0,
    heartbeat=300.0,
    deadband_abs=5.0,
    deadband_rel=0.02,
    jump_abs=50.0,
    jump_rel=0.20,
    decimals=0,
)
CURRENT = Policy(
    interval=30.0,
    heartbeat=300.0,
    deadband_abs=0.1,
    deadband_rel=0.02,
    jump_abs=0.5,
    jump_rel=0.20,
    decimals=1,
)
VOLTAGE = Policy(
    interval=60.0,
    heartbeat=600.0,
    deadband_abs=0.5,
    deadband_rel=0.0,
    jump_abs=5.0,
    jump_rel=0.0,
    decimals=1,
)
FREQUENCY = Policy(
    interval=60.0,
    heartbeat=600.0,
    deadband_abs=0.02,
    deadband_rel=0.0,
    jump_abs=0.2,
    jump_rel=0.0,
    decimals=2,
)

_BY_UNIT = {"W": POWER, "A": CURRENT, "V": VOLTAGE, "Hz": FREQUENCY}


def policy_for(unit: str | None) -> Policy | None:
    """The policy for a unit, or None for readings that are written as they come."""
    return _BY_UNIT.get(unit or "")


class Throttle:
    """Decides, reading by reading, whether a sensor should write and what.

    Feed every reading to `offer` with the time it was true at. It returns the
    value to write, or None to write nothing. The first reading is always
    written, so a new sensor shows a value straight away.
    """

    def __init__(self, policy: Policy) -> None:
        self.policy = policy
        self.written: float | None = None
        self._written_at = 0.0
        # Time-weighted sum over the current window: each reading holds until
        # the next one, so it counts for the time until then.
        self._window_start = 0.0
        self._area = 0.0
        self._last_value: float | None = None
        self._last_at = 0.0

    def _round(self, value: float) -> float:
        return round(value, self.policy.decimals)

    def _restart(self, value: float, at: float) -> None:
        self._window_start = at
        self._area = 0.0
        self._last_value = value
        self._last_at = at

    def _write(self, value: float, at: float, next_value: float) -> float:
        self.written = value
        self._written_at = at
        self._restart(next_value, at)
        return value

    def average(self, now: float) -> float | None:
        """The time-weighted average of the window so far, or None if it is empty."""
        if self._last_value is None:
            return None
        span = now - self._window_start
        area = self._area + self._last_value * max(0.0, now - self._last_at)
        if span <= 0:
            return self._last_value
        return area / span

    def offer(self, value: float, at: float) -> float | None:
        """Take one reading; return the value to write, or None."""
        policy = self.policy
        if self.written is None or self._last_value is None:
            return self._write(self._round(value), at, value)

        # Readings that arrive out of order or twice count for no time.
        at = max(at, self._last_at)

        if abs(value - self.written) >= policy.threshold(
            policy.jump_abs, policy.jump_rel, self.written
        ):
            return self._write(self._round(value), at, value)

        mean = self.average(at)
        self._area += self._last_value * (at - self._last_at)
        self._last_value = value
        self._last_at = at

        # A window closes after `interval`, whether or not it ends in a write.
        if at - self._window_start < policy.interval or mean is None:
            return None

        rounded = self._round(mean)
        moved = abs(rounded - self.written) >= policy.threshold(
            policy.deadband_abs, policy.deadband_rel, self.written
        )
        if moved or at - self._written_at >= policy.heartbeat:
            return self._write(rounded, at, value)
        # Not worth writing: start a fresh window, so the next average describes
        # the load now rather than everything since the last write.
        self._restart(value, at)
        return None
