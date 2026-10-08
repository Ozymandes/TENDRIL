#!/usr/bin/env python3
"""Run the whole suite the way CI does, with diagnostics that name a hang.

    python3 -u tests/run_suite.py            (TENDRIL_TEST_TIMEOUT=90 default)

Same tests as `python3 -m unittest discover -s tests`, plus:
- every test name is printed (and flushed) BEFORE it runs, with its time;
- failures and errors are printed in full the moment they happen, so a
  later hang or job timeout cannot swallow them;
- a per-test watchdog: if one test runs longer than the timeout, every
  Python thread's stack is dumped (faulthandler), followed by the child
  processes still running under this runner, then the run exits 3. If a
  C call holds the GIL, faulthandler's own timer dumps the stacks 20s
  later and exits 1 (the START line above names the test).
Stdlib only; Python 3.9+.
"""
import faulthandler
import os
import subprocess
import sys
import threading
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
TIMEOUT = float(os.environ.get("TENDRIL_TEST_TIMEOUT", "90"))


def _descendants():
    """`ps` lines for every live descendant of this process."""
    try:
        out = subprocess.run(["ps", "-A", "-o", "pid=,ppid=,etime=,command="],
                             capture_output=True, text=True, timeout=10).stdout
    except (OSError, subprocess.TimeoutExpired) as e:
        return ["(ps unavailable: %s)" % e]
    rows = {}
    for ln in out.splitlines():
        parts = ln.split(None, 3)
        if len(parts) >= 3 and parts[0].isdigit() and parts[1].isdigit():
            rows[int(parts[0])] = (int(parts[1]), ln.strip())
    keep, frontier = [], {os.getpid()}
    while frontier:
        nxt = {pid for pid, (ppid, _) in rows.items() if ppid in frontier}
        keep += [rows[p][1] for p in sorted(nxt)]
        frontier = nxt
    return keep or ["(no child processes)"]


class Watchdog:
    def __init__(self, stream):
        self.stream = stream
        self.timer = None

    def arm(self, name):
        self.cancel()
        self.timer = threading.Timer(TIMEOUT, self.fire, args=(name,))
        self.timer.daemon = True
        self.timer.start()
        # Backstop that needs no GIL: a C call that blocks while holding it
        # (termios.tcsetattr on Python 3.9) starves the thread above.
        faulthandler.dump_traceback_later(TIMEOUT + 20, exit=True)

    def cancel(self):
        faulthandler.cancel_dump_traceback_later()
        if self.timer:
            self.timer.cancel()
            self.timer = None

    def fire(self, name):
        w = self.stream
        w.write("\n\n######## HUNG TEST (> %ss): %s\n" % (TIMEOUT, name))
        w.write("######## Python stacks (all threads):\n")
        w.flush()
        faulthandler.dump_traceback(file=sys.stderr, all_threads=True)
        sys.stderr.flush()
        w.write("######## child processes still running:\n")
        for ln in _descendants():
            w.write("    " + ln + "\n")
        w.write("######## aborting the run (exit 3)\n")
        w.flush()
        os._exit(3)


class Result(unittest.TextTestResult):
    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.watchdog = Watchdog(self.stream)

    def startTest(self, test):
        self._t0 = time.monotonic()
        self.stream.write("START %s\n" % test.id())
        self.stream.flush()
        self.watchdog.arm(test.id())
        super().startTest(test)

    def stopTest(self, test):
        self.watchdog.cancel()
        super().stopTest(test)
        self.stream.write("      %.2fs\n" % (time.monotonic() - self._t0))
        self.stream.flush()

    def _now(self, kind, test, err):
        self.stream.write("\n======== %s (live): %s\n%s\n"
                          % (kind, test.id(), self._exc_info_to_string(err, test)))
        self.stream.flush()

    def addFailure(self, test, err):
        super().addFailure(test, err)
        self._now("FAIL", test, err)

    def addError(self, test, err):
        super().addError(test, err)
        self._now("ERROR", test, err)


def main():
    faulthandler.enable()
    sys.path.insert(0, HERE)
    suite = unittest.defaultTestLoader.discover(HERE, top_level_dir=HERE)
    runner = unittest.TextTestRunner(stream=sys.stderr, verbosity=2,
                                     resultclass=Result)
    result = runner.run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(main())
