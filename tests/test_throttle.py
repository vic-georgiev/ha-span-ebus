"""The write rules for fast readings: average, deadband, jump, heartbeat."""

from __future__ import annotations

from span_client.throttle import CURRENT, POWER, Policy, Throttle, policy_for


def feed(throttle: Throttle, samples: list[tuple[float, float]]) -> list[tuple[float, float]]:
    """Offer (time, value) pairs; return the (time, value) pairs that were written."""
    out = []
    for at, value in samples:
        written = throttle.offer(value, at)
        if written is not None:
            out.append((at, written))
    return out


def test_first_reading_is_written_at_once() -> None:
    assert feed(Throttle(POWER), [(0.0, 123.4)]) == [(0.0, 123.0)]


def test_steady_load_writes_only_the_heartbeat() -> None:
    throttle = Throttle(POWER)
    # 120 W wobbling by a watt, twice a second, for ten minutes.
    samples = [(t / 2, 120.0 + (1 if t % 2 else -1)) for t in range(1200)]
    written = feed(throttle, samples)
    times = [at for at, _ in written]
    assert times[0] == 0.0
    # Nothing moved past the deadband, so only the 300 s heartbeat writes.
    assert len(written) == 2
    assert 300.0 <= times[1] < 330.0
    assert written[1][1] == 120.0


def test_jump_is_written_immediately_as_itself() -> None:
    throttle = Throttle(POWER)
    written = feed(throttle, [(0.0, 10.0), (1.0, 10.0), (2.0, 1510.0)])
    assert written == [(0.0, 10.0), (2.0, 1510.0)]


def test_window_writes_the_time_weighted_average() -> None:
    throttle = Throttle(POWER)
    feed(throttle, [(0.0, 100.0)])
    # 100 W for 10 s, then 130 W for 20 s (each under the 50 W / 20 % jump),
    # closing the window at 30 s: (100*10 + 130*20) / 30 = 120.
    written = feed(throttle, [(10.0, 130.0), (30.0, 130.0)])
    assert written == [(30.0, 120.0)]


def test_small_drift_inside_the_deadband_is_not_written() -> None:
    throttle = Throttle(POWER)
    feed(throttle, [(0.0, 200.0)])
    # 203 W is 3 W off 200, under max(5 W, 2 % of 200 = 4 W).
    assert feed(throttle, [(t, 203.0) for t in range(1, 61)]) == []


def test_window_restarts_after_a_quiet_close() -> None:
    throttle = Throttle(POWER)
    feed(throttle, [(0.0, 200.0)])
    # First window holds 200 W and closes quietly at 30 s; the load then sits at
    # 230 W (under the 50 W jump). The next window averages 230, not a blend of
    # everything since the last write.
    samples = [(float(t), 200.0) for t in range(1, 31)]
    samples += [(float(t), 230.0) for t in range(31, 61)]
    written = feed(throttle, samples)
    assert len(written) == 1
    at, value = written[0]
    assert 59.0 <= at <= 60.0
    assert value >= 228.0


def test_out_of_order_reading_counts_for_no_time() -> None:
    throttle = Throttle(POWER)
    feed(throttle, [(10.0, 100.0)])
    assert throttle.offer(100.0, 5.0) is None
    assert throttle.average(10.0) == 100.0


def test_current_rounds_to_a_tenth() -> None:
    assert feed(Throttle(CURRENT), [(0.0, 3.14159)]) == [(0.0, 3.1)]


def test_policy_for_units() -> None:
    assert policy_for("W") is POWER
    assert policy_for("A") is CURRENT
    assert policy_for("Wh") is None
    assert policy_for(None) is None


def test_relative_jump_scales_with_the_load() -> None:
    policy = Policy(
        interval=30.0,
        heartbeat=300.0,
        deadband_abs=5.0,
        deadband_rel=0.02,
        jump_abs=50.0,
        jump_rel=0.20,
        decimals=0,
    )
    throttle = Throttle(policy)
    feed(throttle, [(0.0, 3000.0)])
    # 3000 -> 3400 is 400 W: under 20 % of 3000 (600 W), so it waits for the window.
    assert throttle.offer(3400.0, 1.0) is None
    # 3000 -> 3700 is 700 W: over it, written at once.
    assert throttle.offer(3700.0, 2.0) == 3700.0
