"""Black-box tests for phone/termux-url-opener (POSIX sh, host-run).

The opener is executed with the system `sh` (the Termux shebang does not
exist on dev machines; the script body is POSIX). `tendril` and
`termux-open-url` are stub executables on a temp PATH that append their
argv to a log file, so every test asserts on real subprocess behavior:
exit codes, executed argv, and that hostile ids are never executed.
"""
import os
import shutil
import subprocess
import tempfile
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(REPO, "phone", "termux-url-opener")

STUB = (
    "#!/bin/sh\n"
    "{ printf '%s' 'STUBNAME'"
    "; for a in \"$@\"; do printf ' <%s>' \"$a\"; done"
    "; printf '\\n'; } >> \"$STUB_LOG\"\n"
)


class UrlOpenerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = os.path.join(self.tmp.name, "home")
        self.log = os.path.join(self.tmp.name, "stub.log")
        os.mkdir(self.home)
        self.sh = shutil.which("sh") or "/bin/sh"

    # ---- harness helpers ------------------------------------------------

    def make_path_dir(self, *stub_names):
        """Fresh PATH dir holding the named stub executables."""
        path_dir = tempfile.mkdtemp(dir=self.tmp.name)
        for name in stub_names:
            path = os.path.join(path_dir, name)
            with open(path, "w") as fh:
                fh.write(STUB.replace("STUBNAME", name))
            os.chmod(path, 0o700)
        return path_dir

    def run_opener(self, *urls, path_dir=None):
        env = {
            "PATH": path_dir or self.make_path_dir("tendril", "termux-open-url"),
            "HOME": self.home,
            "STUB_LOG": self.log,
        }
        return subprocess.run([self.sh, SCRIPT, *urls], env=env,
                              capture_output=True, text=True, timeout=30)

    def reset_log(self):
        if os.path.exists(self.log):
            os.unlink(self.log)

    def log_text(self):
        with open(self.log) as fh:
            return fh.read()

    def assert_enter(self, urls, session_id, extra_absent=None):
        """Opener must exec the tendril stub with ['enter', session_id]."""
        self.reset_log()
        r = self.run_opener(*urls)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn(f"tendril <enter> <{session_id}>", self.log_text())
        self.assertNotIn("termux-open-url", self.log_text())
        self.assertNotIn("termux-open ", self.log_text())
        for absent in extra_absent or ():
            self.assertNotIn(absent, self.log_text())

    def assert_reject(self, url, fragment):
        """Known TENDRIL form + malformed id: non-zero exit, nothing run."""
        self.reset_log()
        r = self.run_opener(url)
        self.assertNotEqual(r.returncode, 0, r.stderr)
        log = self.log_text() if os.path.exists(self.log) else ""
        self.assertEqual(log, "", "a stub was executed for a rejected id")
        everywhere = r.stdout + r.stderr + log
        self.assertNotIn(fragment, everywhere)

    # ---- accepted forms -------------------------------------------------

    def test_path_form_enters(self):
        self.assert_enter(["https://host.example/.tendril/w15"], "w15")

    def test_path_form_with_query_junk(self):
        self.assert_enter(
            ["https://host.example/.tendril/w15?utm=1&x=%20"], "w15")

    def test_path_form_last_segment_wins(self):
        self.assert_enter(
            ["https://host.example/.tendril/old/.tendril/w15"], "w15")

    def test_query_p_form(self):
        self.assert_enter(["https://host.example/page?p=wK"], "wK")

    def test_query_p_form_with_more_pairs(self):
        self.assert_enter(["https://h/p?a=1&p=w15&b=2"], "w15")

    def test_fragment_p_form(self):
        self.assert_enter(["https://host.example/page#p=wX"], "wX")

    def test_query_id_form(self):
        self.assert_enter(["https://host.example/open?id=w13"], "w13")

    def test_fragment_id_form(self):
        self.assert_enter(["https://host.example/open#id=w9"], "w9")

    def test_query_wins_when_both_query_and_fragment_carry_ids(self):
        self.assert_enter(["https://h/x?id=w1#p=w2"], "w1")

    def test_tendril_uri_form(self):
        self.assert_enter(["tendril://host/h/workspace/w15"], "w15")

    def test_tendril_uri_form_with_label_query(self):
        self.assert_enter(
            ["tendril://host/h/workspace/w15?label=proj%20x"], "w15")

    def test_pane_id_accepted(self):
        self.assert_enter(["https://h/.tendril/w15:p1"], "w15:p1")

    def test_pane_id_via_query_pair_routed_to_enter(self):
        self.assert_enter(["https://h/open?p=w15:p3"], "w15:p3")
        self.assert_enter(["https://h/open?id=wX:pB#junk"], "wX:pB")

    def test_tendril_uri_with_pane_query_yields_the_workspace(self):
        # the tendril:// form carries the workspace id; exact panes ride
        # the /.tendril/<target> path carrier
        self.assert_enter(["tendril://host/h/workspace/w15?pane=w15%3Ap3"],
                          "w15")

    def test_max_id_128_accepted(self):
        self.assert_enter(["https://h/.tendril/" + "a" * 128], "a" * 128)

    def test_first_tendril_url_wins_and_suppresses_passthrough(self):
        self.assert_enter(
            ["https://example.com/page", "https://h/.tendril/w15"], "w15")

    # ---- rejected: known form, malformed id -----------------------------

    def test_reject_command_substitution(self):
        self.assert_reject("https://h/.tendril/$(reboot)", "reboot")

    def test_reject_semicolon_payload(self):
        self.assert_reject("https://h/.tendril/w15;rm", ";rm")

    def test_reject_python_invocation(self):
        self.assert_reject(
            "https://h/.tendril/$(python -c 'import os;os.system(\"id\")')",
            "python -c")

    def test_reject_empty_id(self):
        self.assert_reject("https://h/.tendril/", "unused")

    def test_reject_oversized_id(self):
        self.assert_reject("https://h/.tendril/" + "a" * 200, "a" * 200)

    def test_reject_129_boundary(self):
        self.assert_reject("https://h/.tendril/" + "a" * 129, "a" * 129)

    def test_reject_spaces(self):
        self.assert_reject("https://h/.tendril/w15 w16", "w15 w16")

    def test_reject_query_pair_with_hostile_value(self):
        self.assert_reject("https://h/x?p=$(reboot)", "reboot")

    # ---- passthrough -----------------------------------------------------

    def test_passthrough_uses_termux_open_url(self):
        self.reset_log()
        r = self.run_opener("https://example.com/page")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.log_text(),
                         "termux-open-url <https://example.com/page>\n")

    def test_passthrough_falls_back_to_termux_open(self):
        self.reset_log()
        r = self.run_opener("https://example.com/page",
                            path_dir=self.make_path_dir("termux-open"))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.log_text(),
                         "termux-open <https://example.com/page>\n")

    # ---- missing handlers -------------------------------------------------

    def test_missing_tendril_launcher_exit_127(self):
        self.reset_log()
        r = self.run_opener("https://h/.tendril/w15",
                            path_dir=self.make_path_dir("termux-open-url"))
        self.assertEqual(r.returncode, 127, r.stderr)
        self.assertEqual(self.log_text() if os.path.exists(self.log) else "",
                         "")
        self.assertIn("tendril", r.stderr)

    def test_no_handlers_at_all_exit_127_with_guidance(self):
        self.reset_log()
        r = self.run_opener("https://example.com/page",
                            path_dir=self.make_path_dir())
        self.assertEqual(r.returncode, 127, r.stderr)
        self.assertIn("termux", r.stderr.lower())

    def test_no_handlers_tendril_url_also_exit_127(self):
        self.reset_log()
        r = self.run_opener("https://h/.tendril/w15",
                            path_dir=self.make_path_dir())
        self.assertEqual(r.returncode, 127, r.stderr)
        self.assertEqual(self.log_text() if os.path.exists(self.log) else "",
                         "")


if __name__ == "__main__":
    unittest.main()
