"""A laptop that goes to sleep must not cost somebody a run.

The time limit on a run is a bill cap: it exists so an afternoon of model
credit cannot be spent unnoticed. A suspended process spends nothing — no turn
runs, no token is bought — but the wall clock keeps going, so a run that was
three minutes into a thirty-minute budget used to wake up over its limit and be
stopped for time it never had.

These tests hold both halves of the answer: the machine is asked to stay awake
while a run works, and when it sleeps anyway the time it was away is given back
rather than charged.
"""
import time
import unittest
from unittest import mock

from biosense.production import awake as AW
from biosense.production import clock as CL


class FakeClocks:
    """Wall and monotonic, moved by hand. Sleeping advances only the wall."""

    def __init__(self, wall=None, mono=1000.0):
        # Anchored to the real clock: a run's deadline is a real timestamp, and
        # a fake wall in another decade would make every comparison vacuous.
        self.w, self.m = (time.time() if wall is None else wall), mono

    def work(self, s):
        """Time with the machine running: both clocks advance."""
        self.w += s
        self.m += s

    def sleep(self, s):
        """Time with the machine suspended: only the sun moves."""
        self.w += s

    def watch(self, **kw):
        return CL.SuspendWatch(wall=lambda: self.w, mono=lambda: self.m, **kw)


class SuspendWatchTests(unittest.TestCase):
    def test_a_long_turn_is_not_mistaken_for_a_sleep(self):
        """The distinction the whole thing rests on: an hour of work and an
        hour of sleep look identical on the wall clock."""
        c = FakeClocks()
        w = c.watch()
        c.work(3600)
        self.assertEqual(0.0, w.check())
        self.assertEqual(0.0, w.total_s)

    def test_a_sleep_is_measured_as_the_difference_between_the_two_clocks(self):
        c = FakeClocks()
        w = c.watch()
        c.work(120)
        c.sleep(2700)
        c.work(5)
        self.assertAlmostEqual(2700.0, w.check(), places=6)
        self.assertEqual(1, w.snapshot()['suspensions'])

    def test_each_sleep_is_counted_once_and_only_once(self):
        c = FakeClocks()
        w = c.watch()
        c.sleep(600)
        self.assertAlmostEqual(600.0, w.check())
        self.assertEqual(0.0, w.check(), 'the same gap must not be credited twice')
        c.sleep(600)
        self.assertAlmostEqual(600.0, w.check())
        self.assertAlmostEqual(1200.0, w.snapshot()['suspended_s'])

    def test_jitter_and_a_backwards_clock_are_not_sleep(self):
        """A clock correction must not hand a run free time: an error in the
        run's favour is still an error, and nobody asked for it."""
        c = FakeClocks()
        w = c.watch()
        c.sleep(3)                      # scheduler jitter, an NTP nudge
        self.assertEqual(0.0, w.check())
        c.w -= 100                      # the clock was set backwards
        self.assertEqual(0.0, w.check())
        self.assertEqual(0.0, w.total_s)

    def test_the_platform_says_whether_it_can_see_a_sleep_at_all(self):
        """Windows counts suspended time in its monotonic clock, so no gap is
        ever visible there. That is a weaker promise, and it is stated rather
        than implied."""
        with mock.patch.object(CL.sys, 'platform', 'win32'):
            self.assertFalse(CL.credits_suspension())
        with mock.patch.object(CL.sys, 'platform', 'darwin'):
            self.assertTrue(CL.credits_suspension())

    def test_a_gap_is_described_for_a_person_not_a_log(self):
        self.assertEqual('45 seconds', CL.describe(45))
        self.assertEqual('50 minutes', CL.describe(50 * 60))
        self.assertEqual('2 h 30 min', CL.describe(150 * 60))


class WakeLockTests(unittest.TestCase):
    def test_each_platform_asks_the_way_that_platform_asks(self):
        calls = []

        class P:
            def poll(self): return None
            def terminate(self): calls.append('terminate')
            def wait(self, timeout=None): return 0

        h = AW.hold(platform='darwin', spawn=lambda a: calls.append(a) or P(), pid=99)
        self.assertTrue(h.held)
        # -w: caffeinate dies with this process, so a crash here cannot leave a
        # machine awake for ever.
        self.assertEqual(['caffeinate', '-i', '-w', '99'], calls[0])
        self.assertTrue(h.release())
        self.assertIn('terminate', calls)
        self.assertFalse(h.release(), 'releasing twice is not an error and not a second kill')

        calls.clear()
        AW.hold(platform='linux', spawn=lambda a: calls.append(a) or P())
        self.assertEqual('systemd-inhibit', calls[0][0])
        self.assertIn('--what=idle:sleep', calls[0])

    def test_a_machine_that_cannot_be_held_awake_says_so_and_runs_anyway(self):
        """A wake lock is a request. Failing to get one may never stop a run."""
        def missing(_argv):
            raise FileNotFoundError('systemd-inhibit')
        h = AW.hold(platform='linux', spawn=missing)
        self.assertFalse(h.held)
        self.assertIn('not installed', h.why)
        self.assertFalse(AW.hold(platform='sunos5').held)

        def exploding(_argv):
            raise RuntimeError('no')
        self.assertFalse(AW.hold(platform='darwin', spawn=exploding).held)

    def test_two_runs_share_one_lock_and_the_first_to_end_does_not_drop_it(self):
        released = []

        class H(AW.Hold):
            def release(self):
                released.append(1)
                return super().release()

        k = AW.Keeper(holder=lambda r: H(held=True, how='test'))
        k.acquire()
        k.acquire()
        k.release()
        self.assertEqual([], released, 'one run ending must not wake-unlock the other')
        self.assertEqual(2 - 1, k.snapshot()['runs'])
        k.release()
        self.assertEqual(1, len(released))
        self.assertFalse(k.snapshot()['held'])


