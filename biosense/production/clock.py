"""Time the computer did not spend, because it was asleep.

A run's time limit is a bill cap. It exists so a run cannot quietly spend an
afternoon of model credit, and every other property of it follows from that:
it is generous, it warns before it fires, and a person can extend it.

A laptop that sleeps breaks the one assumption the cap rests on. The agents are
suspended with everything else — no turn runs, no token is bought, nothing is
spent — but the wall clock keeps going, so the run wakes up over its limit and
is stopped for using time it never had. Somebody closed a lid and lost a run
that was three minutes into a thirty-minute budget.

Telling the two apart needs no operating-system notification. `time.time()`
counts the sun; `time.monotonic()` counts this machine running. On Linux and
macOS the monotonic clock stops while the machine is suspended, so the two
drift apart by exactly the time the machine was away:

    suspended = wall_elapsed - monotonic_elapsed

Below `MIN_GAP_S` the difference is scheduler jitter, an NTP step or a clock
being set, and is ignored; a real suspend is minutes. A *backwards* wall step
(the clock being corrected, or a timezone-naive host) reads as a negative gap
and is ignored too, because giving time back for it would be an error in the
run's favour that nobody asked for.

Where the monotonic clock *does* include suspended time — Windows counts it —
no gap is ever seen, and a run behaves exactly as it did before this module
existed: it is capped on wall-clock time. That is a weaker promise, not a wrong
one, and `credits_suspension()` says which machine this is so the interface can
say so too rather than implying a guarantee it cannot keep.

What this module does NOT do is keep a sleeping computer working. Nothing in a
suspended process runs; `awake.py` is the part that asks the machine to stay up
while a run is live. This module is what makes sleeping harmless when it
happens anyway.
"""
from __future__ import annotations

import sys
import time

# Below this a difference between the two clocks is jitter or a clock
# correction, not a suspend. A machine that sleeps, sleeps for minutes.
MIN_GAP_S = 30.0


def credits_suspension():
    """Whether this platform's monotonic clock stops while suspended.

    Linux (CLOCK_MONOTONIC) and macOS (mach_absolute_time) stop; Windows counts
    suspended time, so the gap is never visible there.
    """
    return not sys.platform.startswith('win')


class SuspendWatch:
    """Samples both clocks and reports how long the machine was away.

    One instance per run, polled from wherever the run already checks whether
    it should stop. No thread: a suspended process has no threads running, so
    nothing could sample during the gap anyway — the gap is measured on the
    other side of it, which is the only side there is.
    """

    def __init__(self, *, min_gap_s=MIN_GAP_S, wall=time.time, mono=time.monotonic):
        self._wall, self._mono = wall, mono
        self.min_gap_s = min_gap_s
        self._last_wall = wall()
        self._last_mono = mono()
        self.total_s = 0.0
        self.events = 0

    def check(self):
        """Seconds the machine was suspended since the last check; 0.0 if none."""
        w, m = self._wall(), self._mono()
        gap = (w - self._last_wall) - (m - self._last_mono)
        self._last_wall, self._last_mono = w, m
        if gap < self.min_gap_s:
            return 0.0
        self.total_s += gap
        self.events += 1
        return gap

    def snapshot(self):
        return {'suspended_s': round(self.total_s, 1), 'suspensions': self.events,
                'credited': credits_suspension()}


def describe(seconds):
    """'41 minutes' — for a sentence a person reads, not a log line."""
    s = int(round(seconds))
    if s < 90:
        return f'{s} seconds'
    m = int(round(s / 60))
    if m < 90:
        return f'{m} minutes'
    h, rm = divmod(m, 60)
    return f'{h} h {rm:02d} min'