class RunCreditTests(unittest.TestCase):
    """The run itself: the deadline moves, and the person is told."""

    def _run(self, **kw):
        from biosense.production import app as APP
        from biosense.production import discovery as DISC
        req = DISC.build(project_id='ipsc_macrophage', runtime_mode='local_real_ai',
                         objective='increase viable macrophage yield from iPSC')
        import tempfile
        from pathlib import Path
        run = APP.DiscoveryRun('a' * 16, req, Path(tempfile.mkdtemp()) / 'r',
                               'local_real_ai', **kw)
        return run

    def test_the_deadline_moves_by_the_time_the_machine_was_away(self):
        run = self._run(deadline=time.time() + 300)
        before = run.deadline
        c = FakeClocks()
        run.suspend = c.watch()
        c.work(60)
        c.sleep(3600)
        self.assertFalse(run.should_stop(), 'an hour asleep is not an hour of spending')
        self.assertAlmostEqual(before + 3600, run.deadline, places=3)
        notes = [e for e in run.events if 'asleep' in (e.get('simple') or '')]
        self.assertEqual(1, len(notes), 'said once, in the timeline')
        self.assertIn('spent nothing', notes[0]['simple'])
        self.assertEqual(3600, round(run.snapshot()['suspension']['suspended_s']))

    def test_without_the_credit_the_same_run_would_have_been_stopped(self):
        """The failure this exists to prevent, as a test rather than a comment.

        Two runs, the same sleep, the same budget. The only difference is
        whether the gap is seen: one is stopped for time it never spent, the
        other keeps working."""
        blind = self._run(deadline=time.time() + 300)
        c = FakeClocks()
        # A machine whose monotonic clock counts suspended time sees no gap at
        # all — which is exactly what happens on Windows today.
        blind.suspend = CL.SuspendWatch(wall=lambda: c.w, mono=lambda: c.w)
        c.work(60)
        c.sleep(3600)
        with mock.patch('time.time', lambda: c.w):
            self.assertTrue(blind.should_stop())
        self.assertEqual('timed_out', blind.cancelled)

        seeing = self._run(deadline=time.time() + 300)
        c2 = FakeClocks()
        seeing.suspend = c2.watch()
        c2.work(60)
        c2.sleep(3600)
        with mock.patch('time.time', lambda: c2.w):
            self.assertFalse(seeing.should_stop())
        self.assertIsNone(seeing.cancelled)

    def test_a_pause_is_not_timed_out_by_a_sleep_either(self):
        """A run paused at its limit holds for a bounded time and then finishes
        with what the agents wrote. A machine asleep during that hold has not
        used it up."""
        run = self._run(deadline=time.time() + 300)
        run.paused, run.paused_at = True, time.time()
        c = FakeClocks()
        run.suspend = c.watch()
        c.sleep(APP_PAUSE_HOLD() + 600)
        self.assertFalse(run.should_stop(), 'the hold was not spent waiting; it was suspended')
        self.assertIsNone(run.cancelled)

    def test_the_wrap_up_warning_is_given_again_for_the_deadline_that_moved(self):
        """The orchestrator is told to write up five minutes before the limit.
        A sleep that moves the limit by an hour makes that warning an hour
        early, so it is given again rather than counted as delivered."""
        run = self._run(deadline=time.time() + 300)
        run.suspend = CL.SuspendWatch()
        self.assertFalse(run.should_stop())
        self.assertTrue(run.wrap_up_sent, 'inside five minutes: the warning goes out')
        c = FakeClocks()
        run.suspend = c.watch()
        c.sleep(3600)
        run.should_stop()
        self.assertFalse(run.wrap_up_sent, 'the deadline it warned about is an hour away now')



def APP_PAUSE_HOLD():
    from biosense.production import app as APP
    return APP.PAUSE_HOLD_S


class PageTests(unittest.TestCase):
    def test_the_page_rebuilds_its_stream_after_a_late_tick(self):
        from biosense import contracts as K
        js = (K.ROOT / 'webapp' / 'runview.js').read_text()
        self.assertIn('visibilitychange', js)
        self.assertIn("resync('slept')", js)
        self.assertIn('snap.suspension', js)


if __name__ == '__main__':
    unittest.main()
